import unittest

import numpy as np

from public_charts import policy
from public_charts.chart_style import KMA_CENTER_RULES, KMA_NUMERIC_RULES
from public_charts.plotter import extrema_candidates, great_circle_km, spaced_centers
from public_charts.quality import TERRAIN_SMOOTH_LOW, display_fields

OLD, NEW = "2026111906", policy.KMA_STYLE_CUTOVER


def plateau_fields(step=.25):
    latitude = np.arange(10, 60 + step / 2, step)
    longitude = np.arange(60, 140 + step / 2, step)
    lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
    terrain = 4500 * np.exp(-(((lat - 33) / 5) ** 2 + ((lon - 88) / 12) ** 2))
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 6, terrain.shape) * (terrain > 2500)  # grid-scale reduction artefacts
    pressure = (1015 + 10 * np.sin(np.deg2rad(lon * 3)) + noise) * 100
    return {"latitude": latitude, "longitude": longitude, "terrain_height": terrain, "pressure_msl": pressure}


class TerrainSmoothingTests(unittest.TestCase):
    def test_only_new_asia_charts_are_smoothed(self):
        fields = plateau_fields()
        self.assertIs(display_fields(fields, OLD, "asia"), fields)
        self.assertIs(display_fields(fields, NEW, "hkor"), fields, "The Korea map keeps published MSLP")
        smoothed = display_fields(fields, NEW, "asia")
        self.assertIsNot(smoothed, fields)
        self.assertIs(smoothed["terrain_height"], fields["terrain_height"])

    def test_plateau_noise_is_removed_and_lowlands_are_untouched(self):
        fields = plateau_fields()
        smoothed = display_fields(fields, NEW, "asia")["pressure_msl"]
        plateau = fields["terrain_height"] > 3000
        lowland = fields["terrain_height"] <= TERRAIN_SMOOTH_LOW
        roughness = lambda value: np.abs(np.diff(value, axis=1))[plateau[:, 1:]].mean()
        self.assertLess(roughness(smoothed), roughness(fields["pressure_msl"]) / 5)
        np.testing.assert_array_equal(smoothed[lowland], fields["pressure_msl"][lowland])

    def test_missing_values_stay_missing(self):
        fields = plateau_fields()
        fields["pressure_msl"][0, 0] = np.nan
        smoothed = display_fields(fields, NEW, "asia")["pressure_msl"]
        self.assertTrue(np.isnan(smoothed[0, 0]))
        self.assertTrue(np.isfinite(smoothed[1:, 1:]).all())


class CommonExtremaRuleTests(unittest.TestCase):
    def test_render_version_marks_the_new_style(self):
        layout = policy._layout_render_version(NEW, "gfs", "surfce", "asia")
        self.assertEqual(policy.render_version(NEW, "gfs", "surfce", "asia"),
                         layout + policy.PALETTE_RENDER_STEP + policy.KMA_STYLE_RENDER_STEP)
        self.assertEqual(policy.render_version(OLD, "gfs", "surfce", "asia"),
                         policy._layout_render_version(OLD, "gfs", "surfce", "asia"))

    def test_korea_resolves_smaller_features_with_tighter_spacing(self):
        for kind in ("pressure", "height", "temperature", "temperature_strict"):
            asia, korea = KMA_CENTER_RULES["asia"][kind], KMA_CENTER_RULES["hkor"][kind]
            self.assertLess(korea["radius"], asia["radius"])
            self.assertLess(korea["spacing_km"], asia["spacing_km"])
            self.assertLess(korea["smooth_km"], asia["smooth_km"])
        for kind in ("precipitation", "wind", "vorticity"):
            self.assertLess(KMA_NUMERIC_RULES["hkor"][kind]["smooth_km"], KMA_NUMERIC_RULES["asia"][kind]["smooth_km"])

    def test_km_smoothing_has_the_same_physical_width_on_every_grid(self):
        from unittest.mock import patch
        import public_charts.plotter as plotter
        widths = {}
        for step in (.25, .125, .1):
            latitude = np.arange(20, 50 + step / 2, step)
            longitude = np.arange(100, 150 + step / 2, step)
            field = np.zeros((len(latitude), len(longitude)))
            seen = []
            original = plotter.smooth_valid
            with patch.object(plotter, "smooth_valid", side_effect=lambda value, sigma, mask=None: (seen.append(sigma), original(value, sigma, mask))[1]):
                extrema_candidates(field, latitude, longitude, 550, .8, True, smooth_km=45)
            sigma_y, sigma_x = seen[0]
            widths[step] = (round(sigma_y * 111 * step, 6), round(sigma_x * 111 * .8 * step, 6))
            legacy = []
            with patch.object(plotter, "smooth_valid", side_effect=lambda value, sigma, mask=None: (legacy.append(sigma), original(value, sigma, mask))[1]):
                extrema_candidates(field, latitude, longitude, 550, .8, True)
            self.assertEqual(legacy[0], .8, "Legacy cycles keep the 0.8-cell smoothing")
        self.assertEqual(set(widths.values()), {(45.0, 45.0)})

    def test_same_kind_centres_are_spaced_and_capped_strongest_first(self):
        latitude, longitude = np.arange(0, 60.1, 1.), np.arange(100, 160.1, 1.)
        candidates = [(9., 30, 20, 1030), (8., 31, 21, 1028), (7., 30, 40, 1024), (6., 50, 50, 1020), (5., 10, 10, 1018)]
        kept = spaced_centers(candidates, latitude, longitude, 500, 3)
        self.assertEqual([item[0] for item in kept], [9., 7., 6.])
        for first in kept:
            for second in kept:
                if first is not second:
                    self.assertGreaterEqual(great_circle_km(latitude[first[1]], longitude[first[2]],
                                                            latitude[second[1]], longitude[second[2]]), 500)


if __name__ == "__main__":
    unittest.main()
