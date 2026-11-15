# -*- coding: utf-8 -*-
"""显微服务的屏幕显示：把预览帧画到 MaixCAM2 自带屏幕上，左上角“退出”按钮。

只在 scope_service.py --screen 时使用（由“显微镜”app 启动）。相机仍由 ScopeService 在主线程读取，
这里只负责显示和读触摸，不碰相机。

小智对话弹窗：小智服务把弹窗画成 /root/xiaozhi/popup.rgba（640×480 RGBA）。桌面靠注入库叠加，
本 app 不带注入库（会卡显示驱动），所以这里自己读这张图叠到屏幕画面上；弹窗出现时触摸转给小智
（写 /tmp/xz_cmd 的 TAP，点 X 关闭、点喇叭静音），不触发“退出”。网页预览不经过这里，不受影响。
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
        x, y, w, h = EXIT_BOX
        self._text(img, x + 18, y + 16, "退出", image.Color.from_rgb(255, 255, 255))
        self.disp.show(img)
