"""小智显微工具（智能显微镜开发方案 2.7）：选槽、自动对焦、拍照并备注、查询状态。

运动类工具经 SIoT 发给行空板 M10（siot/scope_cmd），等到同命令号的最终回执（done/rejected/error）
再答复；超时只报告“未确认”，绝不自动重发。拍照经 siot/scope_capture 发给显微服务，等结果。
连接按需建立，SIoT 账号读 /etc/siot/monitor.json；导入本模块不打开任何连接。
消息格式见 smart-microscope 仓库 docs/protocol.md。

编排任务（2026-11-15）：多步操作（巡检、按清单换片/对焦/拍照/开关灯/等待）交给网页后端
（本机 :8000 /api/jobs）在后台跑。小智云端一轮回答约 70 秒就会断开（实测两次 68–73 s），
逐个调用 select_slot/autofocus/capture 做四个槽要 2 分钟以上，会做到一半停下。
两步确认（用户 2026-11-15 选定）：prepare_plan / prepare_tour 只检查清单、返回中文复述，不动机器；
用户听完说“开始”后 start_plan 才提交。待执行清单存在本进程里 PENDING_TTL_S 秒，
start_plan 不带参数，所以中途对话断开、重新唤醒后说“开始”也能接上。
"""
import itertools
import os
import json
import re
import threading
import time

TOPICS = {"cmd": "siot/scope_cmd", "ack": "siot/scope_ack", "state": "siot/scope_state",
          "capture": "siot/scope_capture"}
STATE_LABEL = {"unhomed": "未回零", "idle": "就绪", "busy": "运行中", "stopped": "已停止",
               "error": "故障", "calibrating": "槽位标定中"}


WEB_BASE = "http://127.0.0.1:8000"
PENDING_TTL_S = 300


def _http_json(method, url, body=None, timeout=8):
    """访问本机网页后端；不走系统代理。返回 (状态码, JSON)。"""
    import urllib.request
    import urllib.error
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


