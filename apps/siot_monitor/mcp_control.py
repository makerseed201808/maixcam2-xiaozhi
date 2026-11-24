"""Private local media requests, applied only by the monitor's UI thread.

No Maix imports: pure validation, queueing and screen coordination are testable
on a host. The socket is not a network API and never returns frames or URLs.
"""
import errno
import json
import os
from pathlib import Path
import queue
import select
import socket
import stat
import struct
import threading
import time
from urllib.parse import urlsplit

DEFAULT_SOCKET = '/run/siot-monitor-mcp/control.sock'
MAX_REQUEST = 4096
MAX_RESPONSE = 8192


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('Invalid media request')
    action = request.get('action')
    allowed = {'get_status': {'action'}, 'stop': {'action'},
               'start': {'action', 'topic', 'wire_format', 'fps'},
               'show': {'action', 'source', 'target'}}
    if action not in allowed or set(request) - allowed[action]:
        raise ValueError('Invalid media action')
    result = dict(request)
    if action == 'start':
        validate_topic(request.get('topic'))
        if request.get('wire_format') not in ('json', 'base64'):
            raise ValueError('Invalid media format')
        if type(request.get('fps')) is not int or request['fps'] not in (2, 5, 10):
            raise ValueError('Invalid media frame rate')
    if action == 'show':
        source, target = request.get('source'), request.get('target')
        if source == 'topic':
            validate_topic(target)
        elif source == 'url':
            if not isinstance(target, str) or not 0 < len(target.encode('utf-8')) <= 512:
                raise ValueError('Invalid media URL')
            if any(ord(c) < 33 or ord(c) == 127 for c in target):
                raise ValueError('Invalid media URL')
            parsed = urlsplit(target)
            if parsed.scheme not in ('http', 'https', 'rtsp') or not parsed.hostname:
                raise ValueError('Invalid media URL')
            if parsed.fragment:
                raise ValueError('Invalid media URL')
            _ = parsed.port
        elif source == 'lan_sender':
            if target != '':
                raise ValueError('LAN sender does not accept a target')
        else:
            raise ValueError('Invalid media source')
    return result


def validate_topic(topic):
    if (not isinstance(topic, str) or not 0 < len(topic.encode('utf-8')) <= 128
            or topic.startswith('$') or any(c in topic for c in '+#')
            or any(ord(c) < 32 or ord(c) == 127 for c in topic)):
        raise ValueError('Invalid media topic')


def safe_status(screen, network=None):
    session = screen.media_session
    state = screen.media_state or {}
    publisher = session.publisher.snapshot() if session else {}
    result = {'monitor_open': True, 'mode': session.mode if session else None,
              'state': str(state.get('status', 'stopped'))[:32],
              'sending': bool(session and session.continuous_topic),
              'topic': session.continuous_topic if session and session.continuous_topic else
                       (session.topic if session and session.mode == 'topic' else None),
              'wire_format': session.wire_format if session else None,
              'target_fps': session.target_fps if session else None,
              'mqtt_connected': getattr(network, 'connected', None) if
                                type(getattr(network, 'connected', None)) is bool else None,
              'publisher_connected': bool(publisher.get('connected', False)),
              'frame_available': bool(state.get('frame') is not None),
              'error': '媒体来源暂不可用，请查看设备屏幕' if state.get('error') else None}
    for name in ('sent', 'dropped'):
        value = publisher.get(name, 0)
        result[name] = value if type(value) is int and value >= 0 else 0
    return result


def apply_request(screen, network, request):
    """Must run on the UI thread, never on the socket worker."""
    request = validate_request(request)
    action = request['action']
    if action == 'get_status':
        return safe_status(screen, network)
    if action == 'stop':
        screen.stop_media()
        if screen.page.startswith('media_'):
            screen.page = 'monitor'
        screen.notice = '语音请求：媒体已停止'
        return dict(safe_status(screen, network), applied=True)
    if screen.page in ('edit', 'create', 'search', 'delete', 'clear_data',
                       'media_url', 'media_send_topic') or getattr(screen, 'delete_target', None):
        raise RuntimeError('请先完成或取消屏幕上的编辑/确认，再控制媒体')
    session = screen.media_session
    if session is None or network is None:
        raise RuntimeError('媒体连接尚未准备好')
    if action == 'start':
        session.start_camera(request['wire_format'], target_fps=request['fps'])
        try:
            session.set_continuous(request['topic'], True)
        except Exception:
            session.stop()
            raise
        screen.media_send_topic.value = request['topic']
        screen.media_send_topic.cursor = len(request['topic'])
        screen.send_fps = request['fps']
    elif request['source'] == 'topic':
        topic = request['target']
        # The main loop routes full image payloads only for selected_topic.
        network.set_media_topic(topic)
        screen.model.selected_topic = topic
        cached = screen.last_media_payload
        session.start_topic(topic, cached if cached and cached[0] == topic else None)
    elif request['source'] == 'url':
        session.start_url(request['target'])
    else:
        session.start_lan_sender()
    screen.page = 'media_view'
    screen.media_state = session.poll()
    screen.notice = '语音媒体请求已应用；画面就绪状态见屏幕'
    return dict(safe_status(screen, network), applied=True)


