import unittest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from public_charts.streamlines import streamlines


def vortex_field(seed=3):
    rng = np.random.default_rng(seed)
    x = np.linspace(-3e6, 3e6, 40)
    y = np.linspace(-2e6, 2e6, 40)
    X, Y = np.meshgrid(x, y)
    u, v = np.full_like(X, 12.), np.zeros_like(X)
    for _ in range(8):
        cx, cy, s, a = rng.uniform(-3e6, 3e6), rng.uniform(-2e6, 2e6), rng.uniform(4e5, 1.2e6), rng.uniform(-30, 30)
        weight = np.exp(-((X - cx)**2 + (Y - cy)**2) / s**2)
        u += -a * (Y - cy) / s * weight
        v += a * (X - cx) / s * weight
    u[:3, :6] = np.nan  # missing data ends trajectories, as masked values do in matplotlib
    return x, y, u, v


def arc(points, samples=60):
    distance = np.r_[0, np.cumsum(np.hypot(*np.diff(points, axis=0).T))]
    target = np.linspace(0, distance[-1], samples)
    return np.column_stack([np.interp(target, distance, points[:, 0]), np.interp(target, distance, points[:, 1])])


class VectorizedStreamlineTests(unittest.TestCase):
    def test_matches_matplotlib_streamplot_without_broken_streamlines(self):
        for seed in (3, 11):
            x, y, u, v = vortex_field(seed)
            figure, axes = plt.subplots()
            try:
                expected = axes.streamplot(x, y, u, v, density=1.35, broken_streamlines=False,
                                           minlength=.08, maxlength=8).lines.get_segments()
            finally:
                plt.close(figure)
            actual = streamlines(x, y, u, v, density=1.35, minlength=.08, maxlength=8)
            self.assertEqual(len(actual), len(expected), seed)
            for reference, line in zip(expected, actual):
                # matplotlib repeats the current point after rejected steps; geometry is identical.
                np.testing.assert_allclose(arc(line), arc(reference), atol=1e-6 * 6e6)

    def test_accepts_meshgrid_coordinates(self):
        x, y, u, v = vortex_field()
        X, Y = np.meshgrid(x, y)
        flat = streamlines(x, y, u, v, density=1.35, minlength=.08, maxlength=8)
        mesh = streamlines(X, Y, u, v, density=1.35, minlength=.08, maxlength=8)
        self.assertEqual(len(flat), len(mesh))


if __name__ == "__main__":
    unittest.main()
