import hashlib, json, os, sys, threading, time, urllib.request
import pytest
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open   # 不走系统代理
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "xiaozhi", "service"))
from scope_camera import CameraBusy, CameraSession, FakeBackend
from scope_service import CaptureStore, ScopeService, make_http
from camera_ownership import CameraLease


class Focus(object):
    z = 0.0
    def __call__(self):           # 峰值在 z=0.5
        return max(0.0, 1.0 - abs(self.z - 0.5) * 2)


@pytest.fixture
def svc(tmp_path):
    focus = Focus()
    lock = str(tmp_path / "cam.lock")
    session = CameraSession(FakeBackend(focus_fn=focus), lease_factory=lambda: CameraLease(lock), idle_s=0.2,
                            lease_wait_s=0.5)
    sent = []
    s = ScopeService(session, CaptureStore(str(tmp_path / "caps")), publish=lambda t, o: sent.append((t, o)),
                     http_base="http://test:8091", log=lambda *a: None)
    s.focus, s.sent, s.lock = focus, sent, lock
    yield s
    session.close()


def test_focus_scores_rise_then_fall(svc):
    scores = []
    for i, z in enumerate([0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0]):
        svc.focus.z = z
        r = svc.handle_focus({"seq": i + 1, "z": int(z * 10000)})
        assert r["seq"] == i + 1 and "score" in r
        assert 0 <= r["brightness"] <= 255                  # 回报观察区亮度，便于发现挡光等异常帧
        scores.append(r["score"])
    peak = scores.index(max(scores))
    assert peak == 3
    assert scores[:peak + 1] == sorted(scores[:peak + 1]) and scores[peak:] == sorted(scores[peak:], reverse=True)
    assert svc.session.exposure_locked is not None          # 对焦期间锁定曝光
    assert svc.sent[-1][0] == "siot/scope_focus"


def test_capture_writes_original_and_hash(svc):
    r = svc.handle_capture({"type": "request", "id": "q1", "record": "R-1", "slot": 2})
    assert r["type"] == "result" and r["capture_id"] == "cap-000001" and r["record"] == "R-1"
    path = svc.store.path("cap-000001", ".jpg")
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == r["sha256"]
    assert r["url"] == "http://test:8091/scope/cap-000001.jpg"
    assert json.load(open(svc.store.path("cap-000001", ".json")))["slot"] == 2
    assert svc.handle_capture({"type": "request", "id": "q2"})["capture_id"] == "cap-000002"
    assert svc.handle_capture({"type": "result"}) is None     # 自己发出的结果不处理


def test_capture_20_in_a_row(svc):
    ids = [svc.handle_capture({"type": "request", "id": "q%d" % i})["capture_id"] for i in range(20)]
    assert len(set(ids)) == 20


def test_camera_busy_is_reported_not_stolen(svc):
    other = CameraLease(svc.lock).acquire()          # 小智正在拍照
    try:
        r = svc.handle_capture({"type": "request", "id": "busy", "record": "R-9"})
        assert r["type"] == "error" and "占用" in r["reason"]
        f = svc.handle_focus({"seq": 7})
        assert "error" in f and f["seq"] == 7
    finally:
        other.release()
    assert svc.handle_capture({"type": "request", "id": "ok"})["type"] == "result"


def test_capture_waits_for_lock_then_succeeds(svc):
    other = CameraLease(svc.lock).acquire()
    svc.session.lease_wait_s = 3.0
    threading.Timer(0.8, other.release).start()          # 小智 0.8 秒后拍完
    t = time.time()
    r = svc.handle_capture({"type": "request", "id": "q"})
    assert r["type"] == "result" and 0.7 < time.time() - t < 3.0


def test_session_releases_lock_when_idle(svc):
    svc.handle_focus({"seq": 1})
    assert svc.session.is_open
    with pytest.raises(RuntimeError):
        CameraLease(svc.lock).acquire()               # 使用中：别人拿不到
    time.sleep(0.3)
    assert svc.session.close_if_idle()
    CameraLease(svc.lock).acquire().release()         # 空闲后：锁已释放


def test_http_serves_original_and_preview(svc):
    r = svc.handle_capture({"type": "request", "id": "h1"})
    srv = make_http(svc, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=svc.preview_loop, args=(stop,), daemon=True).start()
    try:
        body = OPEN("http://127.0.0.1:%d/scope/%s.jpg" % (port, r["capture_id"])).read()
        assert hashlib.sha256(body).hexdigest() == r["sha256"]
        prev = OPEN("http://127.0.0.1:%d/preview.jpg" % port, timeout=5).read()
        assert prev[:2] == b"\xff\xd8"
        st = json.load(OPEN("http://127.0.0.1:%d/status.json" % port))
        assert st["stats"]["capture"] == 1
        with pytest.raises(Exception):
            OPEN("http://127.0.0.1:%d/scope/../../etc/passwd" % port)
    finally:
        stop.set()
        srv.shutdown()


class FakeScreen(object):
    """屏幕模式测试替身：记录显示的帧，显示 n 帧后模拟点“退出”。"""
    def __init__(self, n):
        self.n, self.shown, self.note, self.msgs = n, 0, "", []
    def show(self, img):
        self.shown += 1
    def should_exit(self):
        return self.shown >= self.n
    def message(self, s):
        self.msgs.append(s)


def test_screen_mode_shows_frames_without_viewers_and_exits(svc):
    import threading
    svc.screen = FakeScreen(5)
    t = threading.Thread(target=svc.run_jobs, args=(threading.Event(),), kwargs={"fps": 50})
    t.start(); t.join(10)
    assert not t.is_alive() and svc.screen.shown >= 5
