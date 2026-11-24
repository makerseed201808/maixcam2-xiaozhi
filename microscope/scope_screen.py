# -*- coding: utf-8 -*-
"""显微服务的屏幕显示：把预览帧画到 MaixCAM2 自带屏幕上，左上角“退出”按钮。

只在 scope_service.py --screen 时使用（由“显微镜”app 启动）。相机仍由 ScopeService 在主线程读取，
这里只负责显示和读触摸，不碰相机。

小智对话弹窗：小智服务把弹窗画成 /root/xiaozhi/popup.rgba（640×480 RGBA）。桌面靠注入库叠加，
本 app 不带注入库（会卡显示驱动），所以这里自己读这张图叠到屏幕画面上；弹窗出现时触摸转给小智
（写 /tmp/xz_cmd 的 TAP，点 X 关闭、点喇叭静音），不触发“退出”。网页预览不经过这里，不受影响。

网址二维码（2026-11-15 快速配网）：右上角“网址”按钮。打开 app 后先显示 30 秒，之后点按钮显示 / 隐藏。
网址和二维码模块从本机网页后端 /api/network/qr.json 取（换网络后地址变了也会跟着变）。
"""
import os
import struct
import time

FONT_PATH = "/maixapp/share/font/SourceHanSansCN-Regular.otf"
EXIT_BOX = (0, 0, 120, 64)        # 屏幕坐标：x, y, w, h
POPUP_PATH = "/root/xiaozhi/popup.rgba"
JOB_PATH = "/tmp/microscope_job.json"   # 网页后端自动任务写的状态（编排任务 / 一键巡检）
JOB_END_SHOW_S = 30                     # 任务结束后提示显示多久
XZ_CMD = "/tmp/xz_cmd"
QR_API = "http://127.0.0.1:8000/api/network/qr.json"
QR_BOX_W = 120                          # 右上角“网址”按钮宽度（高度同“退出”）
QR_SHOW_FIRST_S = 30


def screen_to_raw_touch(x, y):
    """屏幕坐标 → 小智 TAP 用的触摸原始坐标（竖屏 480×640）；与小智 overlay_touch_to_screen 互逆。"""
    return 479 - int(y), int(x)


