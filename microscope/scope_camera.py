"""显微相机会话（开发方案 2.2）。

显微功能期间由本服务独占相机：先拿小智的相机占用锁（/run/maixcam2-camera.lock），
再打开相机；预览、清晰度、拍原图都从同一个会话取帧，不再在多个程序之间交接相机。
空闲 idle_s 秒（无清晰度请求、无拍照、无预览观看）后关闭相机并释放锁，小智拍照即可恢复使用。

两种后端：
  - MaixBackend：MaixCAM2 上的 maix.camera；
  - FakeBackend：电脑上测试用，按“当前焦距”生成清晰/模糊的假图。
"""
import threading
import time

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class CameraBusy(RuntimeError):
    pass


class FakeBackend(object):
    """假相机：focus_fn() 返回 0–1 的清晰程度，1 最清晰。"""

    def __init__(self, width=640, height=480, focus_fn=None, seed=0):
        self.width, self.height = width, height
        self.focus_fn = focus_fn or (lambda: 1.0)
        self.frame_no = 0
        rng = np.random.RandomState(seed)
        self.base = ((rng.rand(height // 8, width // 8) > 0.5) * 255).astype(np.float32)
        self.base = np.kron(self.base, np.ones((8, 8), np.float32))
        self.opened = False
        self.locked = None

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def read(self):
        self.frame_no += 1
        k = max(0, int(round((1.0 - self.focus_fn()) * 12)))
        a = self.base.copy()
        for _ in range(k):
            a = (a + np.roll(a, 1, 0) + np.roll(a, -1, 0) + np.roll(a, 1, 1) + np.roll(a, -1, 1)) / 5
        return np.stack([a, a, a], -1).astype(np.uint8)

    def drop_buffered(self):
        pass

    def read_full(self):
        return self.read()

    def lock_exposure(self):
        self.locked = (10000, 1024)
        return self.locked

    def unlock_exposure(self):
        self.locked = None

    def exposure(self):
        return self.locked or (10000, 1024)

    def save_jpeg(self, frame, path, quality=95):
        from PIL import Image
        Image.fromarray(frame).save(path, quality=quality)
        return frame.shape[1], frame.shape[0]

    def to_jpeg_bytes(self, frame, quality=75, max_width=None):
        import io
        from PIL import Image
        im = Image.fromarray(frame)
        if max_width and im.width > max_width:
            im = im.resize((max_width, im.height * max_width // im.width))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    def to_array(self, frame, max_width=640):
        return frame


class MaixBackend(object):
    """MaixCAM2 相机（传感器 ov_os04d10，2560×1440）。2026-11-10 实测见 docs/camera.md：

    - RGB888 最大只到 1920×1080 以下，且在 1920×1080 打开会失败；全分辨率必须用 YVU420SP（26.9 帧/秒）。
      曾试 BGR888 高分辨率时进程卡死、被强制结束后相机“vi init failed”，只能重启 MaixCAM2 恢复：
      本类只用 YVU420SP，且不要在相机打开过程中强杀进程。
    - clear_buff() 不受支持（只打印警告），取新帧靠连续读掉缓冲帧；
    - 主通道 2560×1440 只用于拍原图；add_channel 开一个硬件缩放的 1280×720 通道给预览和清晰度
      （读 33 ms、转 JPEG 20 ms、转灰度 5 ms；软件把全图缩到 1280×720 要 374 ms）；
    - 锁定曝光：切手动模式并写回当前曝光时间即可保持亮度；写回 gain() 会改变增益（单位不一致），不写。
    """

    def __init__(self, width=2560, height=1440, buff_num=2, preview=(1280, 720)):
        self.width, self.height, self.buff_num = width, height, buff_num
        self.preview = preview
        self.cam = None
        self.ch = None

    def open(self):
        from maix import camera, image
        self._camera, self._image = camera, image
        self.cam = camera.Camera(self.width, self.height, image.Format.FMT_YVU420SP, buff_num=self.buff_num)
        self.ch = self.cam.add_channel(self.preview[0], self.preview[1], image.Format.FMT_YVU420SP,
                                       buff_num=self.buff_num)
        self.cam.exp_mode(camera.AeMode.Auto)
        self.cam.awb_mode(camera.AwbMode.Auto)
        t = time.monotonic()
        n = 0
        while n < 30 or time.monotonic() - t < 2.0:   # 与小智一致：至少 30 帧且 2 秒，让自动曝光收敛
            self.ch.read()
            n += 1
            if time.monotonic() - t > 6:
                break

    def close(self):
        if self.ch is not None:
            try:
                self.ch.close()
            finally:
                self.ch = None
        if self.cam is not None:
            self.cam.close()
            self.cam = None

    def read(self):
        """预览/清晰度通道（1280×720）。"""
        img = self.ch.read()
        if img is None:
            raise RuntimeError("相机没有返回画面")
        return img

    def read_full(self):
        """主通道全分辨率，拍原图用；先读掉缓冲里的旧帧。"""
        for _ in range(self.buff_num + 1):
            self.cam.read()
        img = self.cam.read()
        if img is None:
            raise RuntimeError("相机没有返回画面")
        return img

    def drop_buffered(self):
        for _ in range(self.buff_num + 1):
            self.ch.read()

    def lock_exposure(self):
        e = self.cam.exposure()
        self.cam.exp_mode(self._camera.AeMode.Manual)
        self.cam.exposure(e)
        return e, self.cam.gain()

    def unlock_exposure(self):
        self.cam.exp_mode(self._camera.AeMode.Auto)

    def exposure(self):
        return self.cam.exposure(), self.cam.gain()

    def save_jpeg(self, frame, path, quality=95):
        if int(frame.save(path, quality=quality)) != 0:
            raise RuntimeError("JPEG 保存失败")
        return frame.width(), frame.height()

    def to_jpeg_bytes(self, frame, quality=75, max_width=None):
        img = frame
        if max_width and frame.width() > max_width:
            img = frame.resize(max_width, frame.height() * max_width // frame.width())
        enc = img.to_jpeg(quality=quality)
        return bytes(enc.to_bytes(copy=True))

    def to_array(self, frame, max_width=None):
        """清晰度用灰度图：预览通道 YUV → 灰度（Y 平面）→ numpy（H×W×1），不再缩放。"""
        g = frame.to_format(self._image.Format.FMT_GRAYSCALE)
        return self._image.image2cv(g, ensure_bgr=False, copy=True)


class CameraSession(object):
    """线程安全的相机会话：用时自动打开（拿锁），空闲后自动关闭（放锁）。"""

    def __init__(self, backend, lease_factory=None, idle_s=20.0, clock=time.monotonic, lease_wait_s=8.0):
        self.backend = backend
        self.lease_wait_s = lease_wait_s
        self.lease_factory = lease_factory
        self.idle_s = idle_s
        self.clock = clock
        self._lock = threading.RLock()
        self._lease = None
        self._last_use = 0.0
        self.is_open = False
        self.frame_no = 0
        self.exposure_locked = None
        self.open_count = 0

    def _ensure_open(self):
        if self.is_open:
            return
        if self.lease_factory is not None:
            # 排队：小智拍一张照约 3 秒，最多等 lease_wait_s 秒再报“被占用”
            end = time.monotonic() + self.lease_wait_s
            while True:
                try:
                    self._lease = self.lease_factory().acquire()
                    break
                except RuntimeError as exc:
                    if time.monotonic() >= end:
                        raise CameraBusy("相机正被其他程序占用，已等 %.0f 秒（%s）" % (self.lease_wait_s, exc))
                    time.sleep(0.3)
        try:
            self.backend.open()
        except Exception:
            self._release_lease()
            raise
        self.is_open = True
        self.open_count += 1

    def _release_lease(self):
        if self._lease is not None:
            try:
                self._lease.release()
            finally:
                self._lease = None

    def touch(self):
        self._last_use = self.clock()

    def full_frame(self):
        """全分辨率帧（拍原图）。"""
        with self._lock:
            self._ensure_open()
            self.touch()
            img = self.backend.read_full()
            self.frame_no += 1
            return img

    def frame(self, fresh=True):
        """取一帧；fresh=True 时先丢弃缓冲中的旧帧，保证画面是请求之后曝光的。"""
        with self._lock:
            self._ensure_open()
            self.touch()
            if fresh:
                self.backend.drop_buffered()
            img = self.backend.read()
            self.frame_no += 1
            return img

    def lock_exposure(self):
        with self._lock:
            self._ensure_open()
            self.exposure_locked = self.backend.lock_exposure()
            return self.exposure_locked

    def unlock_exposure(self):
        with self._lock:
            if self.is_open and self.exposure_locked:
                self.backend.unlock_exposure()
            self.exposure_locked = None

    def close_if_idle(self):
        with self._lock:
            if self.is_open and self.clock() - self._last_use > self.idle_s:
                self.close()
                return True
        return False

    def close(self):
        with self._lock:
            if self.is_open:
                try:
                    self.backend.close()
                finally:
                    self.is_open = False
                    self.exposure_locked = None
                    self._release_lease()
