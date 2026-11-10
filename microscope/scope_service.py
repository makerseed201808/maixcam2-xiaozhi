#!/usr/bin/env python3
"""MaixCAM2 显微服务：清晰度（2.3）、原图采集（2.2）、原图取回与投影画面（2.4、2.5）。

  /usr/bin/python3 /root/microscope/scope_service.py            # 在 MaixCAM2 上运行
SIoT 账号读 /etc/siot/monitor.json（与小智相同，密码不进仓库）。

SIoT（主题与格式见 smart-microscope/docs/protocol.md）：
  siot/scope_focus_req  {"seq": 41, "z": 5120}
  siot/scope_focus      {"seq": 41, "score": 183.6, "frame": 9921, "ms": 230, "t": ...}  或 {"seq": 41, "error": "..."}
  siot/scope_capture    请求 {"type": "request", "id": "q-1", "record": "R-…", "sample": "S-01", "slot": 2, "note": "…"}
                        结果 {"type": "result", "id": "q-1", "record": "R-…", "capture_id": "cap-000123",
                              "url": "http://<地址>:8091/scope/cap-000123.jpg", "sha256": "…", "width": .., "height": .., "t": ..}
                        失败 {"type": "error", "id": "q-1", "record": "R-…", "reason": "…"}
HTTP（端口 8091）：
  /scope/<capture_id>.jpg   无框原图；/scope/<capture_id>.json 元数据
  /preview.jpg              最新预览帧（缩放到 preview_width）；/stream.mjpg 连续预览；/status.json
有人看预览时相机保持打开；预览、清晰度、拍照共用同一个相机会话。
"""
import argparse
import hashlib
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scope_camera import CameraBusy, CameraSession  # noqa: E402
from sharpness import DEFAULT_ROI, laplacian_var, median  # noqa: E402

TOPICS = {"focus_req": "siot/scope_focus_req", "focus": "siot/scope_focus", "capture": "siot/scope_capture"}
CAP_RE = re.compile(r"^cap-\d{6}$")


