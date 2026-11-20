import unittest

import numpy as np

from public_charts import policy
from public_charts.plotter import contour_sigma
from public_charts.quality import (BELOW_GROUND_MARGIN, GRAVITY, MODEL_TERRAIN_KM, TERRAIN_SMOOTH_LOW,
                                   below_ground_levels, closed_contour_predicate, display_fields)

OLD, NEW = "2026112012", policy.SMOOTHING_CUTOVER


def plateau(step=.25, seed=0):
    latitude = np.arange(10, 60 + step / 2, step)
    longitude = np.arange(60, 140 + step / 2, step)
    lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
    terrain = 4500 * np.exp(-(((lat - 33) / 5) ** 2 + ((lon - 88) / 12) ** 2))
    noise = np.random.default_rng(seed).normal(0, 6, terrain.shape) * (terrain > 2500)
    pressure = (1015 + 10 * np.sin(np.deg2rad(lon * 3)) + noise) * 100
    return {"latitude": latitude, "longitude": longitude, "terrain_height": terrain, "pressure_msl": pressure}


def roughness(fields, out, key="pressure_msl"):
    high = fields["terrain_height"] > 3000
    return np.abs(np.diff(out[key], axis=1))[high[:, 1:]].mean()


class CutoverTests(unittest.TestCase):
    def test_render_version_marks_the_new_smoothing(self):
        for model in ("gfs", "ukmo"):
            self.assertEqual(policy.render_version(NEW, model, "surfce", "asia")
                             - policy.render_version("2026112012", model, "surfce", "asia"), policy.SMOOTHING_RENDER_STEP)
        self.assertFalse(policy.model_smoothing(OLD))

    def test_earlier_cycles_keep_the_common_250_km_smoothing(self):
        fields = plateau()
        old = {m: display_fields(fields, OLD, "asia", model_id=m)["pressure_msl"] for m in ("gfs", "ukmo")}
        np.testing.assert_array_equal(old["gfs"], old["ukmo"])
        np.testing.assert_array_equal(old["gfs"], display_fields(fields, OLD, "asia")["pressure_msl"])

    def test_without_a_model_the_new_cycle_falls_back_to_the_common_width(self):
        fields = plateau()
        np.testing.assert_array_equal(display_fields(fields, NEW, "asia")["pressure_msl"],
                                      display_fields(fields, OLD, "asia")["pressure_msl"])


class ModelWidthTests(unittest.TestCase):
    def test_noisier_models_get_wider_smoothing(self):
        self.assertLess(MODEL_TERRAIN_KM["ukmo"], MODEL_TERRAIN_KM["ecmwf"])
        self.assertLess(MODEL_TERRAIN_KM["ecmwf"], MODEL_TERRAIN_KM["gfs"])
        fields = plateau()
        rough = {m: roughness(fields, display_fields(fields, NEW, "asia", model_id=m)) for m in ("ukmo", "ecmwf", "gfs")}
        self.assertGreater(rough["ukmo"], rough["ecmwf"])
        self.assertGreater(rough["ecmwf"], rough["gfs"])
        self.assertLess(rough["ukmo"], roughness(fields, fields) / 3)

    def test_lowlands_and_the_korea_map_are_untouched(self):
        fields = plateau()
        out = display_fields(fields, NEW, "asia", model_id="gfs")
        low = fields["terrain_height"] <= TERRAIN_SMOOTH_LOW
        np.testing.assert_array_equal(out["pressure_msl"][low], fields["pressure_msl"][low])
        self.assertIs(display_fields(fields, NEW, "hkor", model_id="gfs"), fields)


class BelowGroundTests(unittest.TestCase):
    def fields(self):
        fields = plateau()
        shape = fields["terrain_height"].shape
        rng = np.random.default_rng(1)
        under = fields["terrain_height"] > 1500 + 1000
        fields.update({"geopotential_850": np.full(shape, 1500 * GRAVITY),
                       "temperature_850": 270 + rng.normal(0, 3, shape) * under,
                       "wind_u_850": 5 + rng.normal(0, 4, shape) * under,
                       "geopotential_500": np.full(shape, 5600 * GRAVITY),
                       "temperature_500": 250 + rng.normal(0, 3, shape)})
        return fields

    def test_only_levels_under_the_terrain_are_smoothed(self):
        fields = self.fields()
        out = display_fields(fields, NEW, "asia", model_id="ecmwf")
        for key in ("temperature_850", "wind_u_850"):
            self.assertLess(roughness(fields, out, key), roughness(fields, fields, key) / 3)
        clear = fields["terrain_height"] < 1500 - BELOW_GROUND_MARGIN
        np.testing.assert_array_equal(out["temperature_850"][clear], fields["temperature_850"][clear])
        np.testing.assert_array_equal(out["temperature_500"], fields["temperature_500"])

    def test_old_cycles_and_missing_heights_leave_levels_alone(self):
        fields = self.fields()
        self.assertIs(display_fields(fields, OLD, "asia", model_id="ecmwf")["temperature_850"], fields["temperature_850"])
        without = {k: v for k, v in fields.items() if k != "geopotential_850"}
        self.assertIs(below_ground_levels(without, 100), without)

    def test_missing_values_stay_missing(self):
        fields = self.fields()
        fields["temperature_850"][5, 5] = np.nan
        fields["terrain_height"][5, 5] = 5000
        out = below_ground_levels(fields, 100)
        self.assertTrue(np.isnan(out["temperature_850"][5, 5]))
        self.assertTrue(np.isfinite(out["temperature_850"][6:, 6:]).all())


class DrawingTests(unittest.TestCase):
    def test_plateau_rings_are_no_longer_pruned(self):
        fields = plateau()
        self.assertIsNone(closed_contour_predicate(fields, NEW))
        self.assertTrue(callable(closed_contour_predicate(fields, OLD)))
        self.assertTrue(callable(closed_contour_predicate(fields)))

    def test_contour_smoothing_has_the_same_width_in_km_on_every_grid(self):
        coarse = contour_sigma(np.arange(10, 60, .25), np.arange(60, 140, .25), 30, 38)
        fine = contour_sigma(np.arange(10, 60, .125), np.arange(60, 140, .125), 30, 38)
        self.assertAlmostEqual(fine[0], 2 * coarse[0])
        self.assertAlmostEqual(fine[1], 2 * coarse[1])
        self.assertAlmostEqual(coarse[0] * .25 * 111, 30)


if __name__ == "__main__":
    unittest.main()
