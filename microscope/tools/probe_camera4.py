import sys, time
sys.path.insert(0, "/root/xiaozhi")
from camera_ownership import CameraLease
lease = CameraLease().acquire()
cam = None
try:
    from maix import camera, image
    print(camera.Camera.add_channel.__doc__.strip()[:400], flush=True)
    cam = camera.Camera(2560, 1440, image.Format.FMT_YVU420SP)
    ch = cam.add_channel(1280, 720, image.Format.FMT_YVU420SP)
    print("channels", cam.get_ch_nums(), "ch size", ch.width(), ch.height(), flush=True)
    for _ in range(30): cam.read(); ch.read()
    def T(name, fn, n=5):
        t = time.time()
        for _ in range(n): r = fn()
        print("%-30s %7.1f ms" % (name, (time.time() - t) / n * 1000), flush=True); return r
    s = T("ch.read() 1280x720", lambda: ch.read())
    print("small", s.width(), s.height(), s.format(), flush=True)
    T("small.to_jpeg q75", lambda: s.to_jpeg(quality=75))
    T("small→gray", lambda: s.to_format(image.Format.FMT_GRAYSCALE))
    big = T("cam.read() 2560x1440", lambda: cam.read())
    print("big", big.width(), big.height(), flush=True)
    s.save("/root/scope_probe/ch1_1280x720.jpg", quality=90); big.save("/root/scope_probe/main_2560x1440.jpg", quality=95)
finally:
    if cam is not None: cam.close()
    lease.release()
print("done", flush=True)