class XiaozhiPopup(object):
    """读小智弹窗图层；文件有变化才重读。visible 为 False 时不叠加、不花时间。"""

    def __init__(self, image_mod, path=POPUP_PATH):
        self._image, self.path = image_mod, path
        self._sig = None
        self.img = None

    @property
    def visible(self):
        return self.img is not None

    def refresh(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self._sig, self.img = None, None
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        try:
            import numpy as np
            with open(self.path, "rb") as f:
                raw = f.read()
            n, w, h = struct.unpack("<III", raw[:12])
            if n < 1 or w <= 0 or h <= 0 or len(raw) < 12 + w * h * 4:
                return                                  # 小智还没写完，下次再读
            a = np.frombuffer(raw, np.uint8, count=w * h * 4, offset=12).reshape(h, w, 4)
            self.img = self._image.cv2image(np.ascontiguousarray(a), bgr=False, copy=True) if a[:, :, 3].any() else None
            self._sig = sig
        except Exception:
            self.img = None

    def blend(self, img):
        """img 为屏幕尺寸的 RGB888；转 RGBA 叠加再转回（板上实测约 25 ms/帧，只在弹窗显示时做）。"""
        image = self._image
        if self.img.width() != img.width() or self.img.height() != img.height():
            return img
        x = img.to_format(image.Format.FMT_RGBA8888)
        x.draw_image(0, 0, self.img)
        return x.to_format(image.Format.FMT_RGB888)


class JobBanner(object):
    """读自动任务状态文件，给出屏幕底部一行提示 (文字, 颜色名)；没有要显示的返回 None。
    进行中：一直显示进度；结束（完成 / 失败 / 已停止）：显示 JOB_END_SHOW_S 秒。"""

    END = {"done": ("自动任务完成", "ok"), "failed": ("自动任务失败", "bad"),
           "cancelled": ("自动任务已停止", "warn"), "interrupted": ("自动任务中断", "warn")}

    def __init__(self, path=JOB_PATH, clock=time.time):
        self.path, self.clock = path, clock
        self._sig, self.job = None, None

    def refresh(self):
        try:
            st = os.stat(self.path)
        except OSError:
            self._sig, self.job = None, None
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        try:
            import json
            with open(self.path, encoding="utf-8") as f:
                self.job = json.load(f)
            self._sig = sig
        except (OSError, ValueError):
            pass                                    # 正在写，下次再读

    def line(self):
        j = self.job
        if not j:
            return None
        photos = "已拍 %s/%s 张" % (j.get("photos", 0), j.get("total") or "?")
        if j.get("status") in ("running", "waiting"):
            return "自动任务  %s  %s" % (j.get("progress") or "", photos), "run"
        if j.get("status") in self.END and j.get("ended_at") and self.clock() - j["ended_at"] < JOB_END_SHOW_S:
            title, color = self.END[j["status"]]
            tail = ("：" + j["error"]) if j.get("error") else ""
            return ("%s  %s%s" % (title, photos, tail))[:60], color
        return None


def _fetch_json(url, timeout=3):
    import json
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机地址不走代理
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class WebQr(object):
    """网页网址与二维码：后台每 period 秒向本机网页后端取一次；visible 控制是否画在屏幕上。"""

    def __init__(self, fetch=_fetch_json, url=QR_API, period=15.0, clock=time.monotonic):
        self.fetch, self.api, self.period, self.clock = fetch, url, period, clock
        self.url, self.rows = None, []
        self.version = 0                  # 网址变了加 1，屏幕据此重画二维码
        self.shown_until = clock() + QR_SHOW_FIRST_S
        self.forced = None                # 用户点了按钮：True / False；None = 开头自动显示
        self._t = None

    @property
    def visible(self):
        if self.forced is not None:
            return self.forced and bool(self.rows)
        return bool(self.rows) and self.clock() < self.shown_until

    def toggle(self):
        self.forced = not self.visible

    def refresh(self):
        """取一次；网页后端没起来时保持旧值。返回网址是否变化。"""
        try:
            d = self.fetch(self.api)
        except Exception:
            return False
        url, rows = d.get("url"), d.get("rows") or []
        if not url or not rows or any(len(r) != len(rows) for r in rows):
            return False
        if url == self.url:
            return False
        self.url, self.rows = url, rows
        self.version += 1
        return True

    def start(self):
        import threading

        def loop():
            while True:
                self.refresh()
                time.sleep(self.period)
        self._t = threading.Thread(target=loop, name="web-qr", daemon=True)
        self._t.start()

    def layout(self, w, h, top=72, margin=16):
        """二维码画在右侧：返回 (x, y, 每格像素, 含白边的边长)。"""
        n = len(self.rows)
        scale = max(2, min(9, (min(h - top - 64, w // 2) // (n + 8))))
        size = (n + 8) * scale
        return w - size - margin, top, scale, size


class ScreenView(object):
    def __init__(self, title="显微镜"):
        from maix import app, display, image, touchscreen
        self._app, self._image = app, image
        self.disp = display.Display()
        self.ts = touchscreen.TouchScreen()
        self.w, self.h = self.disp.width(), self.disp.height()
        self.title = title
        self.font = None
        try:
            image.load_font("scope_cn", FONT_PATH, size=24)
            self.font = "scope_cn"
        except Exception:
            pass
        self._exit = False
        self.popup = XiaozhiPopup(image)
        self.jobs = JobBanner()
        self._touch = None            # 弹窗显示时按下的位置（抬起时转给小智）
        self._last_fps_t = time.monotonic()
        self._frames = 0
        self.fps = 0.0
        self.note = ""
        self.qr = WebQr()
        self.qr.start()
        self._qr_img = None
        self._qr_ver = -1
        self._was_pressed = False

    def _qr_image(self, scale, size):
        """二维码图层只在网址变化时重画一次（几百个小方块），之后每帧直接贴图。"""
        image = self._image
        if self._qr_img is None or self._qr_ver != self.qr.version:
            img = image.Image(size, size, image.Format.FMT_RGB888)
            img.draw_rect(0, 0, size, size, image.Color.from_rgb(255, 255, 255), thickness=-1)
            black = image.Color.from_rgb(0, 0, 0)
            for r, row in enumerate(self.qr.rows):
                c = 0
                while c < len(row):                     # 同一行连续的黑格合成一个矩形
                    if row[c] == "1":
                        c0 = c
                        while c < len(row) and row[c] == "1":
                            c += 1
                        img.draw_rect((c0 + 4) * scale, (r + 4) * scale, (c - c0) * scale, scale, black, thickness=-1)
                    else:
                        c += 1
            self._qr_img, self._qr_ver = img, self.qr.version
        return self._qr_img

    def _draw_qr(self, img):
        image = self._image
        white, dark = image.Color.from_rgb(255, 255, 255), image.Color.from_rgb(0, 0, 0)
        bx = self.w - QR_BOX_W
        x, y, w, h = EXIT_BOX
        img.draw_rect(bx, y, QR_BOX_W, h, image.Color.from_rgb(20, 70, 140) if self.qr.visible else dark, thickness=-1)
        self._text(img, bx + 18, y + 16, "网址", white)
        if not self.qr.visible:
            return
        qx, qy, scale, size = self.qr.layout(self.w, self.h)
        img.draw_image(qx, qy, self._qr_image(scale, size))
        label = self.qr.url.replace("http://", "").rstrip("/")
        img.draw_rect(qx, qy + size, size, 40, dark, thickness=-1)
        self._text(img, qx + 8, qy + size + 6, label, white)

    def _text(self, img, x, y, s, color):
        if self.font:
            img.draw_string(x, y, s, color, font=self.font)
        else:
            img.draw_string(x, y, s, color, scale=1.5)

    def show(self, frame):
        image = self._image
        img = frame
        if img.format() != image.Format.FMT_RGB888:
            # 正常由相机 RGB888 屏幕通道供图；万一拿到 YUV（板上不能直接转 RGB），退回灰度画面
            img = img.to_format(image.Format.FMT_GRAYSCALE).to_format(image.Format.FMT_RGB888)
        img = img.resize(self.w, self.h, image.Fit.FIT_CONTAIN)
        self.popup.refresh()
        if self.popup.visible:
            try:
                self.disp.show(self.popup.blend(img))  # 小智对话中：画面上叠对话框，不画“退出”
                return
            except Exception:
                pass
        white, dark = image.Color.from_rgb(255, 255, 255), image.Color.from_rgb(0, 0, 0)
        x, y, w, h = EXIT_BOX
        img.draw_rect(x, y, w, h, dark, thickness=-1)
        self._text(img, x + 18, y + 16, "退出", white)
        self._frames += 1
        now = time.monotonic()
        if now - self._last_fps_t >= 1.0:
            self.fps = self._frames / (now - self._last_fps_t)
            self._frames, self._last_fps_t = 0, now
        info = "%s  %.0f 帧/秒  %s" % (self.title, self.fps, self.note)
        self._text(img, x + w + 12, y + 16, info, white)
        try:
            self._draw_qr(img)
        except Exception:
            pass
        try:
            self.jobs.refresh()
            b = self.jobs.line()
            if b:                                   # 底部一行：进行中蓝色，完成绿色，失败红色，停止灰色
                text, kind = b
                bg = {"run": (20, 70, 140), "ok": (20, 110, 50), "bad": (160, 30, 30)}.get(kind, (80, 80, 80))
                img.draw_rect(0, self.h - 48, self.w, 48, image.Color.from_rgb(*bg), thickness=-1)
                self._text(img, 12, self.h - 40, text, white)
        except Exception:
            pass
        self.disp.show(img)

    def should_exit(self):
        if self._exit:
            return True
        try:
            if self._app.need_exit():
                self._exit = True
        except Exception:
            pass
        try:
            px, py, pressed = self.ts.read()
            if self.popup.visible or self._touch is not None:
                # 小智对话框在屏幕上：触摸交给小智（抬起时发 TAP），不当作“退出”
                if pressed:
                    self._touch = (px, py)
                elif self._touch is not None:
                    rx, ry = screen_to_raw_touch(*self._touch)
                    self._touch = None
                    with open(XZ_CMD, "a") as f:
                        f.write("TAP %d %d\n" % (rx, ry))
                return self._exit
            x, y, w, h = EXIT_BOX
            if pressed and x <= px < x + w and y <= py < y + h:
                self._exit = True
            if pressed and not self._was_pressed and px >= self.w - QR_BOX_W and y <= py < y + h:
                self.qr.toggle()                    # 按下的一瞬间切换一次，按住不连跳
            self._was_pressed = bool(pressed)
        except Exception:
            pass
        return self._exit

    def close(self):
        """释放触摸与显示：Display.close() 不会销毁私有 CMM 内存池，需置空后 gc（siot_monitor 实测）。"""
        try:
            self.ts.close()
        except Exception:
            pass
        self.ts = None
        self.disp = None
        import gc
        gc.collect()

    def message(self, s):
        """无画面时（相机被占用等）在屏幕中央显示一行字。"""
        image = self._image
        img = image.Image(self.w, self.h, image.Format.FMT_RGB888)
        self._text(img, 20, self.h // 2 - 12, s, image.Color.from_rgb(255, 255, 255))
        if self.qr.url:
            self._text(img, 20, self.h // 2 + 24, "网页：" + self.qr.url, image.Color.from_rgb(255, 255, 255))
        x, y, w, h = EXIT_BOX
        self._text(img, x + 18, y + 16, "退出", image.Color.from_rgb(255, 255, 255))
        self.disp.show(img)
