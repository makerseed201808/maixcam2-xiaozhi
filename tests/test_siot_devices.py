import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('siot_devices', ROOT/'xiaozhi/service/siot_devices.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Transport:
    def __init__(self):
        self.events=[];self.reply_kind='ok';self.sequence=0;self.reads=0
    def subscribe(self, topic):
        self.events.append(('subscribe',topic))
        return {'subscribed':True,'ready':True,'sequence':self.sequence}
    def publish(self, topic, message, save=False, cancel_event=None):
        self.events.append(('publish',topic));self.command=json.loads(message)
        return {'status':'local_sent'}
    def read_after(self, topic, sequence=0, timeout=3, cancel_event=None):
        self.reads+=1;self.sequence+=1
        response={'type':'command_result','device_id':self.command['device_id'],
                  'command_id':self.command['command_id'],'success':True,'state':{'indicator':True}}
        if self.reply_kind=='wrong_id':response['command_id']='different'
        if self.reply_kind=='wrong_device':response['device_id']='different'
        if self.reply_kind=='bad_success':response['success']='true'
        if self.reply_kind=='bad_state':response['state']='not an object'
        if self.reply_kind=='failure':response['success']=False
        if self.reply_kind=='cancel':cancel_event.set()
        if self.reply_kind=='timeout':return {'available':False}
        return {'available':True,'message':json.dumps(response),'sequence':
                sequence if self.reply_kind=='stale' else self.sequence,
                'retained':self.reply_kind=='retained'}


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'monitor.json'
        self.config={'devices':{'lamp':{'device_id':'esp32-1','command_topic':'siot/lamp/cmd',
                     'reply_topic':'siot/lamp/reply','allowed_actions':['set_indicator','read_state']}}}
        self.save();self.transport=Transport();self.devices=module.SiotDevices(self.transport,str(self.path))
    def save(self):self.path.write_text(json.dumps(self.config),encoding='utf-8')
    def test_list_safe_and_empty_defaults(self):
        result=self.devices.list_devices();self.assertEqual(result['devices'][0]['name'],'lamp')
        self.config={};self.save();self.assertEqual(self.devices.list_devices()['devices'],[])
    def test_subscribe_first_and_correlated_execution(self):
        result=self.devices.command('lamp','set_indicator','{"enabled":true}',100)
        self.assertEqual([e[0] for e in self.transport.events],['subscribe','publish'])
        self.assertEqual(result['status'],'execution_confirmed')
        self.assertTrue(result['success']);self.assertEqual(result['state'],{'indicator':True})
        self.assertEqual(self.transport.command['value'],{'enabled':True})
    def test_explicit_device_failure(self):
        self.transport.reply_kind='failure'
        result=self.devices.command('lamp','read_state',timeout_ms=100)
        self.assertEqual(result['status'],'execution_failed');self.assertFalse(result['success'])
    def test_rejects_wrong_stale_retained_or_malformed_feedback(self):
        for kind in ('wrong_id','wrong_device','retained','stale','bad_success','bad_state','timeout'):
            with self.subTest(kind=kind):
                self.transport.reply_kind=kind
                result=self.devices.command('lamp','read_state',timeout_ms=100)
                self.assertEqual(result['status'],'execution_unknown');self.assertIsNone(result['success'])
    def test_unknown_device_action_and_bad_values_do_not_send(self):
        for args in [('missing','read_state','{}',100),('lamp','reboot','{}',100),
                     ('lamp','read_state','[]',100),('lamp','read_state','{"x":NaN}',100),
                     ('lamp','read_state','{"x":1e999}',100),
                     ('lamp','read_state','{}',True),('lamp','read_state','{}',10001)]:
            with self.assertRaises(ValueError):self.devices.command(*args)
        self.assertEqual(self.transport.events,[])
    def test_config_topics_are_exact_and_different(self):
        for invalid in ['siot/#','$SYS/test','x\x00y','x'*129,'siot/lamp/cmd']:
            self.config['devices']['lamp']['reply_topic']=invalid;self.save()
            with self.assertRaises(RuntimeError):self.devices.list_devices()
    def test_cancel_before_submit_and_after_submit(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(RuntimeError):self.devices.command('lamp','read_state',timeout_ms=100,cancel_event=cancel)
        self.assertEqual(self.transport.events,[])
        cancel.clear();self.transport.reply_kind='cancel'
        result=self.devices.command('lamp','read_state',timeout_ms=100,cancel_event=cancel)
        self.assertEqual(result['status'],'execution_unknown')
    def test_publish_uncertainty_no_retry(self):
        def fail(*args,**kwargs):self.transport.events.append(('publish','unknown'));raise RuntimeError('private detail')
        self.transport.publish=fail
        result=self.devices.command('lamp','read_state',timeout_ms=100)
        self.assertEqual(result['status'],'execution_unknown')
        self.assertNotIn('private detail',json.dumps(result));self.assertEqual(len(self.transport.events),2)
    def test_commands_have_unique_ids_and_no_retries(self):
        a=self.devices.command('lamp','read_state',timeout_ms=100)
        b=self.devices.command('lamp','read_state',timeout_ms=100)
        self.assertNotEqual(a['command_id'],b['command_id'])
        self.assertEqual(sum(e[0]=='publish' for e in self.transport.events),2)

    def test_simulator_deduplicates_without_actuating_hardware(self):
        spec=importlib.util.spec_from_file_location('device_example',ROOT/'examples/siot_device_responder.py')
        sample=importlib.util.module_from_spec(spec);spec.loader.exec_module(sample)
        simulator=sample.SimulatedDevice('esp32-1',['set_indicator'])
        command={'type':'command','device_id':'esp32-1','command_id':'a'*32,
                 'action':'set_indicator','value':{'enabled':True}}
        body=json.dumps(command).encode()
        first=simulator.handle(body);second=simulator.handle(body)
        self.assertEqual(first,second);self.assertEqual(simulator.executions,1)
        command['value']['enabled']=False
        self.assertFalse(simulator.handle(json.dumps(command).encode())['success'])
        self.assertTrue(simulator.state['indicator']);self.assertEqual(simulator.executions,1)
        self.assertIsNone(simulator.handle(body,retained=True))

    def test_busy_command_rejected_without_waiting_or_sending(self):
        self.devices._transaction.acquire()
        try:
            with self.assertRaises(RuntimeError):self.devices.command('lamp','read_state')
        finally:self.devices._transaction.release()
        self.assertEqual(self.transport.events,[])

    def test_actual_transport_adapter_with_simulated_receiver(self):
        from test_siot_tools import module as transport_module, Client
        spec=importlib.util.spec_from_file_location('device_sample_integration',ROOT/'examples/siot_device_responder.py')
        sample=importlib.util.module_from_spec(spec);spec.loader.exec_module(sample)
        simulator=sample.SimulatedDevice('esp32-1',['set_indicator'])
        class LoopbackClient(Client):
            def publish(client,topic,payload,qos,retain):
                info=super().publish(topic,payload,qos,retain)
                if topic=='siot/lamp/cmd':
                    response=simulator.handle(payload.encode())
                    client.deliver('siot/lamp/reply',json.dumps(response).encode())
                return info
        self.config.update(host='localhost',username='fixture',password='fixture-secret');self.save()
        client=LoopbackClient()
        transport=transport_module.SiotTools(str(self.path),factory=lambda:client)
        self.addCleanup(transport.stop)
        devices=module.SiotDevices(transport,str(self.path))
        result=devices.command('lamp','set_indicator','{"enabled":true}',100)
        self.assertEqual(result['status'],'execution_confirmed')
        self.assertEqual(simulator.executions,1)
        self.assertTrue(result['state']['simulated'])
