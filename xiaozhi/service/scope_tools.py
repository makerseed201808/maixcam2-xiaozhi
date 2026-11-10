"""小智显微工具（智能显微镜开发方案 2.7）：选槽、自动对焦、拍照并备注、查询状态。

运动类工具经 SIoT 发给行空板 M10（siot/scope_cmd），等到同命令号的最终回执（done/rejected/error）
再答复；超时只报告“未确认”，绝不自动重发。拍照经 siot/scope_capture 发给显微服务，等结果。
连接按需建立，SIoT 账号读 /etc/siot/monitor.json；导入本模块不打开任何连接。
消息格式见 smart-microscope 仓库 docs/protocol.md。
"""
import itertools
import json
import threading
import time

TOPICS = {"cmd": "siot/scope_cmd", "ack": "siot/scope_ack", "state": "siot/scope_state",
          "capture": "siot/scope_capture"}
STATE_LABEL = {"unhomed": "未回零", "idle": "就绪", "busy": "运行中", "stopped": "已停止",
               "error": "故障", "calibrating": "槽位标定中"}


def _default_factory(client_id):
    import paho.mqtt.client as mqtt
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    return mqtt.Client(client_id=client_id)


class ScopeTools(object):
    OFFLINE_AFTER_S = 3.0

    def __init__(self, config_path="/etc/siot/monitor.json", factory=None, clock=time.time):
        self._path = config_path
        self._factory = factory or _default_factory
        self._clock = clock
        self._cv = threading.Condition()
        self._client = None
        self._connected = False
        self._acks = {}
        self._captures = {}
        self._state = None
        self._state_t = 0.0
        self._seq = itertools.count(1)
        self._prefix = "xz%d" % (int(clock()) % 100000)

    # ---------- 连接 ----------
    def _ensure(self, wait_s=3.0):
        with self._cv:
            if self._client is None:
                with open(self._path) as f:
                    cfg = json.load(f)
                c = self._factory("xiaozhi_scope_%d" % int(self._clock()))
                c.username_pw_set(cfg["username"], cfg["password"])
                c.on_connect = self._on_connect
                c.on_disconnect = lambda cl, u, rc: self._set_connected(False)
                c.on_message = self._on_message
                c.reconnect_delay_set(1, 5)
                c.connect_async(cfg.get("host", "127.0.0.1"), int(cfg.get("port", 1883)), keepalive=15)
                c.loop_start()
                self._client = c
            end = self._clock() + wait_s
            while not self._connected and self._clock() < end:
                self._cv.wait(0.1)
            if not self._connected:
                raise RuntimeError("SIoT 未连接，显微镜命令未发送")
            # 刚连上时还没收到状态，等一个状态周期
            end = self._clock() + 1.5
            while self._state is None and self._clock() < end:
                self._cv.wait(0.1)

    def _set_connected(self, ok):
        with self._cv:
            self._connected = ok
            self._cv.notify_all()

    def _on_connect(self, client, u, flags, rc):
        if rc == 0:
            client.subscribe(TOPICS["ack"], qos=1)
            client.subscribe(TOPICS["state"], qos=0)
            client.subscribe(TOPICS["capture"], qos=1)
        self._set_connected(rc == 0)

    def _on_message(self, client, u, msg):
        try:
            d = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(d, dict):
            return
        with self._cv:
            if msg.topic == TOPICS["state"] and d.get("src") == "m10":
                self._state, self._state_t = d, self._clock()
            elif msg.topic == TOPICS["ack"] and d.get("id"):
                if d.get("status") in ("done", "rejected", "error") or d.get("id") not in self._acks:
                    self._acks[d["id"]] = d
            elif msg.topic == TOPICS["capture"] and d.get("type") in ("result", "error") and d.get("id"):
                self._captures[d["id"]] = d
            self._cv.notify_all()

    def stop(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

    # ---------- 工具 ----------
    def _online(self):
        return self._state is not None and self._clock() - self._state_t <= self.OFFLINE_AFTER_S

    def _command(self, cmd, args, timeout_s):
        self._ensure()
        if not self._online():
            return {"ok": False, "confirmed": False, "message": "显微镜控制板离线（超过 3 秒没有状态），命令未发送"}
        cid = "%s-%04d" % (self._prefix, next(self._seq))
        msg = {"id": cid, "cmd": cmd}
        if args:
            msg["args"] = args
        self._client.publish(TOPICS["cmd"], json.dumps(msg, ensure_ascii=False), qos=1)
        end = self._clock() + timeout_s
        with self._cv:
            while self._clock() < end:
                a = self._acks.get(cid)
                if a and a.get("status") in ("done", "rejected", "error"):
                    self._acks.pop(cid, None)
                    return self._ack_text(cmd, a)
                self._cv.wait(0.2)
            accepted = self._acks.pop(cid, None)
        return {"ok": False, "confirmed": False, "id": cid,
                "message": "%s 秒内没有收到完成回执，结果未确认；没有自动重发，请看屏幕或问一下状态。%s" % (
                    int(timeout_s), "（控制板已接受命令）" if accepted else "")}

    @staticmethod
    def _ack_text(cmd, a):
        st = a.get("status")
        if st == "done":
            r = a.get("result") or {}
            if cmd == "select_slot":
                text = "已换到 %s 号玻片，载物台高度 %s 步。" % (r.get("slot"), r.get("z"))
            elif cmd == "autofocus":
                text = "对焦完成，最清晰位置在 %s 步，用时 %s 秒。" % (r.get("z"), r.get("seconds"))
            else:
                text = "完成。"
            return {"ok": True, "confirmed": True, "id": a.get("id"), "message": text, "result": r}
        reason = a.get("reason") or ""
        return {"ok": False, "confirmed": True, "id": a.get("id"),
                "message": ("没有执行：" if st == "rejected" else "执行失败：") + reason}

    def select_slot(self, slot):
        if slot not in (1, 2, 3, 4):
            raise ValueError("玻片槽号只能是 1 到 4")
        return self._command("select_slot", {"slot": slot}, 90)

    def autofocus(self):
        return self._command("autofocus", None, 180)

    def capture(self, note=""):
        self._ensure()
        rid = "%s-cap-%04d" % (self._prefix, next(self._seq))
        slot = self._state.get("slot") if self._online() else None
        req = {"type": "request", "id": rid, "note": note or "", "slot": slot, "source": "xiaozhi"}
        self._client.publish(TOPICS["capture"], json.dumps(req, ensure_ascii=False), qos=1)
        end = self._clock() + 25
        with self._cv:
            while self._clock() < end:
                r = self._captures.pop(rid, None)
                if r:
                    if r.get("type") == "result":
                        return {"ok": True, "confirmed": True, "capture_id": r.get("capture_id"),
                                "message": "已拍照存档，编号 %s，%s×%s。%s" % (
                                    r.get("capture_id"), r.get("width"), r.get("height"),
                                    "备注：%s" % note if note else "")}
                    return {"ok": False, "confirmed": True, "message": "拍照失败：%s" % r.get("reason")}
                self._cv.wait(0.2)
        return {"ok": False, "confirmed": False, "message": "25 秒内没有收到拍照结果，未确认；没有自动重拍。"}

    def status(self):
        self._ensure()
        if not self._online():
            return {"online": False, "message": "显微镜控制板离线（超过 3 秒没有状态）"}
        s = self._state
        parts = [STATE_LABEL.get(s.get("state"), s.get("state"))]
        parts.append("当前 %s 号玻片" % s["slot"] if s.get("slot") else "未在任何玻片槽位")
        if s.get("homed"):
            parts.append("载物台高度 %s 步" % s["z"])
        elif s.get("state") != "unhomed":
            parts.append("尚未回零")
        if s.get("volt") is not None:
            parts.append("电池 %.1f 伏%s" % (s["volt"], "，电量低" if s.get("low_batt") else ""))
        if s.get("msg"):
            parts.append("提示：%s" % s["msg"])
        return {"online": True, "state": s.get("state"), "slot": s.get("slot"), "z": s.get("z"),
                "message": "，".join(parts) + "。"}
