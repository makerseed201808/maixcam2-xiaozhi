"""Bounded text MQTT tools; no history query and no arbitrary broker arguments.

QoS0 local_sent is socket completion, never an actuator acknowledgement. Reads
observe only exact-topic subscriptions started by this instance. Retained data
has an observation time, not a known producer timestamp. No payload is logged.
"""
import json
import math
from pathlib import Path
import threading
import time
import uuid


class SiotTools:
    MAX_TEXT = 4096
    MAX_TOPICS = 16
    CONNECT_TIMEOUT = 2.0
    SUBSCRIBE_TIMEOUT = 1.0
    READ_TIMEOUT = 2.5
    PUBLISH_TIMEOUT = 2.0

    def __init__(self, config_path='/etc/siot/monitor.json', factory=None, registry=None):
        self._path = config_path
        self._factory = factory
        self._registry = registry
        self._condition = threading.Condition(threading.RLock())
        self._operation = threading.Lock()
        self._client = None
        self._configured = False
        self._connected = False
        self._stopped = False
        self._error = None
        self._topics = {}
        self._pending = {}
        self._early_acks = {}
        self._unsub_results = {}
        self._sequence = 0
        self._saved_publish_client = None

    @staticmethod
    def _topic(value):
        try:
            valid = (isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 128
                     and not value.startswith('$') and not any(c in value for c in '+#')
                     and not any(ord(c) < 32 or ord(c) == 127 for c in value))
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError('主题必须是1至128字节的明确普通主题，不接受通配符或系统主题')
        return value

    @classmethod
    def _message(cls, value):
        try:
            valid = (isinstance(value, str) and bool(value.strip()) and '\x00' not in value
                     and len(value.encode('utf-8')) <= cls.MAX_TEXT)
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError('消息必须是非空UTF-8文本，最多4096字节')
        return value

    def _start(self):
        with self._condition:
            if self._client is not None or self._stopped:
                return
            client = None
            self._configured = False
            try:
                with Path(self._path).open('rb') as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError('config size')
                config = json.loads(raw)
                if not isinstance(config, dict):
                    raise ValueError('config type')
                for key in ('host', 'username', 'password'):
                    if not isinstance(config.get(key), str) or not config[key]:
                        raise ValueError('config field')
                port = config.get('port', 1883)
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError('config port')
                self._configured = True
                if self._factory is None:
                    import paho.mqtt.client as mqtt
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                         client_id='xiaozhi-siot-' + uuid.uuid4().hex[:12])
                else:
                    client = self._factory()
                client.username_pw_set(config['username'], config['password'])
                client.max_queued_messages_set(1)
                client.max_inflight_messages_set(1)
                client.reconnect_delay_set(min_delay=1, max_delay=15)
                client.on_connect = self._on_connect
                client.on_disconnect = self._on_disconnect
                client.on_subscribe = self._on_subscribe
                client.on_unsubscribe = self._on_unsubscribe
                client.on_message = self._on_message
                self._client = client
                self._error = None
                client.connect_async(config['host'], port, keepalive=30)
                client.loop_start()
            except Exception:
                self._client = None
                self._connected = False
                self._error = 'SIoT配置或连接启动不可用'
                if client is not None:
                    threading.Thread(target=self._shutdown, args=(client,), daemon=True).start()

    def _current(self, client):
        return client is self._client and not self._stopped

    def _on_connect(self, client, userdata, flags, reason, properties):
        with self._condition:
            if not self._current(client): return
            self._connected = not reason.is_failure
            self._error = None if self._connected else 'SIoT连接被拒绝'
            self._pending.clear()
            self._early_acks.clear()
            for record in self._topics.values():
                record.update(ready=False, value=None, error=None)
            if self._connected:
                for topic in self._topics: self._request_subscription(topic)
            self._condition.notify_all()

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        with self._condition:
            if not self._current(client): return
            self._connected = False
            self._error = 'SIoT已断开，等待重连'
            for record in self._topics.values():
                record.update(ready=False, value=None, error=None)
            self._pending.clear()
            self._early_acks.clear()
            self._unsub_results.clear()
            if client is self._saved_publish_client:
                # QoS1 Paho reconnect would replay unacknowledged commands.
                # Mark disconnected synchronously inside this callback so its
                # network loop cannot reconnect before the caller sees failure.
                self._client = None
                try: client.disconnect()
                except Exception: pass
                threading.Thread(target=self._shutdown, args=(client,), daemon=True).start()
            self._condition.notify_all()

    def _request_subscription(self, topic):
        # Called with condition held. Paho subscribe only queues a small packet.
        for mid, pending_topic in list(self._pending.items()):
            if pending_topic == topic: del self._pending[mid]
        try:
            rc, mid = self._client.subscribe(topic, qos=0)
            if rc != 0: raise RuntimeError('subscribe rc')
            if mid in self._early_acks:
                self._topics[topic]['ready'] = self._early_acks.pop(mid)
            else:
                self._pending[mid] = topic
        except Exception:
            self._error = 'SIoT主题订阅未成功'

    def _on_subscribe(self, client, userdata, mid, reasons, properties):
        with self._condition:
            if not self._current(client): return
            success = bool(reasons) and all(not reason.is_failure for reason in reasons)
            topic = self._pending.pop(mid, None)
            if topic in self._topics:
                self._topics[topic]['ready'] = success
            else:
                if len(self._early_acks) >= self.MAX_TOPICS: self._early_acks.clear()
                self._early_acks[mid] = success
            if not success: self._error = 'SIoT主题订阅被拒绝'
            self._condition.notify_all()

    def _on_unsubscribe(self, client, userdata, mid, reasons, properties):
        with self._condition:
            if not self._current(client): return
            # MQTT 3 UNSUBACK has no reason-code list; receipt itself is ACK.
            success = all(not reason.is_failure for reason in reasons)
            if len(self._unsub_results) >= self.MAX_TOPICS: self._unsub_results.clear()
            self._unsub_results[mid] = success
            self._condition.notify_all()

    def _on_message(self, client, userdata, message):
        with self._condition:
            if not self._current(client) or message.topic not in self._topics: return
            record = self._topics[message.topic]
            self._sequence += 1
            try:
                if len(message.payload) > self.MAX_TEXT: raise ValueError('size')
                text = message.payload.decode('utf-8')
                if '\x00' in text: raise ValueError('binary')
                record['value'] = dict(message=text, retained=bool(message.retain),
                                       observed_at=time.time(), monotonic=time.monotonic(),
                                       sequence=self._sequence)
                record['error'] = None
            except (ValueError, UnicodeError, AttributeError):
                # A newer invalid payload must not masquerade as the old value.
                record['value'] = None
                record['error'] = '最新消息不是可读取的UTF-8文本或超过4096字节'
            self._condition.notify_all()

    def _ensure_topic(self, topic, deadline):
        self._start()
        with self._condition:
            connect_deadline = min(deadline, time.monotonic() + self.CONNECT_TIMEOUT)
            while not self._connected and self._client is not None and not self._stopped:
                remaining = connect_deadline - time.monotonic()
                if remaining <= 0: break
                self._condition.wait(remaining)
            if self._stopped or not self._connected:
                raise RuntimeError('SIoT未连接，操作未提交')
            if topic not in self._topics:
                if len(self._topics) >= self.MAX_TOPICS:
                    raise RuntimeError('本次服务已订阅16个主题，未新增订阅')
                self._topics[topic] = dict(ready=False, value=None, error=None)
                self._request_subscription(topic)
            elif not self._topics[topic]['ready']:
                # Retry only on a new explicit operation, never spin in a
                # callback or repeatedly resend while the same call waits.
                self._request_subscription(topic)
            subscribe_deadline = min(deadline, time.monotonic() + self.SUBSCRIBE_TIMEOUT)
            while not self._stopped and self._connected and not self._topics[topic]['ready']:
                remaining = subscribe_deadline - time.monotonic()
                if remaining <= 0: break
                self._condition.wait(remaining)
            if self._stopped or not self._connected or not self._topics[topic]['ready']:
                raise RuntimeError('SIoT订阅未确认，操作未提交')

    def get_status(self):
        with self._operation:
            self._start()
            with self._condition:
                deadline = time.monotonic() + self.CONNECT_TIMEOUT
                while self._client is not None and not self._connected and not self._stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: break
                    self._condition.wait(remaining)
                return dict(configured=self._configured, connected=self._connected,
                            local_subscription_count=len(self._topics), max_topics=self.MAX_TOPICS,
                            subscription_scope='仅本MCP客户端已请求监听的主题，不是SIoT服务器主题总数',
                            max_text_bytes=self.MAX_TEXT, history_queried=False, error=self._error)

    def subscribe(self, topic):
        topic = self._topic(topic)
        with self._operation:
            self._ensure_topic(topic, time.monotonic() + self.CONNECT_TIMEOUT + self.SUBSCRIBE_TIMEOUT)
            with self._condition:
                return dict(topic=topic, status='subscribed', ready=self._topics[topic]['ready'],
                            sequence=self._sequence, connected=self._connected)

    def list_subscriptions(self):
        with self._condition:
            topics = [dict(topic=topic, ready=record['ready'], has_message=record['value'] is not None)
                      for topic, record in self._topics.items()]
            return dict(topics=topics, count=len(topics), max_topics=self.MAX_TOPICS,
                        connected=self._connected, scope='local_client_only')

    def invalidate(self, topic):
        topic = self._topic(topic)
        with self._condition:
            if topic in self._topics:
                self._topics[topic].update(value=None, error=None)
            self._condition.notify_all()

    def unsubscribe(self, topic):
        topic = self._topic(topic)
        with self._operation:
            with self._condition:
                if topic not in self._topics:
                    return dict(topic=topic, status='not_subscribed', broker_acknowledged=False)
                # Remove local interest before sending: no late frame may revive
                # this cache, and reconnect must not resubscribe removed topics.
                del self._topics[topic]
                for mid, pending_topic in list(self._pending.items()):
                    if pending_topic == topic: del self._pending[mid]
                client = self._client
                if not self._connected or not self._current(client):
                    return dict(topic=topic, status='local_unsubscribed', broker_acknowledged=False)
                try:
                    rc, mid = client.unsubscribe(topic)
                    if rc != 0: raise RuntimeError('unsubscribe rc')
                    deadline = time.monotonic() + self.SUBSCRIBE_TIMEOUT
                    while mid not in self._unsub_results and self._connected and self._current(client):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0: break
                        self._condition.wait(remaining)
                    confirmed = self._unsub_results.pop(mid, False)
                except Exception:
                    confirmed = False
                if not confirmed:
                    raise RuntimeError('本地监听已停止，但服务器退订尚未确认')
                return dict(topic=topic, status='unsubscribed', broker_acknowledged=True)

    def publish(self, topic, message, save=False, *, cancel_event=None):
        topic = self._topic(topic)
        message = self._message(message)
        if type(save) is not bool: raise ValueError('save必须是布尔值')
        with self._operation:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('会话已取消，消息未提交')
            self._ensure_topic(topic, time.monotonic() + self.CONNECT_TIMEOUT + self.SUBSCRIBE_TIMEOUT)
            if save:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError('会话已取消，消息未提交')
                try:
                    if self._registry is None:
                        from siot_topic_catalog import SiotTopicCatalog
                        self._registry = SiotTopicCatalog(config_path=self._path)
                    self._registry.create_topic(topic, cancel_event=cancel_event)
                except Exception:
                    raise RuntimeError('保存主题登记失败或已取消，消息未提交') from None
            client = self._client
            try:
                with self._condition:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError('cancelled before submission')
                    if not self._current(client) or not self._connected:
                        raise RuntimeError('offline')
                    if save: self._saved_publish_client = client
                    info = client.publish(topic, message, qos=1 if save else 0, retain=False)
                if info.rc != 0: raise RuntimeError('publish rc')
                info.wait_for_publish(timeout=self.PUBLISH_TIMEOUT)
                if not info.is_published(): raise RuntimeError('publish unconfirmed')
            except Exception:
                # Destroy this session to prevent a timed-out socket queue from
                # later flushing on reconnect. Already delivered bytes cannot
                # be recalled, hence explicitly unknown outcome, never retry.
                self._retire(permanent=False)
                raise RuntimeError('SIoT发送未确认，已停止连接；消息可能已送达，请勿自动重试') from None
            finally:
                with self._condition: self._saved_publish_client = None
            return dict(status='broker_ack' if save else 'local_sent', topic=topic, bytes=len(message.encode('utf-8')),
                        qos=1 if save else 0, retained=False, receiver_acknowledged=False,
                        save_requested=save, history_saved=None if save else False)

    def read_latest(self, topic):
        topic = self._topic(topic)
        with self._operation:
            deadline = time.monotonic() + self.READ_TIMEOUT
            with self._condition:
                cached_value = self._topics.get(topic, {}).get('value')
            self._ensure_topic(topic, deadline)
            with self._condition:
                if self._stopped:
                    raise RuntimeError('SIoT连接已停止')
                record = self._topics[topic]
                while record['value'] is None and record['error'] is None and self._connected and not self._stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: break
                    self._condition.wait(remaining)
                if self._stopped:
                    raise RuntimeError('SIoT连接已停止')
                value = record['value']
                base = dict(topic=topic, connected=self._connected, history_queried=False)
                if value is None:
                    return dict(base, available=False, error=record['error'],
                                reason='未观察到可用消息；不会补读首次订阅之前的未保留消息')
                received_source = 'retained' if value['retained'] else 'live'
                return dict(base, available=True, message=value['message'], retained=value['retained'],
                            source='cache' if value is cached_value else received_source, received_source=received_source,
                            observed_at=value['observed_at'], sequence=value['sequence'], producer_timestamp_known=False,
                            age_seconds=round(max(0, time.monotonic()-value['monotonic']), 3))

    def read_after(self, topic, sequence=0, timeout=3, cancel_event=None):
        """Wait for a later local observation, not a producer/device timestamp."""
        topic = self._topic(topic)
        if type(sequence) is not int or sequence < 0: raise ValueError('sequence必须是非负整数')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 10:
            raise ValueError('timeout必须在0至10秒之间')
        if cancel_event is not None and cancel_event.is_set(): raise RuntimeError('会话已取消')
        with self._operation:
            deadline = time.monotonic() + timeout
            self._ensure_topic(topic, deadline)
            with self._condition:
                while True:
                    if cancel_event is not None and cancel_event.is_set(): raise RuntimeError('会话已取消')
                    if self._stopped: raise RuntimeError('SIoT连接已停止')
                    if not self._connected: raise RuntimeError('SIoT连接已断开')
                    record = self._topics[topic]
                    value = record['value']
                    if value is not None and value['sequence'] > sequence:
                        return dict(topic=topic, available=True, message=value['message'],
                                    sequence=value['sequence'], observed_at=value['observed_at'],
                                    retained=value['retained'], source='retained' if value['retained'] else 'live',
                                    connected=True, producer_timestamp_known=False,
                                    age_seconds=round(max(0, time.monotonic()-value['monotonic']), 3))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return dict(topic=topic, available=False, sequence=self._sequence,
                                    connected=True, error=record['error'], reason='没有更新的可读消息')
                    self._condition.wait(min(remaining, .1))

    @staticmethod
    def _shutdown(client):
        try: client.disconnect()
        except Exception: pass
        try: client.loop_stop()
        except Exception: pass

    def stop(self):
        self._retire(permanent=True)

    def _retire(self, *, permanent):
        with self._condition:
            client = self._client
            self._client = None
            self._stopped = self._stopped or permanent
            self._connected = False
            self._topics.clear()
            self._pending.clear()
            self._early_acks.clear()
            self._unsub_results.clear()
            self._condition.notify_all()
        if client is not None: self._shutdown(client)
