import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "xiaozhi", "service"))
from scope_tools import ScopeTools  # noqa: E402


class Msg(object):
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload.encode("utf-8")


class Broker(object):
    def __init__(self):
        self.clients = []

    def publish(self, topic, payload):
        for c in list(self.clients):
            if topic in c.subs and c.on_message:
                c.on_message(c, None, Msg(topic, payload))


class Client(object):
    def __init__(self, broker):
        self.broker, self.subs = broker, set()
        self.on_message = self.on_connect = self.on_disconnect = None
        broker.clients.append(self)

    def username_pw_set(self, u, p): pass
    def reconnect_delay_set(self, a, b): pass
    def connect_async(self, h, p, keepalive=0): pass
    def loop_start(self): threading.Timer(0.01, lambda: self.on_connect(self, None, {}, 0)).start()
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, t, qos=0): self.subs.add(t)
    def publish(self, t, payload, qos=0): threading.Thread(target=self.broker.publish, args=(t, payload)).start()


class FakeM10(object):
    """回应 scope_cmd 的假控制板；mode=silent 时只回 accepted 不回 done。"""
    def __init__(self, broker, mode="ok", slot=2):
        self.c = Client(broker)
        self.c.subs.add("siot/scope_cmd")
        self.c.on_message = self.on_cmd
        self.mode, self.slot, self.cmds = mode, slot, []
        self.stop = threading.Event()
        threading.Thread(target=self.beat, daemon=True).start()

    def beat(self):
        while not self.stop.is_set():
            self.c.publish("siot/scope_state", json.dumps({"src": "m10", "state": "idle", "homed": True,
                                                           "z": 5000, "slot": self.slot, "volt": 7.9}))
            time.sleep(0.2)

    def on_cmd(self, c, u, m):
        d = json.loads(m.payload)
        self.cmds.append(d)
        c.publish("siot/scope_ack", json.dumps({"id": d["id"], "status": "accepted"}))
        if self.mode == "silent":
            return
        if self.mode == "reject":
            c.publish("siot/scope_ack", json.dumps({"id": d["id"], "status": "rejected", "reason": "正忙：autofocus"}))
            return
        time.sleep(0.05)
        res = {"slot": d.get("args", {}).get("slot"), "z": 202} if d["cmd"] == "select_slot" else {"z": 6207, "seconds": 62.9}
        c.publish("siot/scope_ack", json.dumps({"id": d["id"], "status": "done", "result": res}))


class ScopeToolsTest(unittest.TestCase):
    def make(self, mode="ok"):
        cfg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"host": "x", "port": 1883, "username": "test-user", "password": "p"}, cfg); cfg.close()
        b = Broker()
        m10 = FakeM10(b, mode)
        self.addCleanup(m10.stop.set)
        return ScopeTools(cfg.name, factory=lambda cid: Client(b)), m10, b

    def test_select_slot_waits_for_done(self):
        t, m10, b = self.make()
        r = t.select_slot(3)
        self.assertTrue(r["ok"] and r["confirmed"])
        self.assertIn("3 号玻片", r["message"])
        self.assertEqual(m10.cmds[-1]["args"], {"slot": 3})

    def test_rejection_is_reported(self):
        t, m10, b = self.make("reject")
        r = t.autofocus()
        self.assertFalse(r["ok"]); self.assertTrue(r["confirmed"]); self.assertIn("正忙", r["message"])

    def test_timeout_says_unconfirmed_and_never_resends(self):
        t, m10, b = self.make("silent")
        t._command.__func__  # noqa
        r = t._command("autofocus", None, 0.6)
        self.assertFalse(r["confirmed"]); self.assertIn("未确认", r["message"]); self.assertIn("没有自动重发", r["message"])
        time.sleep(0.5)
        self.assertEqual(len(m10.cmds), 1)

    def test_offline_board_not_commanded(self):
        t, m10, b = self.make()
        m10.stop.set()
        t._ensure()
        time.sleep(3.3)
        r = t.select_slot(1)
        self.assertFalse(r["ok"]); self.assertIn("离线", r["message"])

    def test_status_and_capture(self):
        t, m10, b = self.make()
        self.assertIn("2 号玻片", t.status()["message"])
        cam = Client(b); cam.subs.add("siot/scope_capture")
        def on_cap(c, u, m):
            d = json.loads(m.payload)
            if d.get("type") == "request":
                c.publish("siot/scope_capture", json.dumps({"type": "result", "id": d["id"], "capture_id": "cap-000009",
                                                            "width": 2560, "height": 1440}))
        cam.on_message = on_cap
        r = t.capture("细胞很清楚")
        self.assertTrue(r["ok"]); self.assertIn("cap-000009", r["message"])

    def test_bad_slot(self):
        t, m10, b = self.make()
        with self.assertRaises(ValueError):
            t.select_slot(7)


if __name__ == "__main__":
    unittest.main()
