"""Configured device commands with correlated feedback, never broker-as-actuator ACK.

No hardware is controlled here. Devices must implement the documented protocol.
No command retries, no retained commands, no message content logging.
"""
import json
from pathlib import Path
import threading
import time
import uuid


def _text(value, maximum):
    if (not isinstance(value,str) or not value or len(value.encode('utf-8'))>maximum
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        raise ValueError('Invalid identifier')
    return value


def _topic(value):
    _text(value,128)
    if value.startswith('$') or any(c in value for c in '+#'):
        raise ValueError('Invalid topic')
    return value


def _reject_constant(value):
    raise ValueError('Non-finite JSON is not supported')


class SiotDevices:
    MAX_DEVICES=32
    MAX_COMMAND_BYTES=4096

    def __init__(self, transport, config_path='/etc/siot/monitor.json'):
        self.transport=transport
        self._path=config_path
        self._transaction=threading.Lock()

    def _devices(self):
        try:
            with Path(self._path).open('rb') as stream:raw=stream.read(65537)
            if len(raw)>65536:raise ValueError('Config size')
            config=json.loads(raw)
            if not isinstance(config,dict):raise ValueError('Config type')
            entries=config.get('devices',{})
            if not isinstance(entries,dict) or len(entries)>self.MAX_DEVICES:raise ValueError('Device list')
            result={};ids=set()
            for name,item in entries.items():
                _text(name,64)
                if not isinstance(item,dict):raise ValueError('Device entry')
                device_id=_text(item.get('device_id'),128)
                command=_topic(item.get('command_topic'));reply=_topic(item.get('reply_topic'))
                actions=item.get('allowed_actions')
                if (command==reply or device_id in ids or not isinstance(actions,list)
                        or not 1<=len(actions)<=32):raise ValueError('Device protocol')
                checked=[_text(action,64) for action in actions]
                if len(set(checked))!=len(checked):raise ValueError('Duplicate actions')
                result[name]=dict(device_id=device_id,command_topic=command,reply_topic=reply,
                                  allowed_actions=checked)
                ids.add(device_id)
            return result
        except Exception:
            raise RuntimeError('SIoT设备配置不可用，请检查本地devices配置；未执行命令') from None

    def list_devices(self):
        devices=self._devices()
        return dict(devices=[dict(name=name,**entry) for name,entry in devices.items()],
                    count=len(devices),source='local_configuration',online_verified=False,
                    note='仅列出本地登记的设备与允许动作，不代表设备在线或已完成实物验证')

    @staticmethod
    def _cancelled(event):return event is not None and event.is_set()

    def command(self, device, action, value_json='{}', timeout_ms=3000, cancel_event=None):
        if type(timeout_ms) is not int or not 100<=timeout_ms<=10000:
            raise ValueError('反馈等待时间必须为100至10000毫秒')
        try:
            _text(device,64);_text(action,64)
            if not isinstance(value_json,str) or len(value_json.encode('utf-8'))>2048:raise ValueError('value size')
            value=json.loads(value_json,parse_constant=_reject_constant)
            if not isinstance(value,dict):raise ValueError('value type')
            json.dumps(value,allow_nan=False)
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise ValueError('设备、动作和value_json参数无效；value_json必须为最多2048字节的JSON对象') from None
        entries=self._devices()
        if device not in entries:raise ValueError('设备未登记，请先查询device.list_devices')
        entry=entries[device]
        if action not in entry['allowed_actions']:raise ValueError('该设备未允许此动作')
        if self._cancelled(cancel_event):raise RuntimeError('会话已取消，命令未提交')
        if not self._transaction.acquire(blocking=False):raise RuntimeError('另一个设备命令正在等待反馈，请稍后再试')
        command_id=uuid.uuid4().hex
        base=dict(device=device,device_id=entry['device_id'],action=action,command_id=command_id)
        def unknown(reason):
            return dict(base,status='execution_unknown',success=None,state=None,reason=reason,
                        retry_performed=False,note='不能确认设备是否执行；请查询设备状态，不要自动重试动作')
        try:
            ready=self.transport.subscribe(entry['reply_topic'])
            if not ready.get('ready',ready.get('subscribed',False)):
                raise RuntimeError('反馈主题订阅未确认，命令未提交')
            cursor=ready.get('sequence',0)
            if type(cursor) is not int or cursor<0:raise RuntimeError('反馈序号不可用，命令未提交')
            if self._cancelled(cancel_event):raise RuntimeError('会话已取消，命令未提交')
            envelope=dict(type='command',command_id=command_id,device_id=entry['device_id'],
                          action=action,value=value)
            wire=json.dumps(envelope,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(wire.encode('utf-8'))>self.MAX_COMMAND_BYTES:raise ValueError('命令超过4096字节，未发送')
            try:
                result=self.transport.publish(entry['command_topic'],wire,save=False,cancel_event=cancel_event)
                if result.get('status')!='local_sent':return unknown('发送状态未确认')
            except Exception:
                return unknown('发送状态未确认或会话已取消')
            # timeout_ms bounds feedback waiting after the bounded subscribe and
            # publish operations, not transport setup. No write is repeated.
            deadline=time.monotonic()+timeout_ms/1000
            examined=0
            while time.monotonic()<deadline and examined<64:
                if self._cancelled(cancel_event):return unknown('等待反馈时会话取消')
                try:
                    message=self.transport.read_after(entry['reply_topic'],sequence=cursor,
                        timeout=max(0,deadline-time.monotonic()),cancel_event=cancel_event)
                except Exception:
                    return unknown('反馈读取中断')
                if self._cancelled(cancel_event):return unknown('等待反馈时会话取消')
                if time.monotonic()>deadline:return unknown('反馈超时')
                if not message.get('available'):
                    # Also bound injected/nonblocking transports; production
                    # read_after itself waits on a condition.
                    time.sleep(min(.01,max(0,deadline-time.monotonic())))
                    continue
                examined+=1
                sequence=message.get('sequence')
                if type(sequence) is not int or sequence<=cursor:continue
                cursor=sequence
                if message.get('retained'):continue
                try:
                    payload=message.get('message')
                    if not isinstance(payload,str) or len(payload.encode('utf-8'))>4096:continue
                    reply=json.loads(payload,parse_constant=_reject_constant)
                    if (not isinstance(reply,dict) or reply.get('type')!='command_result'
                            or reply.get('command_id')!=command_id or reply.get('device_id')!=entry['device_id']
                            or type(reply.get('success')) is not bool or not isinstance(reply.get('state'),dict)):
                        continue
                    # Reject pathological nested structures before returning
                    # user-supplied device state over MCP.
                    json.dumps(reply['state'],allow_nan=False)
                except (ValueError,TypeError,UnicodeError,RecursionError):continue
                return dict(base,status='execution_confirmed' if reply['success'] else 'execution_failed',
                            success=reply['success'],state=reply['state'],source='matched_device_reply',
                            retry_performed=False)
            return unknown('反馈超时或无匹配反馈')
        finally:
            self._transaction.release()
