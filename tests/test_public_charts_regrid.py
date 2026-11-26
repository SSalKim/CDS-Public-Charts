import unittest

import cartopy.crs as ccrs
import numpy as np
from cartopy.vector_transform import vector_scalar_to_grid

from public_charts import plotter


class RegridVectorsTests(unittest.TestCase):
    def test_cached_triangulation_matches_cartopy_exactly(self):
        projection = ccrs.LambertConformal(central_longitude=126, central_latitude=38)
        x = np.linspace(-2.1e6, 2.3e6, 200)
        y = np.linspace(-1.7e6, 1.9e6, 140)
        extent = (-2.05e6, 2.25e6, -1.65e6, 1.85e6)
        rng = np.random.default_rng(7)
        plotter._STREAM_TRIANGULATIONS.clear()
        for _ in range(2):  # second pass uses the cached triangulation
            u = rng.normal(size=(140, 200)) * 10
            v = rng.normal(size=(140, 200)) * 10
            u[5:9, 40:60] = np.nan
            expected = vector_scalar_to_grid(projection, projection, [40, 40], x, y, u, v, target_extent=extent)
            actual = plotter.regrid_vectors(x, y, u, v, [40, 40], extent)
            for want, got in zip(expected, actual):
                np.testing.assert_array_equal(np.asarray(want), np.asarray(got))
        self.assertEqual(len(plotter._STREAM_TRIANGULATIONS), 1)


if __name__ == "__main__":
    unittest.main()
