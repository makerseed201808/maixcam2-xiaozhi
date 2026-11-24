#!/usr/bin/env python3
"""Explicitly launched PC simulation of the SIoT ESP32 reply protocol.

This example does not access GPIO or control hardware. It changes an in-memory
indicator only. Use --config with a private monitor.json and --device alias.
"""
import argparse
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
import time


class SimulatedDevice:
    def __init__(self, device_id, allowed_actions, clock=time.monotonic):
        self.device_id=device_id
        self.allowed_actions=set(allowed_actions).intersection({'read_state','set_indicator'})
        self.clock=clock;self.state={'indicator':False,'simulated':True}
        self.completed=OrderedDict();self.executions=0

    def handle(self, payload, retained=False):
        if retained or not isinstance(payload,bytes) or len(payload)>4096:return None
        try:
            request=json.loads(payload.decode('utf-8'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if not isinstance(request,dict) or request.get('type')!='command' or request.get('device_id')!=self.device_id:return None
            identity=request.get('command_id');action=request.get('action');value=request.get('value')
            if not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{32}',identity):return None
            if not isinstance(action,str) or not isinstance(value,dict):return None
            fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        except (ValueError,TypeError,UnicodeError,RecursionError):return None
        now=self.clock()
        for key in list(self.completed):
            if now-self.completed[key][0]>300:self.completed.pop(key)
        previous=self.completed.get(identity)
        if previous:
            if previous[1]==fingerprint:return json.loads(previous[2])
            return dict(type='command_result',device_id=self.device_id,command_id=identity,
                        success=False,state=dict(self.state),error='command_id_reused')
        success=False
        if action in self.allowed_actions:
            if action=='read_state' and not value:success=True
            elif action=='set_indicator' and set(value)=={'enabled'} and type(value['enabled']) is bool:
                self.state['indicator']=value['enabled'];self.executions+=1;success=True
        reply=dict(type='command_result',device_id=self.device_id,command_id=identity,
                   success=success,state=dict(self.state))
        self.completed[identity]=(now,fingerprint,json.dumps(reply))
        while len(self.completed)>128:self.completed.popitem(last=False)
        return reply


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True,help='Private connection file, never commit credentials')
    parser.add_argument('--device',required=True,help='Device alias from devices mapping')
    args=parser.parse_args()
    # Reuse validation without importing any Maix/audio code.
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'xiaozhi/service'))
    from siot_devices import SiotDevices
    entries=SiotDevices(None,str(args.config))._devices()
    if args.device not in entries:raise SystemExit('Device alias not configured')
    profile=entries[args.device]
    raw=args.config.read_bytes()
    if len(raw)>65536:raise SystemExit('Connection file too large')
    config=json.loads(raw)
    import paho.mqtt.client as mqtt
    simulator=SimulatedDevice(profile['device_id'],profile['allowed_actions'])
    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(config['username'],config['password'])
    def connected(c,u,f,reason,props):
        if not reason.is_failure:c.subscribe(profile['command_topic'],qos=0)
    def received(c,u,message):
        if message.topic!=profile['command_topic']:return
        reply=simulator.handle(bytes(message.payload),retained=bool(message.retain))
        if reply is not None:c.publish(profile['reply_topic'],json.dumps(reply,ensure_ascii=False),qos=0,retain=False)
    client.on_connect=connected;client.on_message=received
    client.connect(config['host'],config.get('port',1883),keepalive=30)
    print('SIMULATION ONLY: in-memory indicator; no GPIO or physical device control.')
    try:client.loop_forever()
    except KeyboardInterrupt:pass
    finally:client.disconnect()


if __name__=='__main__':main()
