import sys, time
sys.path.insert(0, "/root/xiaozhi"); sys.path.insert(0, "/root/microscope")
from camera_ownership import CameraLease
from sharpness import laplacian_var
lease = CameraLease().acquire()
try:
    from maix import camera, image
    cam = camera.Camera(2560, 1440, image.Format.FMT_YVU420SP)
    for _ in range(40): cam.read()
    img = cam.read()
    def T(name, fn, n=5):
        t = time.time()
        for _ in range(n): r = fn()
        print("%-34s %7.1f ms" % (name, (time.time() - t) / n * 1000), flush=True); return r
    T("read()", lambda: cam.read())
    try: T("clear_buff()", lambda: cam.clear_buff())
    except Exception as e: print("clear_buff 不可用", e)
    g = T("to_format(GRAYSCALE)", lambda: img.to_format(image.Format.FMT_GRAYSCALE))
    gs = T("gray.resize(640,360)", lambda: g.resize(640, 360))
    a = T("image2cv(gray 640x360)", lambda: image.image2cv(gs, ensure_bgr=False, copy=True))
    print("array", a.shape, a.dtype)
    T("laplacian_var(640x360 中央)", lambda: laplacian_var(a))
    s = T("YUV resize(1280,720)", lambda: img.resize(1280, 720))
    T("to_jpeg(1280x720, q75)", lambda: s.to_jpeg(quality=75))
    T("save 2560x1440 jpg q95", lambda: img.save("/tmp/t.jpg", quality=95), n=2)
    cam.close()
finally:
    lease.release()
