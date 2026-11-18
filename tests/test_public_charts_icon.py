import bz2
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests

from public_charts import policy
from public_charts.icon import IconProvider, IconRemapper, native, read_native, source_urls
from public_charts.sources import Decoded, HttpCache, NotReady, Provider


class IconPolicyTests(unittest.TestCase):
    def test_icon_uses_the_shared_catalog_with_nine_retained_cycles(self):
        self.assertEqual(policy.RETENTION["icon"], 9)
        self.assertIn("icon",policy.RETENTION)
        self.assertEqual(policy.TITLES["icon"], "DWD ICON (R03B07 L120)")

    def test_native_run_horizons_and_kim_display_schedule(self):
        for cycle, maximum, count in (("2026111600", 180, 38), ("2026111606", 120, 33),
                                      ("2026111612", 180, 38), ("2026111618", 120, 33)):
            hours = policy.hours("icon", cycle)
            self.assertEqual((hours[-1], len(hours)), (maximum, count))
            self.assertTrue({0, 81, 84, 90, 96, 108, 120} <= set(hours))
            self.assertNotIn(87, hours)
            for hour in hours:
                policy.expected_keys("icon", cycle, hour)
        with self.assertRaises(ValueError):
            policy.key("icon", "2026111606", 132, "gph500", "asia")

    def test_v1_urls_prefer_pressure_in_pa_and_exact_run(self):
        urls = source_urls("2026111612", 24, "FI", 500)
        self.assertEqual(urls[0], "https://opendata.dwd.de/weather/nwp/v1/m/icon/p/FI/"
                         "lvt1/100/lv1/50000/r/2026-11-16T12%3A00/s/PT024H00M.grib2")
        self.assertTrue(urls[1].endswith("/12/fi/icon_global_icosahedral_pressure-level_2026111612_024_500_FI.grib2.bz2"))
        self.assertTrue(source_urls("2026111612", 0, "CLAT", constant=True)[1].endswith(
            "icon_global_icosahedral_time-invariant_2026111612_CLAT.grib2.bz2"))
        for cycle, hour in (("2026111603", 0), ("2026111606", 123), ("2026111600", 181), ("2026111600", 80)):
            with self.assertRaises(ValueError):
                source_urls(cycle, hour, "PMSL")


