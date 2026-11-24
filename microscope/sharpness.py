"""清晰度计算（开发方案 2.3）：自动对焦用 tenengrad_small（缩小后 Sobel 梯度，抗噪）；laplacian_var 保留作对比。

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


def tenengrad_small(arr, roi=DEFAULT_ROI, factor=4):
    """抗噪清晰度：观察区先按 factor×factor 块求平均（缩小），再算梯度平方均值，除以平均亮度。

    2026-11-13 真机：画面噪点多时 laplacian_var 被噪声主导，自动对焦停在离焦位置（sharp.py 实测 12.9）；
    缩小 4 倍再算梯度能正确找到峰值（3720 步，86）。除以亮度让曝光小幅变化时分数可比。
    """
    g = gray(crop(arr, roi))
    f = max(1, int(factor))
    h, w = (g.shape[0] // f) * f, (g.shape[1] // f) * f
    if h < 3 * f or w < 3 * f:
        f = 1
        h, w = g.shape[0], g.shape[1]
    s = g[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    # 3×3 Sobel
    gx = (s[:-2, 2:] + 2 * s[1:-1, 2:] + s[2:, 2:]) - (s[:-2, :-2] + 2 * s[1:-1, :-2] + s[2:, :-2])
    gy = (s[2:, :-2] + 2 * s[2:, 1:-1] + s[2:, 2:]) - (s[:-2, :-2] + 2 * s[:-2, 1:-1] + s[:-2, 2:])
    return float((gx * gx + gy * gy).mean() / max(1.0, float(s.mean())))


def roi_mean(arr, roi=DEFAULT_ROI, step=4):
    """观察区平均亮度（0–255）。随清晰度一起回报：挡光或背光掉了时亮度骤降，便于发现异常帧。"""
    return float(gray(crop(arr, roi))[::step, ::step].mean())


def median(values):
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0
