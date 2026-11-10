import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load('siot_monitor_control_test', 'apps/siot_monitor/mcp_control.py')
client_module = load('siot_media_control_test', 'xiaozhi/service/siot_media_control.py')


class Session:
    def __init__(self):
        self.mode = None
        self.topic = self.continuous_topic = None
        self.wire_format = 'json'
        self.target_fps = 5
        self.publisher = Mock()
        self.publisher.snapshot.return_value = {'connected': True, 'sent': 2}
        self.actions = []
    def start_camera(self, wire_format, target_fps):
        self.actions.append(('camera', wire_format, target_fps))
        self.mode = 'camera'; self.wire_format = wire_format; self.target_fps = target_fps
    def set_continuous(self, topic, enabled):
        self.actions.append(('continuous', topic, enabled)); self.continuous_topic = topic if enabled else None
    def start_topic(self, topic, cached):
        self.actions.append(('topic', topic, cached)); self.mode = 'topic'; self.topic = topic; self.continuous_topic = None
    def start_url(self, target):
        self.actions.append(('url', target)); self.mode = 'url'; self.topic = self.continuous_topic = None
    def start_lan_sender(self):
        self.actions.append(('lan_sender',)); self.mode = 'lan_sender'; self.topic = self.continuous_topic = None
    def poll(self): return {'status': 'starting', 'frame': None}
    def stop(self):
        self.actions.append(('stop',)); self.mode = None; self.topic = self.continuous_topic = None


def screen_fixture():
    screen = types.SimpleNamespace(page='monitor', media_session=Session(), media_state={},
        model=types.SimpleNamespace(selected_topic=None), media_send_topic=types.SimpleNamespace(value=''),
        last_media_payload=None, notice='', delete_target=None)
    def stop():
        screen.media_session.stop(); screen.media_state = {}
    screen.stop_media = stop
    return screen


class MediaControlTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen_fixture()
        self.network = Mock()
        self.client = client_module.SiotMediaControl()

    def test_start_applies_exact_format_rate_topic_without_claiming_live(self):
        result = control.apply_request(self.screen, self.network,
            dict(action='start', topic='siot/camera', wire_format='base64', fps=5))
        self.assertEqual(self.screen.media_session.actions, [('camera','base64',5),('continuous','siot/camera',True)])
        self.assertEqual(self.screen.page, 'media_view')
        self.assertTrue(result['applied'])
        self.assertEqual(result['state'], 'starting')
        self.assertFalse(result['frame_available'])

    def test_topic_show_updates_selected_full_payload_route(self):
        control.apply_request(self.screen, self.network,dict(action='show',source='topic',target='siot/image'))
        self.network.set_media_topic.assert_called_once_with('siot/image')
        self.assertEqual(self.screen.model.selected_topic, 'siot/image')
        self.assertEqual(self.screen.media_session.actions, [('topic','siot/image',None)])

    def test_url_credentials_not_in_response(self):
        value = 'rtsp://name:private-password@192.168.1.2/live'
        result = control.apply_request(self.screen,self.network,dict(action='show',source='url',target=value))
        self.assertEqual(self.screen.media_session.actions,[('url',value)])
        self.assertNotIn('private-password',json.dumps(result))
        self.assertNotIn('192.168',json.dumps(result))

    def test_lan_sender_and_stop(self):
        control.apply_request(self.screen,self.network,dict(action='show',source='lan_sender',target=''))
        self.assertEqual(self.screen.media_session.mode,'lan_sender')
        result = control.apply_request(self.screen,self.network,dict(action='stop'))
        self.assertEqual(self.screen.page,'monitor')
        self.assertIsNone(result['mode'])

    def test_editor_and_confirmation_are_not_discarded(self):
        for page in ('edit','create','search','media_url','media_send_topic','delete','clear_data'):
            self.screen.page = page
            with self.subTest(page=page),self.assertRaises(RuntimeError):
                control.apply_request(self.screen,self.network,dict(action='start',topic='siot/camera',wire_format='json',fps=2))
        self.assertEqual(self.screen.media_session.actions,[])

    def test_stop_preserves_unrelated_editor(self):
        self.screen.page='edit'
        control.apply_request(self.screen,self.network,dict(action='stop'))
        self.assertEqual(self.screen.page,'edit')

    def test_get_status_does_not_poll_or_change_media(self):
        result = control.apply_request(self.screen,self.network,dict(action='get_status'))
        self.assertEqual(self.screen.media_session.actions,[])
        self.assertTrue(result['monitor_open'])

    def test_receive_mode_reports_monitor_not_publisher_connection(self):
        self.screen.media_session.mode='topic'
        self.screen.media_session.publisher.snapshot.return_value={'connected':False}
        self.network.connected=True
        result=control.apply_request(self.screen,self.network,dict(action='get_status'))
        self.assertTrue(result['mqtt_connected'])
        self.assertFalse(result['publisher_connected'])

    def test_unknown_monitor_connection_is_not_fabricated_false(self):
        result=control.apply_request(self.screen,None,dict(action='get_status'))
        self.assertIsNone(result['mqtt_connected'])

    def test_invalid_requests_rejected_before_changes(self):
        bad = [dict(action='start',topic='a/#',wire_format='json',fps=5),
            dict(action='start',topic='a',wire_format='jpeg',fps=5),
            dict(action='start',topic='a',wire_format='json',fps=True),
            dict(action='show',source='url',target='file:///etc/passwd'),
            dict(action='show',source='url',target='http://server:bad/'),
            dict(action='get_status',command='shell'),dict(action='show',source='lan_sender',target='http://x')]
        for request in bad:
            with self.subTest(request=request),self.assertRaises(ValueError):
                control.apply_request(self.screen,self.network,request)
        self.assertEqual(self.screen.media_session.actions,[])

    def test_expired_queue_item_never_starts_camera(self):
        server = control.MediaControlServer()
        pending = control.PendingRequest(dict(action='start',topic='a',wire_format='json',fps=2),time.monotonic()-1)
        server.requests.put(pending)
        self.assertFalse(server.drain(self.screen,self.network))
        self.assertFalse(pending.response['ok'])
        self.assertEqual(self.screen.media_session.actions,[])

    def test_cancelled_queue_item_never_applies(self):
        server = control.MediaControlServer()
        pending = control.PendingRequest(dict(action='start',topic='a',wire_format='json',fps=2),time.monotonic()+5)
        pending.cancelled.set();server.requests.put(pending)
        server.drain(self.screen,self.network)
        self.assertEqual(self.screen.media_session.actions,[])

    def test_timeout_during_native_start_stops_late_camera(self):
        server = control.MediaControlServer()
        pending = control.PendingRequest(dict(action='start',topic='a',wire_format='json',fps=2),time.monotonic()+5)
        original = self.screen.media_session.start_camera
        def delayed(*args,**kwargs):
            original(*args,**kwargs);pending.cancelled.set()
        self.screen.media_session.start_camera = delayed
        server.requests.put(pending);server.drain(self.screen,self.network)
        self.assertFalse(pending.response['ok'])
        self.assertIsNone(self.screen.media_session.mode)
        self.assertEqual(self.screen.page,'monitor')

    def test_client_validation_no_socket_for_bad_arguments(self):
        with patch.object(self.client,'_request') as request:
            for fps in (True,0,30):
                with self.assertRaises(ValueError):self.client.start('a',fps=fps)
            with self.assertRaises(ValueError):self.client.start('a/#')
            with self.assertRaises(ValueError):self.client.show('url','file:///etc/passwd')
            with self.assertRaises(ValueError):self.client.show('lan_sender','bad')
        request.assert_not_called()

    def test_client_closed_monitor_has_actionable_error(self):
        with patch.object(self.client,'_check_endpoint',side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError,'SIoT监控'):self.client.start('siot/camera')
            result=self.client.get_status()
        self.assertEqual(result['state'],'unavailable')
        self.assertIsNone(result['monitor_open'])

    def test_precancelled_client_does_not_connect(self):
        event=threading.Event();event.set()
        with patch.object(self.client,'_check_endpoint') as endpoint:
            with self.assertRaises(RuntimeError):self.client.stop(cancel_event=event)
        endpoint.assert_not_called()