class IconProviderTests(unittest.TestCase):
    def test_slow_socket_switches_exact_cycle_feed_after_one_short_attempt(self):
        client = Mock()
        client.get.side_effect = [requests.ReadTimeout("slow source"),b"second feed"]
        with patch("public_charts.icon.read_native",return_value=(np.ones(2),{"grid":"native"})) as decode:
            values, _ = native(client,"2026111700",39,"FI",300)
        self.assertEqual([call.args[0] for call in client.get.call_args_list],list(source_urls("2026111700",39,"FI",300)))
        for call in client.get.call_args_list:
            self.assertEqual(call.kwargs,{"attempts":1,"timeout":(10,15),"total_timeout":30})
        decode.assert_called_once_with(b"second feed","2026111700",39,"FI",300,constant=False)
        np.testing.assert_array_equal(values,np.ones(2))

    @staticmethod
    def field(parameter, level=None):
        value = {"T": 273.15, "RELHUM": 80, "SNOW_GSP": 2, "SNOW_CON": 1}.get(parameter, 101000)
        return Decoded(np.full((2, 2), value), np.array([30., 40.]), np.array([120., 130.]),
                       {"startStep": 0, "endStep": 24, "native_grid_uuid": "same", "packing_error": .001,
                        "units": "%" if parameter == "RELHUM" else "K"})

    def test_native_snow_is_the_sum_of_two_matching_accumulations(self):
        source = IconProvider(Mock(root=Path("unused")))
        with patch.object(source, "field", side_effect=lambda cycle,hour,param,level=None:self.field(param,level)):
            result = source.fetch("2026111612", 24, {"snowfall_water_equivalent"})["snowfall_water_equivalent"]
        np.testing.assert_allclose(result.values, 3)
        self.assertAlmostEqual(result.metadata["packing_error"], .002)

    def test_inconsistent_snow_intervals_are_not_added(self):
        source = IconProvider(Mock(root=Path("unused")))
        fields = [self.field("SNOW_GSP"), self.field("SNOW_CON")]
        fields[1].metadata["startStep"] = 21
        with patch.object(source, "field", side_effect=fields), self.assertRaisesRegex(ValueError, "windows differ"):
            source.fetch("2026111612", 24, {"snowfall_water_equivalent"})

    def test_humidity_is_derived_on_the_same_pressure_level(self):
        source = IconProvider(Mock(root=Path("unused")))
        with patch.object(source, "field", side_effect=lambda cycle,hour,param,level=None:self.field(param,level)) as fetch:
            result = source.fetch("2026111612", 24, {"specific_humidity_850"})["specific_humidity_850"]
        self.assertTrue(np.all((result.values > .003) & (result.values < .004)))
        self.assertEqual({call.args[3] for call in fetch.call_args_list}, {850})
        self.assertIn("water reference", result.metadata["derived_from"])

    def test_malformed_or_truncated_messages_are_rejected(self):
        for data in (b"", b"<html>missing</html>", bz2.compress(b"not GRIB"), b"BZhinvalid"):
            with self.assertRaises(ValueError):
                read_native(data, "2026111612", 0, "PMSL")

    def test_native_minute_units_are_normalized_without_guessing_wind_orientation(self):
        message = b"GRIB" + b"\0\0\0\2" + (20).to_bytes(8, "big") + b"7777"
        for orientation, name, valid in ((None, "u", True), (0, "u", True),
                                         (1, "u", False), (None, "unknown", False)):
            metadata = {"dataDate": 20261116, "dataTime": 1200, "startStep": "0m", "endStep": "0m",
                        "stepUnits": 0, "gridType": "unstructured_grid", "uuidOfHGrid": "test",
                        "numberOfDataPoints": 2, "numberOfMissing": 0, "typeOfLevel": "isobaricInhPa",
                        "level": 850, "uvRelativeToGrid": orientation, "shortName": name}
            def normalize(handle, key, value):
                self.assertEqual((key, value), ("stepUnits", "h"))
                metadata.update(stepUnits=1, startStep=0, endStep=0)
            with self.subTest(orientation=orientation, name=name), \
                    patch("public_charts.icon.ec.codes_new_from_message", return_value=1), \
                    patch("public_charts.icon.ec.codes_get", side_effect=lambda handle,key:metadata.get(key)), \
                    patch("public_charts.icon.ec.codes_set", side_effect=normalize), \
                    patch("public_charts.icon.ec.codes_get_values", return_value=np.array([1., 2.])), \
                    patch("public_charts.icon.ec.codes_release"):
                if valid:
                    _, result = read_native(message, "2026111612", 0, "U", 850)
                    self.assertEqual(result["original_step_units"], 0)
                    self.assertEqual(result["endStep"], 0)
                else:
                    with self.assertRaisesRegex(ValueError, "Earth-relative"):
                        read_native(message, "2026111612", 0, "U", 850)


class IconRemappingTests(unittest.TestCase):
    def test_constant_field_remains_constant_and_weights_are_reused(self):
        x, y = np.meshgrid(np.linspace(124.8, 125.2, 7), np.linspace(34.8, 35.2, 7))
        client = Mock()
        def coordinates(client, cycle, hour, parameter, **kwargs):
            values = y if parameter == "CLAT" else x
            return values.ravel(), {"uuidOfHGrid": "test", "units": "degrees"}
        with tempfile.TemporaryDirectory() as root, patch("public_charts.icon.native", side_effect=coordinates) as fetch:
            remap = IconRemapper(client, "2026111612", "test", x.size, root,
                                 spacing=.1, bounds=(35., 35.1, 125., 125.1))
            result = remap(np.full(x.size, 7., dtype=np.float32))
            np.testing.assert_allclose(result, 7)
            self.assertEqual(fetch.call_count, 2)
            cached = IconRemapper(client, "2026111618", "test", x.size, root,
                                  spacing=.1, bounds=(35., 35.1, 125., 125.1))
            np.testing.assert_array_equal(cached(np.full(x.size, 7., dtype=np.float32)), result)
            self.assertEqual(fetch.call_count, 2)
            with self.assertRaises(ValueError):
                cached(np.ones(x.size - 1))
