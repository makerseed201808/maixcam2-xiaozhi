# 2.1 补充探测（2026-11-10 实测用）。注意：probe_camera2 中 BGR888 高分辨率一项曾导致相机卡死，需重启 MaixCAM2 恢复，勿再运行该项。
import sys, time, json
sys.path.insert(0, "/root/xiaozhi")
from camera_ownership import CameraLease
lease = CameraLease().acquire()
out = {}
try:
    from maix import camera, image
    def bright(img):
        g = img.to_format(image.Format.FMT_GRAYSCALE) if img.format() != image.Format.FMT_GRAYSCALE else img
        h = g.get_histogram()
        return round(h.get_statistics().l_mean(), 1)
    # 1. 曝光锁定方式对比（1280x720 RGB888）
    for variant in ("A_manual_only", "B_manual_plus_exposure", "C_manual_exposure_gain"):
        cam = camera.Camera(1280, 720)
        try:
            for _ in range(40): cam.read()
            e0, g0 = cam.exposure(), cam.gain(); b0 = bright(cam.read())
            cam.exp_mode(camera.AeMode.Manual)
            if variant != "A_manual_only": cam.exposure(e0)
            if variant == "C_manual_exposure_gain": cam.gain(g0)
            for _ in range(15): cam.read()
            vals = [bright(cam.read()) for _ in range(5)]
            out[variant] = {"auto": [e0, g0, b0], "locked": [cam.exposure(), cam.gain()], "bright": vals}
        finally:
            cam.close()
        print(variant, out[variant], flush=True)
    # 2. 更高分辨率：换像素格式
    for fmt in ("FMT_YVU420SP",):   # BGR888/RGB888 高分辨率会卡死相机，已移除
        for (w, h) in [(1920, 1080), (2560, 1440)]:
            key = "%s_%dx%d" % (fmt, w, h)
            cam = None
            try:
                cam = camera.Camera(w, h, getattr(image.Format, fmt))
                t = time.time()
                for _ in range(30): img = cam.read()
                fps = 30 / (time.time() - t)
                t = time.time(); p = "/root/scope_probe/%s.jpg" % key
                r = img.save(p, quality=95); ts = time.time() - t
                out[key] = {"got": [img.width(), img.height()], "fmt": str(img.format()), "fps": round(fps, 1), "save_ret": int(r), "save_s": round(ts, 3)}
            except Exception as exc:
                out[key] = {"error": repr(exc)[:160]}
            finally:
                if cam is not None: cam.close()
            print(key, out[key], flush=True)
finally:
    lease.release()
json.dump(out, open("/root/scope_probe/report2.json", "w"), ensure_ascii=False, indent=1)
