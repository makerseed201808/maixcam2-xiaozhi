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
        r = t.capture("细胞很清楚", autofocus=False)
        self.assertTrue(r["ok"]); self.assertIn("cap-000009", r["message"])

    def test_bad_slot(self):
        t, m10, b = self.make()
        with self.assertRaises(ValueError):
            t.select_slot(7)


if __name__ == "__main__":
    unittest.main()


class FakeWeb(object):
    """替身网页后端：plan_check 只认 select_slot/autofocus/capture，jobs 记录提交的清单。"""
    def __init__(self):
        self.calls = []
        self.jobs = []

    def __call__(self, method, url, body=None, timeout=8):
        path = url.split("8000", 1)[1]
        self.calls.append((method, path, body))
        if path == "/api/jobs/plan_check":
            names = {"select_slot": lambda s: "换到 %d 号玻片" % s["slot"], "autofocus": lambda s: "自动对焦",
                     "capture": lambda s: "拍照存档"}
            if any(s.get("do") not in names for s in body["steps"]):
                return 400, {"error": "第 1 步不是允许的步骤"}
            return 200, {"steps": body["steps"], "says": [names[s["do"]](s) for s in body["steps"]],
                         "est_s": 34 * len(body["steps"])}
        if path == "/api/jobs" and method == "POST":
            self.jobs.append(body)
            return 200, {"job_id": "J-1", "status": "running"}
        if path.startswith("/api/jobs?"):
            return 200, {"active": "J-1", "jobs": [{"job_id": "J-1", "status": "running", "progress": "步骤 2/3 自动对焦",
                                                    "records": ["R-1"]}]}
        if path == "/api/jobs/cancel":
            return 200, {"job_id": "J-1"}
        return 404, {}


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.web = FakeWeb()
        self.tools = ScopeTools(config_path="/nonexistent", clock=lambda: self.t[0], http=self.web)

    def test_prepare_does_not_start_until_confirmed(self):
        r = self.tools.prepare_plan('[{"do":"select_slot","slot":2},{"do":"autofocus"},{"do":"capture"}]', note="课堂")
        self.assertTrue(r["ok"]) ; self.assertFalse(r["started"])
        self.assertIn("换到 2 号玻片", r["message"]); self.assertIn("确认开始", r["message"])
        self.assertEqual(self.web.jobs, [])                         # 只检查，没提交
        r = self.tools.start_plan()
        self.assertTrue(r["started"])
        self.assertEqual(self.web.jobs[0]["kind"], "plan")
        self.assertEqual(len(self.web.jobs[0]["steps"]), 3); self.assertEqual(self.web.jobs[0]["note"], "小智 课堂")
        self.assertFalse(self.tools.start_plan()["ok"])              # 一份清单只执行一次

    def test_tour_preset_builds_steps(self):
        self.tools.prepare_tour("1，3", autofocus=False)
        steps = self.web.calls[0][2]["steps"]
        self.assertEqual(steps, [{"do": "select_slot", "slot": 1}, {"do": "capture"},
                                 {"do": "select_slot", "slot": 3}, {"do": "capture"}])
        self.tools.prepare_tour("")
        self.assertEqual(len(self.web.calls[1][2]["steps"]), 12)
        self.tools.prepare_tour("12", autofocus=False)            # 小智把“1号和2号”写成 "12"
        self.assertEqual([x["slot"] for x in self.web.calls[2][2]["steps"] if x["do"] == "select_slot"], [1, 2])
        self.tools.prepare_tour("15", autofocus=False)            # 含 5：不拆，交给后端拒绝
        self.assertEqual(self.web.calls[3][2]["steps"][0]["slot"], 15)

    def test_bad_plan_and_expiry(self):
        self.assertFalse(self.tools.prepare_plan("不是json")["ok"])
        r = self.tools.prepare_plan('[{"do":"move_z","z":1}]')
        self.assertFalse(r["ok"]); self.assertIn("不是允许", r["message"])
        self.assertFalse(self.tools.start_plan()["ok"])
        self.tools.prepare_tour("1")
        self.t[0] += 301
        r = self.tools.start_plan()
        self.assertFalse(r["ok"]); self.assertIn("过期", r["message"]); self.assertEqual(self.web.jobs, [])

    def test_status_counts_records_and_stop_clears_pending(self):
        r = self.tools.job_status()
        self.assertTrue(r["running"]); self.assertEqual(r["photos"], 1); self.assertIn("已拍 1 张", r["message"])
        self.tools.prepare_tour("1")
        self.assertTrue(self.tools.stop_job()["ok"])
        self.assertFalse(self.tools.start_plan()["ok"])

    def test_backend_unreachable(self):
        def boom(*a, **k):
            raise OSError("refused")
        tools = ScopeTools(config_path="/nonexistent", http=boom)
        r = tools.prepare_tour("1")
        self.assertFalse(r["ok"]); self.assertIn("连不上", r["message"])



class AutofocusDefaultTests(unittest.TestCase):
    """2026-11-15 用户要求：给小智下命令拍照时默认包含对焦。"""
    def setUp(self):
        self.web = FakeWeb()
        self.tools = ScopeTools(config_path="/nonexistent", http=self.web)

    def test_plan_inserts_focus_before_capture(self):
        from scope_tools import add_focus_before_capture
        a, c, w = {"do": "autofocus"}, {"do": "capture"}, {"do": "wait", "seconds": 10}
        s1, s2 = {"do": "select_slot", "slot": 1}, {"do": "select_slot", "slot": 2}
        self.assertEqual(add_focus_before_capture([s1, c, s2, c]), [s1, a, c, s2, a, c])
        self.assertEqual(add_focus_before_capture([s1, a, c, w, c]), [s1, a, c, w, c])     # 已对焦：等待后再拍不重复对焦
        self.assertEqual(add_focus_before_capture([c]), [a, c])                            # 当前槽直接拍：先对焦
        self.tools.prepare_plan('[{"do":"select_slot","slot":3},{"do":"capture"}]')
        self.assertEqual([x["do"] for x in self.web.calls[-1][2]["steps"]], ["select_slot", "autofocus", "capture"])
        self.tools.prepare_plan('[{"do":"select_slot","slot":3},{"do":"capture"}]', autofocus=False)
        self.assertEqual([x["do"] for x in self.web.calls[-1][2]["steps"]], ["select_slot", "capture"])

    def test_single_capture_focuses_in_background(self):
        r = self.tools.capture("洋葱")
        self.assertTrue(r["ok"]); self.assertIn("对焦", r["message"])
        job = self.web.jobs[-1]
        self.assertEqual(job["kind"], "plan")
        self.assertEqual(job["steps"], [{"do": "autofocus"}, {"do": "capture", "note": "洋葱"}])
