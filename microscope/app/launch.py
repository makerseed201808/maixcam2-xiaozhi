#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""“显微镜”app 启动器：借用屏幕（UiLease），在前台运行显微服务并显示实时画面。

- 若显微服务已在后台运行（run_scope.sh start），先用停止文件让它关相机退出，再由本 app 接管；
- 关闭 app 时把 SIGTERM 转给显微服务，它会先关相机再退出；绝不先强杀（相机被强杀需重启才能恢复）。
"""
import os
import signal
import subprocess
import sys
import time

SERVICE_DIR = "/root/microscope"
LOG = "/tmp/scope_service.log"


def _parent_death_signal(parent_pid):
    import ctypes
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set parent-death signal")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def stop_background_service():
    out = subprocess.run(["sh", os.path.join(SERVICE_DIR, "run_scope.sh"), "stop"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    print("[显微镜 app] 后台服务：", out.stdout.strip(), flush=True)


def main():
    if any(k == "LD_PRELOAD" or k.startswith("MAIXCAM2_") for k in os.environ):
        clean = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD" and not k.startswith("MAIXCAM2_")}
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:], clean)
    sys.path.insert(0, "/root/xiaozhi")
    from ui_ownership import UiLease

    stop_background_service()
    child = None
    stop_at = None

    def stop(signum, frame):
        nonlocal stop_at
        if stop_at is None:
            stop_at = time.monotonic()
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with UiLease():
        if stop_at is not None:
            return 0
        try:
            os.remove("/tmp/scope_service.stop")
        except OSError:
            pass
        parent_pid = os.getpid()
        with open(LOG, "a") as log:
            child = subprocess.Popen([sys.executable, "-u", "scope_service.py", "--screen"], cwd=SERVICE_DIR,
                                     stdout=log, stderr=subprocess.STDOUT,
                                     preexec_fn=lambda: _parent_death_signal(parent_pid))
        try:
            while child.poll() is None:
                if stop_at is not None and time.monotonic() - stop_at > 20:
                    # 20 秒仍未退出：再用停止文件请求一次，仍不强杀相机进程
                    open("/tmp/scope_service.stop", "w").close()
                    stop_at = time.monotonic()
                time.sleep(0.1)
            return child.returncode
        finally:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    print("[显微镜 app] 显微服务 20 秒未退出，保留进程以免相机异常", flush=True)


if __name__ == "__main__":
    # 原生启动器需要脚本正常结束；SystemExit 非零会弹 code:-1 错误框（siot_monitor 实测）
    rc = main()
    if rc not in (0, None):
        print("[显微镜 app] 显微服务退出码 %s，见 %s" % (rc, LOG), flush=True)
