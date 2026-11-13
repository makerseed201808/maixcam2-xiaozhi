"""清晰度计算（开发方案 2.3）：固定观察区灰度图的拉普拉斯方差。

只依赖 numpy，可在电脑上测试。输入为 H×W×3（RGB）或 H×W 的 uint8 数组。
观察区 roi = (x, y, w, h)，按画面比例给出（0–1），默认取中央 50%×50%。
为控制耗时，观察区先按 step 抽样（默认每 2 个像素取 1 个）。
"""
import numpy as np

DEFAULT_ROI = (0.25, 0.25, 0.5, 0.5)


def gray(arr):
    a = np.asarray(arr)
    if a.ndim == 3 and a.shape[2] == 1:      # MaixPy 灰度图转 numpy 后是 H×W×1
        a = a[..., 0]
    if a.ndim == 3:
        a = a[..., 0].astype(np.float32) * 0.299 + a[..., 1].astype(np.float32) * 0.587 \
            + a[..., 2].astype(np.float32) * 0.114
    return a.astype(np.float32)


def crop(arr, roi=DEFAULT_ROI):
    h, w = arr.shape[:2]
    x, y, rw, rh = roi
    x0, y0 = int(w * x), int(h * y)
    x1, y1 = max(x0 + 3, int(w * (x + rw))), max(y0 + 3, int(h * (y + rh)))
    return arr[y0:y1, x0:x1]


def laplacian_var(arr, roi=DEFAULT_ROI, step=2):
    g = gray(crop(arr, roi))[::step, ::step]
    lap = (4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
    return float(lap.var())


def roi_mean(arr, roi=DEFAULT_ROI, step=4):
    """观察区平均亮度（0–255）。随清晰度一起回报：挡光或背光掉了时亮度骤降，便于发现异常帧。"""
    return float(gray(crop(arr, roi))[::step, ::step].mean())


def median(values):
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0
