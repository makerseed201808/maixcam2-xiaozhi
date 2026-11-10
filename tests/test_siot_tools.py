import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('siot_tools', ROOT / 'xiaozhi/service/siot_tools.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Client:
    def __init__(self):
        self.connected = True
        self.ack = True
        self.sent = True
        self.rc = 0
        self.mid = 0
        self.subscriptions = []
        self.publications = []
        self.closed = False
        self.unsubscriptions = []
        self.unsub_ack = True
    def username_pw_set(self, *args): pass
    def reconnect_delay_set(self, **kwargs): pass
    def max_queued_messages_set(self, value): pass
    def max_inflight_messages_set(self, value): pass
    def connect_async(self, *args, **kwargs): pass
    def loop_start(self):
        self.on_connect(self, None, None, types.SimpleNamespace(is_failure=not self.connected), None)
    def loop_stop(self): pass
    def disconnect(self): self.closed = True
    def subscribe(self, topic, qos):
        self.mid += 1
        self.subscriptions.append(topic)
        if self.ack:
            self.on_subscribe(self, None, self.mid, [types.SimpleNamespace(is_failure=False)], None)
        return 0, self.mid
    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        if self.rc == 0: self.deliver(topic, payload.encode('utf-8'))
        return types.SimpleNamespace(rc=self.rc, wait_for_publish=lambda timeout: None, is_published=lambda: self.sent)
    def unsubscribe(self, topic):
        self.mid += 1
        self.unsubscriptions.append(topic)
        if self.unsub_ack: self.on_unsubscribe(self, None, self.mid, [], None)
        return 0, self.mid
    def deliver(self, topic, payload, retain=False):
        self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=payload, retain=retain))


class SiotToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'monitor.json'
        self.path.write_text(json.dumps(dict(host='localhost', username='fixture', password='secret-test')), encoding='utf-8')
        self.client = Client()
        self.tools = module.SiotTools(str(self.path), factory=lambda: self.client)
        self.tools.CONNECT_TIMEOUT = .01
        self.tools.SUBSCRIBE_TIMEOUT = .01
        self.tools.READ_TIMEOUT = .01
        self.addCleanup(self.tools.stop)

    def test_publish_and_read_back_with_exact_topic(self):
        result = self.tools.publish('fixture/test', '测试 123')
        self.assertEqual(result['status'], 'local_sent')
        self.assertFalse(result['receiver_acknowledged'])
        self.assertEqual(self.client.publications, [('fixture/test', '测试 123', 0, False)])
        value = self.tools.read_latest('fixture/test')
        self.assertTrue(value['available'])
        self.assertEqual(value['message'], '测试 123')
        self.assertEqual(value['source'], 'cache')
        self.assertGreaterEqual(value['age_seconds'], 0)
        self.assertEqual(self.client.subscriptions, ['fixture/test'])

    def test_invalid_input_has_no_network_side_effect(self):
        for topic in ['', 'x/#', 'x/+', 'x\0y', '$SYS/foo', 'x' * 129, '\ud800']:
            with self.subTest(topic=repr(topic)):
                with self.assertRaises(ValueError): self.tools.publish(topic, 'test')
        for payload in ['', ' ', '测' * 1366, '\ud800', 123, 'x\0y']:
            with self.assertRaises(ValueError): self.tools.publish('fixture/test', payload)
        self.assertEqual(self.client.subscriptions, [])
        self.assertEqual(self.client.publications, [])

    def test_missing_config_status_is_safe(self):
        self.path.unlink()
        status = self.tools.get_status()
        self.assertFalse(status['configured'])
        self.assertFalse(status['connected'])
        self.assertNotIn('secret', json.dumps(status))

    def test_connection_exception_is_sanitized(self):
        def broken(): raise RuntimeError('secret-test private hostname')
        self.tools._factory = broken
        status = self.tools.get_status()
        self.assertNotIn('secret-test', json.dumps(status))
        self.assertNotIn('private hostname', json.dumps(status))
        self.assertFalse(status['connected'])

    def test_first_read_marks_retained_observation_not_production_time(self):
        original = self.client.subscribe
        def retained_subscription(topic, qos):
            result = original(topic, qos)
            self.client.deliver(topic, b'saved', True)
            return result
        self.client.subscribe = retained_subscription
        value = self.tools.read_latest('fixture/retained')
        self.assertEqual(value['source'], 'retained')
        self.assertFalse(value['producer_timestamp_known'])
        self.assertGreater(value['observed_at'], 0)

    def test_no_message_is_explicit_not_fabricated_history(self):
        value = self.tools.read_latest('fixture/new')
        self.assertFalse(value['available'])
        self.assertNotIn('message', value)
        self.assertFalse(value['history_queried'])

    def test_retained_and_invalid_media(self):
        self.tools.read_latest('fixture/test')
        self.client.deliver('fixture/test', b'old-value', True)
        result = self.tools.read_latest('fixture/test')
        self.assertTrue(result['retained'])
        self.assertEqual(result['received_source'], 'retained')
        for invalid in [b'\xff\xd8', b'x' * 4097]:
            self.client.deliver('fixture/test', invalid)
            result = self.tools.read_latest('fixture/test')
            self.assertFalse(result['available'])
            self.assertIn('error', result)
        self.client.deliver('fixture/unsubscribed', b'do not retain')
        self.assertEqual(self.tools.get_status()['local_subscription_count'], 1)

    def test_bounded_subscription_count(self):
        for i in range(16): self.tools.read_latest('fixture/' + str(i))
        with self.assertRaises(RuntimeError): self.tools.read_latest('fixture/17')
        self.assertEqual(len(self.client.subscriptions), 16)

    def test_disconnected_does_not_send(self):
        self.client.connected = False
        with self.assertRaises(RuntimeError): self.tools.publish('fixture/test', 'test')
        self.assertEqual(self.client.publications, [])

    def test_suback_required_before_publish(self):
        self.client.ack = False
        with self.assertRaises(RuntimeError): self.tools.publish('fixture/test', 'test')
        self.assertEqual(self.client.publications, [])

    def test_unconfirmed_publish_stops_client_and_never_retries(self):
        self.client.sent = False
        with self.assertRaises(RuntimeError): self.tools.publish('fixture/test', 'test')
        self.assertTrue(self.client.closed)
        self.assertEqual(len(self.client.publications), 1)
        replacement = Client()
        self.tools._factory = lambda: replacement
        self.assertEqual(self.tools.publish('fixture/next', 'new request')['status'], 'local_sent')
        self.assertEqual(replacement.publications, [('fixture/next', 'new request', 0, False)])

    def test_subscription_timeout_can_retry_on_next_explicit_call(self):
        self.client.ack = False
        with self.assertRaises(RuntimeError): self.tools.publish('fixture/test', 'test')
        self.client.ack = True
        self.assertEqual(self.tools.publish('fixture/test', 'test')['status'], 'local_sent')
        self.assertEqual(len(self.client.publications), 1)

    def test_stop_ignores_late_callbacks_and_prevents_restart(self):
        self.tools.publish('fixture/test', 'test')
        self.tools.stop()
        self.client.deliver('fixture/test', b'late')
        self.client.loop_start()
        self.assertFalse(self.tools.get_status()['connected'])
        with self.assertRaises(RuntimeError): self.tools.read_latest('fixture/test')

    def test_reconnect_resubscribes_only_explicit_topics(self):
        self.tools.publish('fixture/test', 'test')
        self.client.on_disconnect(self.client, None, None, None, None)
        self.client.loop_start()
        self.assertEqual(self.client.subscriptions, ['fixture/test', 'fixture/test'])
        self.assertTrue(self.tools.get_status()['connected'])
        self.assertFalse(self.tools.read_latest('fixture/test')['available'])

    def test_stop_interrupts_subscription_wait_without_key_error(self):
        self.client.ack = False
        self.tools.SUBSCRIBE_TIMEOUT = 1
        self.tools.READ_TIMEOUT = 1
        errors = []
        def read():
            try: self.tools.read_latest('fixture/test')
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=read)
        worker.start()
        deadline = time.monotonic() + 1
        while not self.client.subscriptions and time.monotonic() < deadline: time.sleep(.001)
        self.tools.stop()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_explicit_subscriptions_release_slots_and_cache(self):
        result = self.tools.subscribe('fixture/one')
        self.assertTrue(result['ready'])
        self.client.deliver('fixture/one', b'one')
        listing = self.tools.list_subscriptions()
        self.assertEqual([item['topic'] for item in listing['topics']], ['fixture/one'])
        result = self.tools.unsubscribe('fixture/one')
        self.assertTrue(result['broker_acknowledged'])
        self.assertEqual(self.client.unsubscriptions, ['fixture/one'])
        self.client.deliver('fixture/one', b'late')
        self.assertEqual(self.tools.list_subscriptions()['topics'], [])
        self.assertFalse(self.tools.unsubscribe('fixture/absent')['broker_acknowledged'])
        self.assertEqual(self.client.unsubscriptions, ['fixture/one'])

    def test_unsubscribe_timeout_is_honest_and_ignores_late_messages(self):
        self.tools.subscribe('fixture/one')
        self.client.unsub_ack = False
        with self.assertRaises(RuntimeError): self.tools.unsubscribe('fixture/one')
        self.client.deliver('fixture/one', b'late')
        self.assertEqual(self.tools.list_subscriptions()['topics'], [])

    def test_saved_publish_registers_before_qos1_and_does_not_claim_persistence(self):
        calls=[]
        class Registry:
            def create_topic(self, topic, cancel_event=None): calls.append(topic)
        self.tools._registry=Registry()
        result=self.tools.publish('fixture/saved','hello',save=True)
        self.assertEqual(calls,['fixture/saved'])
        self.assertEqual(self.client.publications,[('fixture/saved','hello',1,False)])
        self.assertEqual(result['status'],'broker_ack')
        self.assertTrue(result['save_requested'])
        self.assertIsNone(result['history_saved'])
        self.assertFalse(result['receiver_acknowledged'])

    def test_registration_failure_and_cancellation_do_not_publish(self):
        event=threading.Event()
        class Registry:
            def create_topic(self, topic, cancel_event=None): cancel_event.set()
        self.tools._registry=Registry()
        with self.assertRaises(RuntimeError):self.tools.publish('fixture/saved','hello',save=True,cancel_event=event)
        self.assertEqual(self.client.publications,[])
        class Broken:
            def create_topic(self, topic, cancel_event=None):raise RuntimeError('secret')
        self.tools._registry=Broken()
        with self.assertRaises(RuntimeError) as caught:self.tools.publish('fixture/saved','hello',save=True)
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(self.client.publications,[])

    def test_read_after_uses_sequence_and_ignores_existing_value(self):
        self.tools.subscribe('fixture/reply')
        self.client.deliver('fixture/reply',b'old')
        old=self.tools.read_latest('fixture/reply')
        self.assertGreater(old['sequence'],0)
        missing=self.tools.read_after('fixture/reply',sequence=old['sequence'],timeout=.01)
        self.assertFalse(missing['available'])
        self.client.deliver('fixture/reply',b'new')
        new=self.tools.read_after('fixture/reply',sequence=old['sequence'],timeout=.01)
        self.assertEqual(new['message'],'new')
        self.assertGreater(new['sequence'],old['sequence'])

    def test_cancelled_read_after_returns_without_waiting(self):
        event=threading.Event();event.set()
        with self.assertRaises(RuntimeError):self.tools.read_after('fixture/reply',timeout=1,cancel_event=event)
        self.assertEqual(self.client.subscriptions,[])

    def test_sequence_never_resets_on_disconnect(self):
        self.tools.subscribe('fixture/reply')
        self.client.deliver('fixture/reply',b'old')
        old=self.tools.read_latest('fixture/reply')['sequence']
        self.client.on_disconnect(self.client,None,None,None,None)
        self.client.loop_start()
        self.client.deliver('fixture/reply',b'new')
        self.assertGreater(self.tools.read_latest('fixture/reply')['sequence'],old)

    def test_invalidate_drops_old_value_without_removing_subscription(self):
        self.tools.subscribe('fixture/topic')
        self.client.deliver('fixture/topic',b'old')
        self.tools.invalidate('fixture/topic')
        self.assertEqual(self.tools.list_subscriptions()['count'],1)
        self.assertFalse(self.tools.read_latest('fixture/topic')['available'])

    def test_saved_publish_disconnect_retires_client_and_cannot_replay(self):
        class Registry:
            def create_topic(self,topic,cancel_event=None):pass
        self.tools._registry=Registry()
        original=self.client.publish
        def disconnected_publish(*args,**kwargs):
            self.client.sent=False
            info=original(*args,**kwargs)
            self.client.on_disconnect(self.client,None,None,None,None)
            return info
        self.client.publish=disconnected_publish
        with self.assertRaises(RuntimeError):self.tools.publish('fixture/saved','one',save=True)
        self.assertTrue(self.client.closed)
        self.assertEqual(self.client.publications,[('fixture/saved','one',1,False)])
        self.client.loop_start()  # Late reconnect callback from old object ignored.
        self.assertIsNone(self.tools._client)
        self.assertFalse(self.tools._connected)
        replacement=Client();self.tools._factory=lambda:replacement
        self.tools.publish('fixture/next','two')
        self.assertEqual(replacement.publications,[('fixture/next','two',0,False)])

    def test_unsubscribe_not_reinstalled_after_reconnect(self):
        self.tools.subscribe('fixture/one');self.tools.subscribe('fixture/two')
        self.tools.unsubscribe('fixture/one')
        self.client.on_disconnect(self.client,None,None,None,None)
        self.client.loop_start()
        self.assertEqual(self.client.subscriptions,['fixture/one','fixture/two','fixture/two'])

    def test_read_after_cancel_interrupts_wait(self):
        self.tools.subscribe('fixture/reply')
        event=threading.Event();errors=[]
        def wait():
            try:self.tools.read_after('fixture/reply',timeout=5,cancel_event=event)
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=wait);worker.start()
        time.sleep(.02);event.set();worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],RuntimeError)


if __name__ == '__main__': unittest.main()
