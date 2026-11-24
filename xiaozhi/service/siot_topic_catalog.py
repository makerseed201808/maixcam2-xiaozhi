"""Read the SIoT server topic catalog independently of MQTT subscriptions.

Uses only the locally configured server. Login tokens exist for one call,
redirects and environment proxies are disabled, and responses are bounded.
"""
import ipaddress
import json
from pathlib import Path
import re
import math
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError('', code, 'SIoT redirects are disabled', headers, fp)


class SiotTopicCatalog:
    MAX_RESPONSE = 512 * 1024
    TIMEOUT = 3
    MAX_TOPICS = 512
    CONFIRM_TTL = 120

    def __init__(self, config_path='/etc/siot/monitor.json', opener=None):
        self._path = config_path
        self._opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self._lock = threading.RLock()
        self._confirmations = {}

    @staticmethod
    def _host(value):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError('Invalid host')
        if any(char in value for char in '/@?#\\%') or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise ValueError('Invalid host')
        literal = value[1:-1] if value.startswith('[') and value.endswith(']') else value
        try:
            address = ipaddress.ip_address(literal)
            return '[' + address.compressed + ']' if address.version == 6 else address.compressed
        except ValueError:
            if ':' in value or '[' in value or ']' in value:
                raise ValueError('Invalid host') from None
        hostname = value.encode('idna').decode('ascii')
        if len(hostname) > 253:
            raise ValueError('Invalid host')
        labels = hostname.rstrip('.').split('.')
        if not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) for label in labels):
            raise ValueError('Invalid host')
        return hostname

    def _config(self):
        with Path(self._path).open('rb') as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise ValueError('Configuration too large')
        config = json.loads(raw.decode('utf-8'))
        if not isinstance(config, dict):
            raise ValueError('Invalid configuration')
        for field in ('host', 'username', 'password'):
            if not isinstance(config.get(field), str) or not config[field]:
                raise ValueError('Missing configuration')
        self._host(config['host'])
        host = self._host(config.get('http_host', config['host']))
        port = config.get('http_port', 8080)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid HTTP port')
        return config, 'http://%s:%d/api/v2' % (host, port)

    def _request(self, url, body=None, token=None, method=None):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['authorization'] = token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method or ('GET' if body is None else 'POST'))
        with self._opener.open(request, timeout=self.TIMEOUT) as response:
            raw = response.read(self.MAX_RESPONSE + 1)
        if len(raw) > self.MAX_RESPONSE:
            raise ValueError('Response too large')
        result = json.loads(raw.decode('utf-8'))
        if (not isinstance(result, dict) or type(result.get('code')) is not int
                or result['code'] != 0):
            raise ValueError('Invalid API response')
        return result.get('data')

    @staticmethod
    def _name(value):
        if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 128
                or any(char in value for char in '+#')
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('Invalid topic name')
        return value

    @staticmethod
    def _integer(value, low, high, name):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(name + '超出允许的整数范围')

    @staticmethod
    def _cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('请求已取消，未提交操作')

    def _login(self):
        config, base = self._config()
        login = self._request(base + '/login', {'username': config['username'], 'password': config['password']})
        token = login.get('token') if isinstance(login, dict) else None
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or not token.strip()
                or any(ord(char) < 32 or ord(char) > 126 for char in token)):
            raise ValueError('Invalid login token')
        return config, base, token

    def _catalog(self, base, token):
        # The supplied 2618 frontend GETs /topics and counts data.topics itself.
        # It does not use data.total. Fetch once with a bounded response, then
        # paginate locally instead of assuming undocumented server pagination.
        data = self._request(base + '/topics', token=token)
        records = data.get('topics') if isinstance(data, dict) else None
        if not isinstance(records, list) or len(records) > self.MAX_TOPICS:
            raise ValueError('Invalid or oversized topic catalog')
        total = data.get('total', len(records))
        if type(total) is not int or total != len(records):
            raise ValueError('Incomplete topic catalog')
        out = []
        for record in records:
            if not isinstance(record, dict): raise ValueError('Invalid topic record')
            name = self._name(record.get('name'))
            identity = record.get('id')
            if type(identity) is not int or not 0 < identity < 2**63:
                raise ValueError('Invalid topic id')
            out.append({'name': name, 'id': identity})
        if len({x['name'] for x in out}) != len(out) or len({x['id'] for x in out}) != len(out):
            raise ValueError('Ambiguous topic catalog')
        return out

    @staticmethod
    def _failure(operation):
        # Never propagate HTTP bodies, headers, URLs, credentials or tokens.
        return RuntimeError('SIoT' + operation + '失败或结果未确认；请查询状态，勿自动重试写操作')

    def list_topics(self, limit=20, offset=0):
        self._integer(limit, 1, 64, 'limit'); self._integer(offset, 0, self.MAX_TOPICS, 'offset')
        with self._lock:
            try:
                _, base, token = self._login()
                all_topics = self._catalog(base, token)
                names = [item['name'] for item in all_topics[offset:offset + limit]]
                return dict(source='server_catalog', topics=names, total=len(all_topics), count_known=True,
                            returned=len(names), has_more=offset + len(names) < len(all_topics), limit=limit, offset=offset)
            except Exception:
                raise self._failure('服务器主题目录读取') from None

    def _owned(self, topic, config):
        topic = self._name(topic)
        prefix = config['username'] + '/'
        if not topic.startswith(prefix) or not topic[len(prefix):].strip():
            raise ValueError('修改主题必须使用当前账号前缀及非空名称')
        return topic[len(prefix):]

    def _lookup(self, topic, base, token):
        return next((item for item in self._catalog(base, token) if item['name'] == topic), None)

    def _history(self, topic, base, token, limit, offset, start_time, end_time):
        query = urllib.parse.urlencode(dict(length=limit, offset=offset, startTime=start_time, endTime=end_time))
        data = self._request(base + '/messages/getMsgByTopic?' + query, {'topic': topic}, token)
        records = data.get('messages') if isinstance(data, dict) else None
        total = data.get('total') if isinstance(data, dict) else None
        if (not isinstance(records, list) or len(records) > limit or type(total) is not int
                or not 0 <= total < 2**63 or (records and offset + len(records) > total)):
            raise ValueError('Invalid history response')
        messages = []
        ids = set()
        for record in records:
            if not isinstance(record, dict): raise ValueError('Invalid history record')
            identity = record.get('id')
            if not ((type(identity) is int and 0 < identity < 2**63) or
                    (isinstance(identity, str) and 0 < len(identity) <= 128)):
                raise ValueError('Invalid message identity')
            if str(identity) in ids: raise ValueError('Duplicate history identity')
            ids.add(str(identity))
            timestamp = record.get('timestamp')
            if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError('Invalid history timestamp')
            content = record.get('content')
            if not isinstance(content, str): raise ValueError('Invalid history text')
            raw = content.encode('utf-8')
            messages.append(dict(id=identity, message=raw[:4096].decode('utf-8', 'ignore'),
                                 timestamp=timestamp, source='history', truncated=len(raw) > 4096, bytes=len(raw)))
        # Supplied 2618 frontend main.d9df4e1c.js, component dg: fetches this
        # endpoint without an order argument, displays messages[0].content as
        # "最新数据" and its timestamp as "数据时间". Preserve that contract;
        # no invented sorting parameter or local page-only reordering.
        return dict(topic=topic, source='history', messages=messages, total=total, returned=len(messages),
                    offset=offset, has_more=offset + len(messages) < total,
                    order='newest_first_server_contract', order_evidence='supplied_2618_frontend')

    def get_history(self, topic, limit=10, offset=0, start_time=0, end_time=0):
        topic = self._name(topic)
        self._integer(limit, 1, 30, 'limit'); self._integer(offset, 0, 10000, 'offset')
        self._integer(start_time, 0, 2**53 - 1, 'start_time'); self._integer(end_time, 0, 2**53 - 1, 'end_time')
        end_time = end_time or int(time.time())
        if start_time > end_time: raise ValueError('起始时间不能晚于结束时间')
        with self._lock:
            try:
                _, base, token = self._login()
                return self._history(topic, base, token, limit, offset, start_time, end_time)
            except Exception:
                raise self._failure('历史读取') from None

    def create_topic(self, topic, cancel_event=None):
        topic = self._name(topic)
        self._cancelled(cancel_event)
        with self._lock:
            try:
                config, base, token = self._login()
                relative = self._owned(topic, config)
                current = self._lookup(topic, base, token)
                if current is not None:
                    return dict(topic=topic, topic_id=current['id'], created=False, verified=True)
                self._cancelled(cancel_event)
                self._request(base + '/topics', {'name': relative}, token, method='PUT')
                current = self._lookup(topic, base, token)
                if current is None: raise ValueError('Creation not confirmed')
                return dict(topic=topic, topic_id=current['id'], created=True, verified=True)
            except Exception:
                raise self._failure('主题创建') from None

    def ensure_topic(self, topic, cancel_event=None):
        return self.create_topic(topic, cancel_event=cancel_event)

    def prepare_change(self, action, topic):
        if action not in ('delete', 'clear'): raise ValueError('操作必须为delete或clear')
        topic = self._name(topic)
        with self._lock:
            try:
                config, base, token = self._login()
                self._owned(topic, config)
                current = self._lookup(topic, base, token)
                if current is None: raise ValueError('Topic not found')
                now = time.monotonic()
                self._confirmations = {key: value for key, value in self._confirmations.items() if value['expires'] > now}
                if len(self._confirmations) >= 16: raise ValueError('Too many confirmations')
                confirmation = secrets.token_urlsafe(24)
                self._confirmations[confirmation] = dict(action=action, topic=topic, topic_id=current['id'],
                    expires=now + self.CONFIRM_TTL, base=base, username=config['username'])
                return dict(confirmation_required=True, confirmation_token=confirmation, action=action,
                    topic=topic, topic_id=current['id'], expires_in_seconds=self.CONFIRM_TTL,
                    summary=('删除主题及其全部历史数据' if action == 'delete' else '清空全部历史数据，保留主题') + '：' + topic)
            except Exception:
                raise self._failure('操作准备') from None

    def confirm_change(self, token, cancel_event=None):
        if not isinstance(token, str) or not 1 <= len(token) <= 128: raise ValueError('确认令牌不合法')
        with self._lock:
            pending = self._confirmations.pop(token, None)
            if pending is None or pending['expires'] <= time.monotonic():
                raise ValueError('确认已过期、无效或已使用，请重新准备操作')
            self._cancelled(cancel_event)
            try:
                config, base, auth = self._login()
                if base != pending['base'] or config['username'] != pending['username']:
                    raise ValueError('Server or account changed')
                topic = pending['topic']
                self._owned(topic, config)
                current = self._lookup(topic, base, auth)
                if current is None or current['id'] != pending['topic_id']:
                    raise ValueError('Topic identity changed')
                self._cancelled(cancel_event)
                if pending['action'] == 'delete':
                    self._request(base + '/topics/' + str(current['id']), token=auth, method='DELETE')
                    if self._lookup(topic, base, auth) is not None: raise ValueError('Deletion not confirmed')
                else:
                    self._request(base + '/messages/delMsgByTopic', {'topic': topic}, auth, method='POST')
                    after = self._lookup(topic, base, auth)
                    if after is None or after['id'] != current['id']: raise ValueError('Topic changed during clear')
                    history = self._history(topic, base, auth, 1, 0, 0, int(time.time()))
                    if history['total'] != 0:
                        raise ValueError('History not empty; may include new messages')
                return dict(action=pending['action'], topic=topic, topic_id=current['id'], verified=True,
                            status='deleted' if pending['action'] == 'delete' else 'cleared', automatic_retry=False)
            except Exception:
                raise self._failure('已确认操作') from None
