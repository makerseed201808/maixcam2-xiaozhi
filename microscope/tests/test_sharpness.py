import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sharpness import laplacian_var, median


def blur(a, n):
    a = a.astype(np.float32)
    for _ in range(n):
        a = (a + np.roll(a, 1, 0) + np.roll(a, -1, 0) + np.roll(a, 1, 1) + np.roll(a, -1, 1)) / 5
    return a


def test_sharper_scores_higher():
    rng = np.random.RandomState(0)
    img = (rng.rand(240, 320) > 0.5).astype(np.float32) * 255
    scores = [laplacian_var(blur(img, n)) for n in (0, 1, 3, 8)]
    assert scores == sorted(scores, reverse=True)


def test_flat_image_is_zero_and_rgb_ok():
    assert laplacian_var(np.full((100, 100, 3), 128, np.uint8)) == 0.0


def test_median():
    assert median([3, 1, 2]) == 2 and median([1, 2, 3, 10]) == 2.5


def test_roi_mean_tracks_brightness():
    import numpy as np
    from sharpness import roi_mean
    dark = np.full((90, 160, 3), 30, np.uint8)
    bright = np.full((90, 160, 3), 200, np.uint8)
    assert abs(roi_mean(dark) - 30) < 0.5 and abs(roi_mean(bright) - 200) < 0.5


def test_tenengrad_small_robust_to_heavy_noise():
    """噪声大时：清晰图案 + 噪声 应高于 模糊图案 + 噪声（laplacian_var 在这种情况下会反过来）。"""
    import numpy as np
    from sharpness import tenengrad_small, laplacian_var
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:360, 0:640]
    sharp = np.where(((xx // 40) + (yy // 40)) % 2 == 0, 160.0, 80.0)
    k = 31
    pad = np.pad(sharp, k // 2, mode="edge")
    c = pad.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    blur = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    noisy = lambda a, sd: np.clip(a + rng.normal(0, sd, a.shape), 0, 255).astype(np.uint8)
    a_sharp, a_blur = noisy(sharp, 25), noisy(blur, 45)        # 模糊帧噪声更大（增益更高）
    assert tenengrad_small(a_sharp) > 2 * tenengrad_small(a_blur)
    assert laplacian_var(a_blur) > laplacian_var(a_sharp)      # 旧指标被噪声误导
