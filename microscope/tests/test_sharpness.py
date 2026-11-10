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
