# -*- coding: utf-8 -*-
"""显微镜 app 屏幕叠加小智弹窗（2026-11-15）：不依赖 maix，用假的 image 模块。"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "xiaozhi", "service"))

from scope_screen import XiaozhiPopup, screen_to_raw_touch  # noqa: E402


class FakeImage(object):
    @staticmethod
    def cv2image(arr, bgr=False, copy=True):
        return ("img", arr.shape)


def write_popup(path, alpha):
    a = np.zeros((480, 640, 4), np.uint8)
    a[:, :, 3] = alpha
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 1, 640, 480) + a.tobytes())


def test_popup_visible_only_when_any_alpha(tmp_path):
    p = str(tmp_path / "popup.rgba")
    pop = XiaozhiPopup(FakeImage, p)
    pop.refresh()
    assert not pop.visible                          # 文件不存在
    write_popup(p, 0)
    pop.refresh()
    assert not pop.visible                          # 全透明 = 小智没弹窗
    write_popup(p, 200)
    os.utime(p, ns=(1, 2))
    pop.refresh()
    assert pop.visible
    with open(p, "wb") as f:                        # 写到一半：保持上次结果，不报错
        f.write(struct.pack("<III", 1, 640, 480) + b"\0" * 100)
    pop.refresh()
    assert pop.visible


def test_touch_mapping_is_inverse_of_xiaozhi():
    def overlay_touch_to_screen(raw_x, raw_y):       # 与 xiaozhi_service.overlay_touch_to_screen 相同
        return min(raw_y, 639), max(0, 479 - raw_x)
    for x, y in ((0, 0), (639, 479), (600, 20), (320, 240)):
        assert overlay_touch_to_screen(*screen_to_raw_touch(x, y)) == (x, y)


def test_ui_lease_partial_scope(tmp_path):
    from ui_ownership import UiLease, UiLeaseReader
    path = str(tmp_path / "owner.json")
    ps = lambda pid: "123"                          # Mac 上没有 /proc：用假的进程启动时刻
    with UiLease(path, scopes=("touch_wake",), process_start=ps):
        r = UiLeaseReader(path, process_start=ps)
        assert r.active("touch_wake") and not r.active("popup")
    with UiLease(path, process_start=ps):
        r = UiLeaseReader(path, process_start=ps)
        assert r.active("touch_wake") and r.active("popup")
    import pytest
    with pytest.raises(ValueError):
        UiLease(path, scopes=("screen",))
