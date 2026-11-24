import sys
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'xiaozhi/service'))
from siot_api import SiotApi


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.mqtt = Mock()
        self.catalog = Mock()
        self.media = Mock()
        self.devices = Mock()
        self.now = 10
        self.api = SiotApi(self.mqtt, self.catalog, self.media, self.devices, clock=lambda: self.now)
        # Mock is callable; provide an explicit getter for the injected client.
        self.api._mqtt = lambda: self.mqtt

    def test_auto_uses_live_then_labels_history(self):
        self.mqtt.read_latest.return_value = {'available': True, 'source': 'cache', 'message': 'new'}
        self.assertEqual(self.api.read_latest('u/t')['source'], 'cache')
        self.catalog.get_history.assert_not_called()
        self.mqtt.read_latest.return_value = {'available': False}
        self.catalog.get_history.return_value = {'messages': [{'message': 'old', 'timestamp': 1, 'source': 'history'}]}
        result = self.api.read_latest('u/t')
        self.assertEqual(result['message'], 'old')
        self.assertTrue(result['history_queried'])
        self.assertFalse(result['current_physical_state_verified'])

    def test_invalid_new_data_not_masked_by_history(self):
        self.mqtt.read_latest.return_value = {'available': False, 'error': 'binary'}
        self.assertEqual(self.api.read_latest('u/t')['error'], 'binary')
        self.catalog.get_history.assert_not_called()

    def test_explicit_history_does_not_subscribe(self):
        self.catalog.get_history.return_value = {'messages': []}
        self.assertFalse(self.api.read_latest('u/t', 'history')['available'])
        self.mqtt.read_latest.assert_not_called()

    def test_destructive_change_requires_same_scope_and_exact_action_topic(self):
        self.catalog.prepare_change.return_value = {'confirmation_token': 'token', 'expires_in_seconds': 120}
        prepared = self.api.change('delete', 'u/t', scope='A')
        self.assertEqual(prepared['expires_in_seconds'], 60)
        self.catalog.confirm_change.assert_not_called()
        with self.assertRaises(ValueError):
            self.api.change('delete', 'u/t', 'token', scope='B')
        self.catalog.confirm_change.assert_not_called()
        self.api.change('delete', 'u/t', scope='A')
        with self.assertRaises(ValueError):
            self.api.change('clear', 'u/t', 'token', scope='A')
        self.catalog.confirm_change.assert_not_called()

    def test_confirmation_single_use_expiry_and_cancellation(self):
        self.catalog.prepare_change.return_value = {'confirmation_token': 'token'}
        self.catalog.confirm_change.return_value = {'status': 'deleted'}
        self.api.change('delete', 'u/t', scope='A')
        self.api.change('delete', 'u/t', 'token', scope='A')
        self.catalog.confirm_change.assert_called_once()
        with self.assertRaises(ValueError):self.api.change('delete', 'u/t', 'token', scope='A')
        self.api.change('clear', 'u/t', scope='A')
        self.now += 61
        with self.assertRaises(ValueError):self.api.change('clear', 'u/t', 'token', scope='A')
        cancelled = threading.Event();cancelled.set()
        with self.assertRaises(RuntimeError):self.api.call('publish', {}, cancel_event=cancelled)
        self.mqtt.publish.assert_not_called()

    def test_media_and_device_delegation_preserve_cancel_flag(self):
        event = threading.Event()
        self.api.call('media.start', {'topic': 'u/t'}, cancel_event=event)
        self.media.start.assert_called_once_with(topic='u/t', cancel_event=event)
        self.api.call('device.command', {'device': 'lamp', 'action': 'set'}, cancel_event=event)
        self.devices.command.assert_called_once_with(device='lamp', action='set', cancel_event=event)

    def test_clear_preserves_subscription_and_invalidates_before_rest(self):
        order = []
        self.catalog.prepare_change.return_value = {'confirmation_token': 'clear'}
        self.catalog.confirm_change.side_effect = lambda *a, **kw: order.append('rest') or {'verified': True}
        self.mqtt.invalidate.side_effect = lambda topic: order.append('invalidate')
        self.api.change('clear', 'u/t', scope='A')
        self.api.change('clear', 'u/t', 'clear', scope='A')
        self.assertEqual(order, ['invalidate', 'rest'])
        self.mqtt.unsubscribe.assert_not_called()

    def test_catalog_failure_is_unknown_not_zero(self):
        self.mqtt.get_status.return_value = {'local_subscription_count': 0}
        self.catalog.list_topics.side_effect = RuntimeError('offline')
        self.assertIsNone(self.api.get_status()['server_topics_total'])