class PendingRequest:
    def __init__(self, request, deadline, peer_alive=None):
        self.request = request
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.response = None
        self.peer_alive = peer_alive if peer_alive is not None else lambda: True

    def is_live(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            return False
        try:
            return self.peer_alive()
        except (OSError, ValueError):
            return False


class MediaControlServer:
    def __init__(self, path=DEFAULT_SOCKET, timeout=5.0):
        self.path = Path(path)
        self.timeout = timeout
        self.requests = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._socket = None
        self._thread = None
        self._identity = None
        self._clients = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)

    @staticmethod
    def _safe_directory(path):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Unsafe media control directory')

    def start(self):
        if self._thread is not None:
            raise RuntimeError('Media control already started')
        if not hasattr(socket, 'AF_UNIX') or not hasattr(os, 'getuid'):
            raise RuntimeError('Local media control requires Unix sockets')
        self._safe_directory(self.path.parent)
        if os.path.lexists(self.path):
            info = os.lstat(self.path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Unsafe media control socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.1)
                try:
                    probe.connect(str(self.path))
                except OSError as exc:
                    if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                        raise RuntimeError('Media control socket is busy') from None
                else:
                    raise RuntimeError('Media control already active')
            current = os.lstat(self.path)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError('Media control socket changed')
            os.unlink(self.path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            info = os.lstat(self.path)
            self._identity = (info.st_dev, info.st_ino)
            server.listen(4)
            server.settimeout(.2)
        except Exception:
            server.close()
            raise
        self._socket = server
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @staticmethod
    def _peer_allowed(client):
        if hasattr(socket, 'SO_PEERCRED'):
            _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid == os.getuid()
        return True  # Private directory and socket permissions still required.

    @staticmethod
    def _peer_alive(client):
        # Called from both the socket worker and the UI admission boundary.
        # MSG_PEEK never consumes data, so two checks cannot steal one another's
        # EOF notification. No sleep/window is allowed before an operation.
        try:
            readable, _, _ = select.select([client], [], [], 0)
            if not readable:
                return True
            return bool(client.recv(1, socket.MSG_PEEK | getattr(socket, 'MSG_DONTWAIT', 0)))
        except (OSError, ValueError):
            return False

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            with self._lock:
                self._clients.add(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        pending = None
        try:
            if not self._peer_allowed(client):
                return
            client.settimeout(1.0)
            read_deadline = time.monotonic() + 1.0
            raw = bytearray()
            while b'\n' not in raw:
                remaining = read_deadline - time.monotonic()
                if remaining <= 0:
                    return
                client.settimeout(remaining)
                block = client.recv(min(1024, MAX_REQUEST + 1 - len(raw)))
                if not block:
                    return
                raw.extend(block)
                if len(raw) > MAX_REQUEST:
                    raise ValueError('Request too large')
            request = validate_request(json.loads(raw.decode('utf-8')))
            client.settimeout(1.0)
            pending = PendingRequest(request, time.monotonic() + self.timeout,
                                     peer_alive=lambda: self._peer_alive(client))
            self.requests.put_nowait(pending)
            while not pending.done.wait(.02):
                if self._stop.is_set() or time.monotonic() >= pending.deadline:
                    pending.cancelled.set()
                    break
                if not self._peer_alive(client):
                    pending.cancelled.set()
                    return
            response = pending.response if pending.done.is_set() else {
                'ok': False, 'error': '媒体请求超时，未确认应用；请查询状态后再操作'}
            wire = json.dumps(response, ensure_ascii=False).encode('utf-8') + b'\n'
            if len(wire) > MAX_RESPONSE:
                raise ValueError('Response too large')
            client.sendall(wire)
        except (ValueError, UnicodeError, queue.Full):
            try: client.sendall(b'{"ok":false,"error":"Invalid or busy media request"}\n')
            except OSError: pass
        except OSError:
            pass
        finally:
            if pending is not None and not pending.done.is_set():
                pending.cancelled.set()
            client.close()
            with self._lock:
                self._clients.discard(client)
            self._slots.release()

    def drain(self, screen, network):
        """Apply at most one request per UI iteration; return whether UI changed."""
        try:
            pending = self.requests.get_nowait()
        except queue.Empty:
            return False
        if not pending.is_live() or self._stop.is_set():
            pending.response = {'ok': False, 'error': '媒体请求已取消，未应用'}
            pending.done.set()
            return False
        mutation = pending.request['action'] != 'get_status'
        try:
            result = apply_request(screen, network, pending.request)
            if mutation and not pending.is_live():
                # A native stop/start cannot be interrupted mid-call. Do not
                # leave a late camera stream active after the caller timed out.
                screen.stop_media()
                if screen.page.startswith('media_'):
                    screen.page = 'monitor'
                raise RuntimeError('媒体请求已超时，已停止本次媒体操作')
            pending.response = {'ok': True, 'result': result}
        except Exception:
            pending.response = {'ok': False, 'error': '媒体操作未完成，请先完成屏幕编辑并检查摄像头或媒体来源'}
        finally:
            pending.done.set()
        return mutation

    def close(self):
        self._stop.set()
        while True:
            try: pending = self.requests.get_nowait()
            except queue.Empty: break
            pending.cancelled.set()
            pending.response = {'ok': False, 'error': '监控界面已关闭'}
            pending.done.set()
        if self._socket is not None:
            self._socket.close()
        with self._lock:
            for client in self._clients:
                try: client.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            info = os.lstat(self.path)
            if self._identity == (info.st_dev, info.st_ino) and stat.S_ISSOCK(info.st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass
