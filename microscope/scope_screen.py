# -*- coding: utf-8 -*-
"""显微服务的屏幕显示：把预览帧画到 MaixCAM2 自带屏幕上，左上角“退出”按钮。

只在 scope_service.py --screen 时使用（由“显微镜”app 启动）。相机仍由 ScopeService 在主线程读取，
这里只负责显示和读触摸，不碰相机。
"""
import time

FONT_PATH = "/maixapp/share/font/SourceHanSansCN-Regular.otf"
EXIT_BOX = (0, 0, 120, 64)        # 屏幕坐标：x, y, w, h


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