class CaptureStore(object):
    """原图目录：cap-000001.jpg + .json；编号持久化在 next_id 文件。"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._lock = threading.Lock()

    def next_id(self):
        with self._lock:
            path = os.path.join(self.root, "next_id")
            n = 1
            if os.path.exists(path):
                with open(path) as f:
                    n = int(f.read().strip() or 1)
            existing = [int(x[4:10]) for x in os.listdir(self.root) if CAP_RE.match(x[:10])]
            n = max([n] + [e + 1 for e in existing])
            with open(path + ".tmp", "w") as f:
                f.write(str(n + 1))
            os.replace(path + ".tmp", path)
            return "cap-%06d" % n

    def path(self, cid, ext):
        if not CAP_RE.match(cid):
            raise ValueError("bad capture id")
        return os.path.join(self.root, cid + ext)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def lan_address(fallback="127.0.0.1"):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.50.1", 9))
        a = s.getsockname()[0]
        s.close()
        return a
    except Exception:
        return fallback


class ScopeService(object):
    def __init__(self, session, store, publish=None, http_base=None, roi=DEFAULT_ROI, samples=3,
                 preview_width=1280, analysis_width=640, log=print):
        self.session = session
        self.store = store
        self.publish = publish or (lambda topic, obj: None)
        self.http_base = http_base
        self.roi = roi
        self.samples = samples
        self.preview_width = preview_width
        self.analysis_width = analysis_width
        self.log = log
        self._work = threading.Lock()          # 清晰度和拍照串行执行
        self._preview = None                   # (jpeg, frame_no, t)
        self._preview_cv = threading.Condition()
        self._viewers = 0
        self.stats = {"focus": 0, "focus_err": 0, "capture": 0, "capture_err": 0, "last_focus_ms": None}
        self._focus_session_t = 0.0
        self.jobs = queue.Queue()
        self.stop_file = None

    # ---------- 清晰度 ----------
    def handle_focus(self, msg):
        seq = msg.get("seq")
        t0 = time.time()
        try:
            with self._work:
                # 一轮对焦期间锁定曝光和增益，避免亮度变化干扰清晰度比较；10 s 无请求视为新一轮
                if self.session.exposure_locked is None or time.time() - self._focus_session_t > 10:
                    self.session.unlock_exposure()
                    self.session.frame(fresh=True)
                    self.session.lock_exposure()
                self._focus_session_t = time.time()
                scores, frame_no = [], None
                for _ in range(self.samples):
                    img = self.session.frame(fresh=True)
                    frame_no = self.session.frame_no
                    arr = self.session.backend.to_array(img, self.analysis_width)
                    scores.append(laplacian_var(arr, self.roi))
                    self._set_preview(img, frame_no)
            score = median(scores)
            ms = int((time.time() - t0) * 1000)
            self.stats["focus"] += 1
            self.stats["last_focus_ms"] = ms
            reply = {"seq": seq, "score": round(score, 2), "frame": frame_no, "ms": ms,
                     "samples": [round(s, 2) for s in scores], "t": round(time.time(), 3)}
        except Exception as exc:
            self.stats["focus_err"] += 1
            reply = {"seq": seq, "error": str(exc), "t": round(time.time(), 3)}
        self.publish(TOPICS["focus"], reply)
        return reply

    # ---------- 拍原图 ----------
    def handle_capture(self, msg):
        if msg.get("type") != "request":
            return None
        base = {"id": msg.get("id"), "record": msg.get("record")}
        try:
            with self._work:
                self.session.unlock_exposure()      # 拍照用自动曝光
                img = self.session.full_frame()
                cid = self.store.next_id()
                path = self.store.path(cid, ".jpg")
                w, h = self.session.backend.save_jpeg(img, path, quality=95)
            digest = sha256_file(path)
            meta = dict(base, type="result", capture_id=cid, sha256=digest, width=w, height=h,
                        bytes=os.path.getsize(path), frame=self.session.frame_no,
                        exposure=list(self.session.backend.exposure()),
                        sample=msg.get("sample"), slot=msg.get("slot"), note=msg.get("note"),
                        t=round(time.time(), 3))
            if self.http_base:
                meta["url"] = "%s/scope/%s.jpg" % (self.http_base, cid)
            with open(self.store.path(cid, ".json"), "w") as f:
                json.dump(meta, f, ensure_ascii=False)
            self.stats["capture"] += 1
            self.publish(TOPICS["capture"], meta)
            self.log("[拍照] %s %dx%d %s" % (cid, w, h, digest[:12]))
            return meta
        except Exception as exc:
            self.stats["capture_err"] += 1
            err = dict(base, type="error", reason=str(exc), t=round(time.time(), 3))
            self.publish(TOPICS["capture"], err)
            return err

    # ---------- 预览 ----------
    def _set_preview(self, img, frame_no):
        try:
            jpeg = self.session.backend.to_jpeg_bytes(img, quality=75, max_width=self.preview_width)
        except Exception:
            return
        with self._preview_cv:
            self._preview = (jpeg, frame_no, time.time())
            self._preview_cv.notify_all()

    def run_jobs(self, stop, fps=8):
        """相机相关的一切都在调用线程（主线程）里做：MaixPy 打开相机要求在主线程。

        有请求先处理请求；空闲时若有人看预览就出预览帧，否则到时关闭相机放锁。
        """
        while not stop.is_set():
            if self.stop_file and os.path.exists(self.stop_file):
                self.log("[显微服务] 收到停止文件，关闭相机后退出")
                os.remove(self.stop_file)
                break
            try:
                kind, data = self.jobs.get(timeout=(1.0 / fps) if self._viewers > 0 else 0.2)
            except queue.Empty:
                kind = None
            if kind == "focus":
                self.handle_focus(data)
            elif kind == "capture":
                self.handle_capture(data)
            elif self._viewers > 0:
                try:
                    img = self.session.frame(fresh=False)
                    self._set_preview(img, self.session.frame_no)
                except CameraBusy:
                    time.sleep(1)
                except Exception as exc:
                    self.log("[预览] %s" % exc)
                    time.sleep(1)
            else:
                self.session.close_if_idle()

    def preview_loop(self, stop, fps=8):
        """有人观看时持续出预览帧；忙于对焦/拍照时让路。"""
        while not stop.is_set():
            if self._viewers > 0 and self._work.acquire(False):
                try:
                    img = self.session.frame(fresh=False)
                    self._set_preview(img, self.session.frame_no)
                except CameraBusy:
                    time.sleep(1)
                except Exception as exc:
                    self.log("[预览] %s" % exc)
                    time.sleep(1)
                finally:
                    self._work.release()
                time.sleep(1.0 / fps)
            else:
                self.session.close_if_idle()
                time.sleep(0.2)

    def status(self):
        p = self._preview
        return {"camera_open": self.session.is_open, "frame": self.session.frame_no,
                "exposure_locked": self.session.exposure_locked, "viewers": self._viewers,
                "preview_age_s": None if p is None else round(time.time() - p[2], 2), "stats": self.stats}


def make_http(service, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            m = re.match(r"^/scope/(cap-\d{6})\.(jpg|json)$", path)
            if m:
                f = service.store.path(m.group(1), "." + m.group(2))
                if not os.path.exists(f):
                    return self._send(404, b"not found", "text/plain")
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), "image/jpeg" if m.group(2) == "jpg" else "application/json")
            if path == "/status.json":
                return self._send(200, json.dumps(service.status()).encode(), "application/json")
            if path == "/preview.jpg":
                service._viewers += 1
                try:
                    with service._preview_cv:
                        service._preview_cv.wait(3)
                        p = service._preview
                finally:
                    service._viewers -= 1
                if p is None:
                    return self._send(503, b"no frame", "text/plain")
                return self._send(200, p[0], "image/jpeg")
            if path == "/stream.mjpg":
                service._viewers += 1
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    last = None
                    while True:
                        with service._preview_cv:
                            service._preview_cv.wait(2)
                            p = service._preview
                        if p is None or p[1] == last:
                            continue
                        last = p[1]
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(p[0]))
                        self.wfile.write(p[0] + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    pass
                finally:
                    service._viewers -= 1
                return
            return self._send(404, b"not found", "text/plain")

    class Server(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server(("0.0.0.0", port), H)


def connect_siot(service, config_path, log=print):
    import paho.mqtt.client as mqtt
    with open(config_path) as f:
        cfg = json.load(f)
    if hasattr(mqtt, "CallbackAPIVersion"):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="scope_cam_%d" % int(time.time()))
    else:
        c = mqtt.Client(client_id="scope_cam_%d" % int(time.time()))
    c.username_pw_set(cfg["username"], cfg["password"])
    c.reconnect_delay_set(1, 5)
    def on_connect(client, u, flags, rc):
        log("[SIoT] 已连接 rc=%s" % rc)
        client.subscribe(TOPICS["focus_req"], qos=0)
        client.subscribe(TOPICS["capture"], qos=1)

    def on_message(client, u, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if msg.topic == TOPICS["focus_req"]:
            service.jobs.put(("focus", data))
        elif msg.topic == TOPICS["capture"] and data.get("type") == "request":
            service.jobs.put(("capture", data))

    c.on_connect = on_connect
    c.on_message = on_message
    service.publish = lambda topic, obj: c.publish(topic, json.dumps(obj, ensure_ascii=False), qos=1)
    c.connect_async(cfg.get("host", "127.0.0.1"), int(cfg.get("port", 1883)), keepalive=15)
    c.loop_start()
    return c


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--width", type=int, default=2560)
    p.add_argument("--height", type=int, default=1440)
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--captures", default="/root/scope_captures")
    p.add_argument("--siot-config", default="/etc/siot/monitor.json")
    p.add_argument("--idle", type=float, default=20.0)
    p.add_argument("--fake", action="store_true", help="电脑上用假相机")
    a = p.parse_args()

    def log(*parts):
        print(time.strftime("%H:%M:%S"), *parts, flush=True)

    if a.fake:
        from scope_camera import FakeBackend
        backend, lease = FakeBackend(), None
    else:
        sys.path.insert(0, "/root/xiaozhi")
        from camera_ownership import CameraLease
        from scope_camera import MaixBackend
        backend, lease = MaixBackend(a.width, a.height), CameraLease
    session = CameraSession(backend, lease_factory=lease, idle_s=a.idle)
    base = "http://%s:%d" % (lan_address(), a.port)
    service = ScopeService(session, CaptureStore(a.captures), http_base=base, log=log)
    service.stop_file = "/tmp/scope_service.stop"   # 停止：touch 这个文件（见 run_scope.sh），保证先关相机再退出
    connect_siot(service, a.siot_config, log)
    stop = threading.Event()
    http = make_http(service, a.port)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    log("[显微服务] HTTP %s ，原图目录 %s，相机 %dx%d" % (base, a.captures, a.width, a.height))
    try:
        service.run_jobs(stop)          # 主线程
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        session.close()
        http.shutdown()


if __name__ == "__main__":
    main()
