"""Coordinate SIoT MCP sources and session-bound destructive confirmations."""
import threading
import time
import json
from pathlib import Path


class SiotApi:
    def __init__(self, mqtt, catalog=None, media=None, devices=None, clock=time.monotonic,
                 config_path='/etc/siot/monitor.json'):
        self._config_path = config_path
        self._mqtt = mqtt if callable(mqtt) else lambda: mqtt
        self._catalog_instance = catalog
        self._media_instance = media
        self._devices_instance = devices
        self._clock = clock
        self._lock = threading.RLock()
        self._confirmations = {}

    def _catalog(self):
        with self._lock:
            if self._catalog_instance is None:
                from siot_topic_catalog import SiotTopicCatalog
                self._catalog_instance = SiotTopicCatalog()
            return self._catalog_instance

    def _media(self):
        with self._lock:
            if self._media_instance is None:
                from siot_media_control import SiotMediaControl
                self._media_instance = SiotMediaControl()
            return self._media_instance

    def _devices(self):
        with self._lock:
            if self._devices_instance is None:
                from siot_devices import SiotDevices
                self._devices_instance = SiotDevices(self._mqtt())
            return self._devices_instance

    def get_status(self):
        result = dict(self._mqtt().get_status())
        try:
            catalog = self._catalog().list_topics(limit=1)
            result.update(server_topics_total=catalog['total'], server_catalog_available=True)
        except Exception:
            result.update(server_topics_total=None, server_catalog_available=False,
                          server_catalog_note='服务器目录暂不可查询，不能推断为0个主题')
        return result

    def read_latest(self, topic, source='auto'):
        if source not in ('auto', 'live', 'history'):
            raise ValueError('source必须为auto、live或history')
        live = None
        if source != 'history':
            try:
                live = self._mqtt().read_latest(topic)
            except RuntimeError:
                if source == 'live':
                    raise
                live = {'available': False, 'reason': '实时读取不可用'}
            if live.get('available') or source == 'live':
                return live
            # Invalid newer data must not be silently replaced with old data.
            if live.get('error'):
                return live
        history = self._catalog().get_history(topic=topic, limit=1)
        messages = history['messages']
        if not messages:
            return dict(topic=topic, available=False, source='history', history_queried=True,
                        reason='未查到历史消息', live_available=False if live else None)
        item = messages[0]
        return dict(item, topic=topic, available=True, source='history', history_queried=True,
                    current_physical_state_verified=False)

    def change(self, action, topic, confirmation_token='', *, scope=None, cancel_event=None):
        if action not in ('delete', 'clear'):
            raise ValueError('不支持的主题操作')
        if not isinstance(confirmation_token, str):
            raise ValueError('确认标记必须是字符串')
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('会话已取消，未提交操作')
        with self._lock:
            now = self._clock()
            self._confirmations = {k: v for k, v in self._confirmations.items() if v[3] > now}
            if not confirmation_token:
                if len(self._confirmations) >= 16:
                    raise RuntimeError('待确认操作过多，请稍后再试')
                result = self._catalog().prepare_change(action, topic)
                token = result['confirmation_token']
                self._confirmations[token] = (action, topic, scope, self._clock() + 60)
                return dict(result, requires_confirmation=True, expires_in_seconds=60,
                            instruction='向用户说明完整主题与影响，取得明确二次确认后再用同一工具和确认标记执行')
            pending = self._confirmations.pop(confirmation_token, None)
            if pending is None or pending[:3] != (action, topic, scope):
                raise ValueError('确认已过期或不属于当前会话、主题和操作，请重新发起')
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('会话已取消，未提交操作')
            # Clear the old cache before the REST mutation; preserve messages
            # that arrive while the mutation/readback is in progress.
            self._mqtt().invalidate(topic)
            result = self._catalog().confirm_change(confirmation_token, cancel_event=cancel_event)
            if action == 'delete':
                try:
                    self._mqtt().unsubscribe(topic)
                except Exception:
                    result = dict(result, local_subscription_cleanup_confirmed=False)
            return result

    def resolve_topic(self, topic):
        from siot_tools import SiotTools
        topic = SiotTools._topic(topic)
        # A slash denotes an explicitly supplied full topic. Never guess a
        # different account, fuzzy-match suffixes or silently rewrite it.
        if '/' in topic:
            return topic
        try:
            with Path(self._config_path).open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:raise ValueError('size')
            config = json.loads(raw)
            account = config['username']
            if not isinstance(account, str) or not account or '/' in account:
                raise ValueError('account')
            return SiotTools._topic(account + '/' + topic)
        except Exception:
            raise ValueError('无法从本地配置解析主题前缀，请使用完整主题名') from None

    def call(self, method, kwargs, *, scope=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('会话已取消')
        kwargs = dict(kwargs)
        if 'topic' in kwargs:
            kwargs['topic'] = self.resolve_topic(kwargs['topic'])
        if method == 'media.show' and kwargs.get('source') == 'topic':
            kwargs['target'] = self.resolve_topic(kwargs.get('target'))
        if method == 'get_status':
            return self.get_status()
        if method == 'read_latest':
            return self.read_latest(**kwargs)
        if method in ('list_topics', 'get_history'):
            return getattr(self._catalog(), method)(**kwargs)
        if method == 'create_topic':
            return self._catalog().create_topic(**kwargs, cancel_event=cancel_event)
        if method in ('delete_topic', 'clear_history'):
            return self.change('delete' if method == 'delete_topic' else 'clear',
                               **kwargs, scope=scope, cancel_event=cancel_event)
        if method in ('publish',):
            return self._mqtt().publish(**kwargs, cancel_event=cancel_event)
        if method in ('subscribe', 'unsubscribe', 'list_subscriptions'):
            return getattr(self._mqtt(), method)(**kwargs)
        if method.startswith('media.'):
            name = method.split('.', 1)[1]
            if name not in ('get_status', 'start', 'stop', 'show'):
                raise ValueError('不支持的媒体操作')
            return getattr(self._media(), name)(**kwargs, cancel_event=cancel_event)
        if method == 'device.list_devices':
            return self._devices().list_devices()
        if method == 'device.command':
            return self._devices().command(**kwargs, cancel_event=cancel_event)
        raise ValueError('不支持的SIoT工具')
