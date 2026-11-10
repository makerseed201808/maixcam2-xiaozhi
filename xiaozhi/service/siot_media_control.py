"""Client for the already-open SIoT monitor's private media control socket.

Never opens the display or camera in the voice service. The monitor must be
opened from the device desktop first; no unverified native-App launch command
or background display owner is created here.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
from urllib.parse import urlsplit


class SiotMediaControl:
    SOCKET_PATH = '/run/siot-monitor-mcp/control.sock'
    TIMEOUT = 6.0
    MAX_RESPONSE = 8192

    def __init__(self, path=SOCKET_PATH):
        self.path = Path(path)

    @staticmethod
    def _topic(topic):
        if (not isinstance(topic, str) or not 0 < len(topic.encode('utf-8')) <= 128
                or topic.startswith('$') or any(c in topic for c in '+#')
                or any(ord(c) < 32 or ord(c) == 127 for c in topic)):
            raise ValueError('媒体主题必须是明确完整主题，最多128字节')

    def _check_endpoint(self):
        if not hasattr(os, 'getuid') or not hasattr(socket, 'AF_UNIX'):
            raise RuntimeError('媒体语音控制仅在设备Unix环境可用')
        directory, endpoint = os.lstat(self.path.parent), os.lstat(self.path)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or directory.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
                or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
            raise RuntimeError('媒体控制端点权限不正确')

    def _request(self, request, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('媒体请求已取消，未提交')
        try:
            self._check_endpoint()
        except FileNotFoundError:
            raise RuntimeError('请先从设备桌面打开SIoT监控，再使用媒体语音控制') from None
        wire = json.dumps(request, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(wire) > 4096:
            raise ValueError('媒体请求过大')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(.5)
                client.connect(str(self.path))
                if hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        raise RuntimeError('媒体控制进程身份不匹配')
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError('媒体请求已取消，未提交')
                client.sendall(wire)
                deadline = time.monotonic() + self.TIMEOUT
                response = bytearray()
                while b'\n' not in response:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError('媒体请求已取消；已应用的操作请查询状态确认')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('媒体请求超时，结果未确认；请查询状态后再操作')
                    client.settimeout(min(.1, remaining))
                    try:
                        chunk = client.recv(min(1024, self.MAX_RESPONSE + 1 - len(response)))
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise RuntimeError('媒体控制连接关闭，结果未确认')
                    response.extend(chunk)
                    if len(response) > self.MAX_RESPONSE:
                        raise RuntimeError('媒体状态返回过大')
        except OSError:
            raise RuntimeError('SIoT监控未就绪或已关闭，请从设备桌面打开后再试') from None
        try:
            result = json.loads(response.decode('utf-8'))
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('Invalid reply')
            if not result['ok']:
                # Do not reflect arbitrary server error strings/URLs to cloud.
                raise RuntimeError('媒体操作未确认；请完成屏幕编辑、检查来源并查询状态')
            state = result.get('result')
            if not isinstance(state, dict):
                raise ValueError('Invalid state')
            return state
        except (ValueError, UnicodeError):
            raise RuntimeError('媒体控制响应无效') from None

    def get_status(self, cancel_event=None):
        try:
            return self._request({'action': 'get_status'}, cancel_event)
        except RuntimeError:
            # Missing/unavailable monitor is not a claim that MQTT or camera is off.
            return {'monitor_open': None, 'state': 'unavailable',
                    'note': '无法查询监控媒体状态；请先打开SIoT监控并检查本地控制端点'}

    def start(self, topic, wire_format='base64', fps=5, cancel_event=None):
        self._topic(topic)
        if wire_format not in ('json', 'base64'):
            raise ValueError('媒体格式必须为json或base64')
        if type(fps) is not int or fps not in (2, 5, 10):
            raise ValueError('目标帧率必须为2、5或10')
        return self._request({'action': 'start', 'topic': topic,
                              'wire_format': wire_format, 'fps': fps}, cancel_event)

    def stop(self, cancel_event=None):
        return self._request({'action': 'stop'}, cancel_event)

    def show(self, source, target='', cancel_event=None):
        if source == 'topic':
            self._topic(target)
        elif source == 'url':
            if not isinstance(target, str) or not 0 < len(target.encode('utf-8')) <= 512:
                raise ValueError('媒体地址无效')
            try:
                value = urlsplit(target)
                valid = value.scheme in ('http', 'https', 'rtsp') and value.hostname and not value.fragment
                _ = value.port
            except ValueError:
                valid = False
            if not valid or any(ord(c) < 33 or ord(c) == 127 for c in target):
                raise ValueError('只支持完整HTTP、HTTPS或RTSP媒体地址')
        elif source != 'lan_sender' or target != '':
            raise ValueError('source必须为topic、url或lan_sender；局域网发送端target留空')
        return self._request({'action': 'show', 'source': source, 'target': target}, cancel_event)
