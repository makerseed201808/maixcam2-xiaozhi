#!/usr/bin/env python3
"""开发方案 2.1：摸清 MaixCAM2 相机能力。在 MaixCAM2 上用 /usr/bin/python3 运行。

全程持有小智的相机占用锁（/run/maixcam2-camera.lock）；相机被占用时直接退出，不抢占。
输出：传感器名、传感器尺寸、可打开的分辨率与实际帧率、自动/锁定曝光读回值，样张存到 --out 目录。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/root/xiaozhi")
from camera_ownership import CameraLease  # noqa: E402


def frames(cam, n):
    t = time.time()
    img = None
    for _ in range(n):
        img = cam.read()
    return img, n / (time.time() - t)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/root/scope_probe")
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rep = {"t": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        lease = CameraLease().acquire()
    except RuntimeError as exc:
        sys.exit("相机正被占用，退出：%s" % exc)
    try:
        from maix import camera, image
        rep["device_name"] = camera.get_device_name()
        try:
            rep["devices"] = camera.list_devices()
        except Exception as exc:
            rep["devices"] = repr(exc)
        rep["sensor_size"] = list(camera.get_sensor_size())
        sw, sh = rep["sensor_size"]
        rep["modes"] = []
        for (w, h) in [(640, 480), (1280, 720), (1920, 1080), (2560, 1440), (sw, sh)]:
            r = {"req": [w, h]}
            cam = None
            try:
                cam = camera.Camera(w, h)
                r["got"] = [cam.width(), cam.height()]
                r["fps_cfg"] = cam.fps()
                img, fps = frames(cam, 40)
                r["fps_meas"] = round(fps, 1)
                r["auto_exposure_us"] = cam.exposure()
                r["auto_gain"] = cam.gain()
                path = os.path.join(a.out, "auto_%dx%d.jpg" % (img.width(), img.height()))
                img.save(path, quality=95)
                r["file"] = path
                r["bytes"] = os.path.getsize(path)
            except Exception as exc:
                r["error"] = repr(exc)
            finally:
                if cam is not None:
                    cam.close()
            rep["modes"].append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
        # 锁定曝光与增益：自动收敛后读出，切手动写回，再读 30 帧检查是否保持
        best = max((m for m in rep["modes"] if "got" in m), key=lambda m: m["got"][0] * m["got"][1])
        w, h = best["got"]
        cam = camera.Camera(w, h)
        try:
            frames(cam, 40)
            e0, g0 = cam.exposure(), cam.gain()
            cam.exp_mode(camera.AeMode.Manual)
            e1, g1 = cam.exposure(e0), cam.gain(g0)
            seq = []
            for _ in range(30):
                cam.read()
                seq.append((cam.exposure(), cam.gain()))
            img = cam.read()
            img.save(os.path.join(a.out, "locked_%dx%d.jpg" % (w, h)), quality=95)
            # 改变曝光看是否生效（亮度应变化）
            cam.exposure(max(1, e0 // 2))
            frames(cam, 10)
            dim = cam.read()
            dim.save(os.path.join(a.out, "locked_half_exposure_%dx%d.jpg" % (w, h)), quality=95)
            rep["lock"] = {"res": [w, h], "auto_exposure_us": e0, "auto_gain": g0, "set_return": [e1, g1],
                           "after_30_frames": seq[-1], "stable": len(set(seq)) == 1,
                           "mode_now": str(cam.exp_mode()), "half_exposure_readback": cam.exposure()}
        finally:
            cam.close()
        print(json.dumps(rep.get("lock"), ensure_ascii=False), flush=True)
    finally:
        lease.release()
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print("报告：", os.path.join(a.out, "report.json"))


if __name__ == "__main__":
    main()