@unittest.skipUnless(hasattr(os,'getuid') and hasattr(socket,'AF_UNIX'),'Unix socket security requires POSIX')
class UnixMediaControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'private'/'control.sock'
        self.server=control.MediaControlServer(self.path,timeout=.3)
        self.server.start();self.addCleanup(self.server.close)
        self.client=client_module.SiotMediaControl(self.path);self.client.TIMEOUT=1
        self.screen=screen_fixture();self.network=Mock()

    def test_real_socket_applied_reply_and_cleanup(self):
        result=[]
        worker=threading.Thread(target=lambda:result.append(self.client.start('siot/camera',fps=2)))
        worker.start()
        until=time.monotonic()+1
        while worker.is_alive() and time.monotonic()<until:
            self.server.drain(self.screen,self.network);time.sleep(.01)
        worker.join(1)
        self.assertFalse(worker.is_alive());self.assertTrue(result[0]['applied'])
        self.assertEqual(os.stat(self.path).st_mode&0o777,0o600)
        self.server.close();self.assertFalse(self.path.exists())

    def test_real_timeout_cannot_apply_late(self):
        with self.assertRaises(RuntimeError):self.client.start('siot/camera')
        self.server.drain(self.screen,self.network)
        self.assertEqual(self.screen.media_session.actions,[])

    def _queue_raw_start(self):
        client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        client.connect(str(self.path))
        client.sendall(json.dumps(dict(action='start',topic='siot/camera',wire_format='base64',fps=5)).encode()+b'\n')
        # Synchronize on actual queue admission, not a timing sleep. Put back
        # before closing so UI drain sees the real server PendingRequest.
        pending=self.server.requests.get(timeout=1)
        self.server.requests.put_nowait(pending)
        return client,pending

    def test_immediate_peer_close_before_drain_never_starts_camera(self):
        client,pending=self._queue_raw_start()
        client.close()
        self.server.drain(self.screen,self.network)
        self.assertEqual(self.screen.media_session.actions,[])
        self.assertFalse(pending.response['ok'])

    def test_peer_close_during_native_start_stops_media(self):
        client,pending=self._queue_raw_start()
        original=self.screen.media_session.start_camera
        def disconnect(*args,**kwargs):
            original(*args,**kwargs)
            client.close()
        self.screen.media_session.start_camera=disconnect
        self.server.drain(self.screen,self.network)
        self.assertIsNone(self.screen.media_session.mode)
        self.assertEqual(self.screen.media_session.actions[-1],('stop',))
        self.assertFalse(pending.response['ok'])

    def test_unsafe_socket_symlink_is_not_removed(self):
        self.server.close()
        target=Path(self.temp.name)/'important';target.write_text('untouched')
        self.path.symlink_to(target)
        other=control.MediaControlServer(self.path)
        with self.assertRaises(RuntimeError):other.start()
        self.assertEqual(target.read_text(),'untouched')
        self.path.unlink()

    def test_second_server_never_replaces_active_socket(self):
        before=self.path.stat().st_ino
        with self.assertRaises(RuntimeError):control.MediaControlServer(self.path).start()
        self.assertEqual(self.path.stat().st_ino,before)