def microscope_running(proc="/proc"):
    """显微服务（scope_service.py）在跑时它占着摄像头，小智自带的拍照工具打不开相机。"""
    try:
        pids = [x for x in os.listdir(proc) if x.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                if b"scope_service.py" in f.read():
                    return True
        except OSError:
            continue
    return False


def add_focus_before_capture(steps):
    """拍照默认包含对焦：开始时或每次换片后，第一张照片前没有对焦就补一步对焦；等待、开关灯不影响已对好的焦。"""
    out, focused = [], False
    for st in steps:
        do = st.get("do") if isinstance(st, dict) else st
        if do == "select_slot":
            focused = False
        elif do == "autofocus":
            focused = True
        elif do == "capture" and not focused:
            out.append({"do": "autofocus"})
            focused = True
        out.append(st)
    return out


def _default_factory(client_id):
    import paho.mqtt.client as mqtt
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    return mqtt.Client(client_id=client_id)


class ScopeTools(object):
    OFFLINE_AFTER_S = 5.0          # 与网页“离线”显示、M10 链路保护一致（用户 2026-11-15 定为 5 秒）

    def __init__(self, config_path="/etc/siot/monitor.json", factory=None, clock=time.time, http=None,
                 web_base=WEB_BASE):
        self._http = http or _http_json
        self._pending = None          # 待用户确认的清单 {"steps", "says", "est_s", "note", "t"}
        self._pending_lock = threading.Lock()
        self._web = web_base.rstrip("/")
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
            return {"ok": False, "confirmed": False, "message": "显微镜控制板离线（超过 5 秒没有状态），命令未发送"}
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

    def capture(self, note="", autofocus=True):
        """拍照默认先自动对焦（用户 2026-11-15 要求）。对焦+拍照交给网页后端后台执行（对焦有时走完整扫描约 1 分钟，
        同步等会碰上小智约 70 秒的对话上限），工具立即返回；结果在屏幕底部和网页提示。autofocus=False 才直接拍。"""
        if autofocus:
            code, d = self._web_call("POST", "/api/jobs", {
                "kind": "plan", "steps": [{"do": "autofocus"}, {"do": "capture", "note": (note or "")[:60]}],
                "note": "小智 对焦后拍照"})
            if code != 200:
                return {"ok": False, "message": "没有开始对焦拍照：%s" % (d.get("error") or "网页后端返回 %s" % code)}
            return {"ok": True, "started": True, "job_id": d.get("job_id"),
                    "message": "正在先自动对焦再拍照（任务 %s），约 20 秒，最长约 1 分钟；完成后显微镜屏幕底部和网页会提示，"
                               "可以问我任务进度。" % d.get("job_id")}
        return self._capture_now(note)

    def _capture_now(self, note=""):
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

    # ---------- 编排任务 / 一键巡检（网页后端后台执行，两步确认） ----------
    def _web_call(self, method, path, body=None):
        try:
            return self._http(method, self._web + path, body)
        except Exception as exc:
            return None, {"error": "连不上显微镜网页后端（%s）" % exc.__class__.__name__}

    def _prepare(self, steps, note):
        code, d = self._web_call("POST", "/api/jobs/plan_check", {"steps": steps})
        if code != 200:
            return {"ok": False, "message": "清单没有通过检查，没有执行：%s" % (d.get("error") or "网页后端返回 %s" % code)}
        with self._pending_lock:
            self._pending = {"steps": d["steps"], "says": d["says"], "est_s": d.get("est_s") or 0,
                             "note": ("小智 " + (note or "")).strip()[:100], "t": self._clock()}
        est = d.get("est_s") or 0
        return {"ok": True, "started": False, "steps": d["says"],
                "message": "还没有开始。请把下面的步骤念给用户，并问“确认开始吗？”；用户说开始/确认后再调用 "
                           "self.microscope.start_plan，用户说不要就不调用。步骤：%s。共 %d 步，预计约 %s。" % (
                               "；".join("%d. %s" % (i, x) for i, x in enumerate(d["says"], 1)), len(d["says"]),
                               "%d 分钟" % max(1, round(est / 60.0)) if est >= 90 else "%d 秒" % max(1, est))}

    def prepare_plan(self, steps="", note="", autofocus=True):
        """steps：JSON 数组字符串，如 [{"do":"select_slot","slot":1},{"do":"autofocus"},{"do":"capture"}]。
        autofocus=True（默认）：拍照前如果还没对焦（开始时或换片后没对焦过），自动补一步对焦。"""
        if isinstance(steps, str):
            try:
                steps = json.loads(steps or "[]")
            except ValueError:
                return {"ok": False, "message": "steps 不是合法的 JSON 数组，请按工具说明的格式重写"}
        if autofocus and isinstance(steps, list):
            steps = add_focus_before_capture(steps)
        return self._prepare(steps, note)

    def prepare_tour(self, slots="", autofocus=True, note=""):
        """slots：如 "1,2,3,4" 或 "2 4"，空 = 全部 4 个槽。每片换片→（对焦）→拍照。"""
        try:
            # 只有 1–4 号槽：小智有时把“1号和2号”写成 "12"，每一位都是 1–4 时按单个数字拆开
            parts = [x for x in re.split(r"[,，、和\s]+", str(slots or "").strip()) if x]
            nums = [int(c) for x in parts for c in (x if len(x) > 1 and set(x) <= set("1234") else [x])]
        except ValueError:
            return {"ok": False, "message": "槽号要写成 1 到 4 的数字，例如 1,2,3,4"}
        steps = []
        for n in nums or [1, 2, 3, 4]:
            steps += [{"do": "select_slot", "slot": n}] + ([{"do": "autofocus"}] if autofocus else []) + [{"do": "capture"}]
        return self._prepare(steps, note)

    def start_plan(self):
        with self._pending_lock:
            p, self._pending = self._pending, None
        if p is None:
            return {"ok": False, "message": "没有待确认的清单，请先说要做什么（先生成清单并复述给用户）"}
        if self._clock() - p["t"] > PENDING_TTL_S:
            return {"ok": False, "message": "清单是 %d 分钟前生成的，已过期，没有执行；请重新说一遍要做什么" % (PENDING_TTL_S // 60)}
        code, d = self._web_call("POST", "/api/jobs", {"kind": "plan", "steps": p["steps"], "note": p["note"]})
        if code != 200:
            return {"ok": False, "message": "没有开始：%s" % (d.get("error") or "网页后端返回 %s" % code)}
        return {"ok": True, "started": True, "job_id": d.get("job_id"),
                "message": "已开始任务 %s，共 %d 步，在后台进行。说完这句我就回到待机，不再听；"
                           "完成后显微镜屏幕和网页会提示，要查进度或停止任务请重新唤醒我。"
                           % (d.get("job_id"), len(p["steps"]))}

    def job_status(self):
        code, d = self._web_call("GET", "/api/jobs?limit=1")
        if code != 200:
            return {"ok": False, "message": "查不到任务状态：%s" % (d.get("error") or code)}
        jobs = d.get("jobs") or []
        if not jobs:
            return {"ok": True, "running": False, "message": "还没有做过自动任务。"}
        j = jobs[0]
        running = d.get("active") == j.get("job_id")
        recs = j.get("records")
        n = len(recs) if isinstance(recs, list) else None
        msg = "任务 %s %s。" % (j.get("job_id"), "正在进行：%s" % (j.get("progress") or "") if running
                                else "已结束（%s）%s" % (j.get("status"), "：" + j["error"] if j.get("error") else ""))
        if n is not None:
            msg += "已拍 %d 张。" % n
        return {"ok": True, "running": running, "job_id": j.get("job_id"), "status": j.get("status"),
                "progress": j.get("progress"), "photos": n, "message": msg}

    def stop_job(self):
        with self._pending_lock:
            self._pending = None
        code, d = self._web_call("POST", "/api/jobs/cancel", {})
        if code != 200:
            return {"ok": False, "message": d.get("error") or "没有正在进行的自动任务"}
        if d.get("motor_stopped"):
            return {"ok": True, "message": "已停止任务 %s，电机已立即停下，后面的步骤不再执行。要继续用显微镜，先在网页或面板上点“复位”。" % d.get("job_id")}
        return {"ok": True, "message": "已停止任务 %s，后面的步骤不再执行。" % d.get("job_id")}

    def status(self):
        self._ensure()
        if not self._online():
            return {"online": False, "message": "显微镜控制板离线（超过 5 秒没有状态）"}
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
