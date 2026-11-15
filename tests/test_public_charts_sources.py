from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import eccodes as ec
import numpy as np
import requests

from public_charts.sources import (Decoded, HttpCache, NotReady, Provider, decode,
                                   rainfall_from_rate, rainfall_interval)


def response(status=206, content=b"G", content_range="bytes 0-0/3571212"):
    result = requests.Response()
    result.status_code = status
    result.url = "https://nomads.ncep.noaa.gov/example.grib2"
    result._content = content
    result._content_consumed = True
    result.headers["Content-Range"] = content_range
    result.headers["Content-Length"] = str(len(content))
    return result


class FileSizeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.client = HttpCache(self.temporary.name)
        self.client.session = Mock()

    def test_probe_uses_content_range_not_compressed_head_length(self):
        self.client.session.get.return_value = response()
        self.client.session.head.return_value = response(200, b"", "")
        self.client.session.head.return_value.headers["Content-Length"] = "20"
        self.assertEqual(self.client.file_size("https://example.invalid/data.grib2"), 3571212)
        self.client.session.head.assert_not_called()
        self.assertEqual(self.client.session.get.call_args.kwargs["headers"],
                         {"Range": "bytes=0-0", "Accept-Encoding": "identity"})

    def test_rejects_full_response_bad_range_and_non_grib_header(self):
        for value in (response(200), response(content_range="bytes 1-1/100"),
                      response(content=b"X"), response(content_range="bytes 0-0/*")):
            self.client.session.get.return_value = value
            with self.assertRaises(ValueError):
                self.client.file_size("https://example.invalid/data.grib2")

    def test_missing_is_pending_but_permission_failure_still_fails(self):
        self.client.session.get.return_value = response(404)
        with self.assertRaises(NotReady):
            self.client.file_size("https://example.invalid/data.grib2")
        self.client.session.get.return_value = response(403)
        with self.assertRaises(requests.HTTPError):
            self.client.file_size("https://example.invalid/data.grib2")

    def test_last_ncep_field_uses_real_size_and_never_reversed_range(self):
        client = Mock()
        client.get.side_effect = lambda url, *args, **kwargs: (
            b"1:0:d=2026111518:TMP:surface:anl:\n2:100:d=2026111518:PRMSL:mean sea level:anl:\n"
            if url.endswith(".idx") else b"GRIB")
        client.file_size.return_value = 200
        with patch("public_charts.sources.decode", return_value="decoded"):
            result = Provider("aigfs", client).fetch("2026111518", 0, {"pressure_msl"})
        self.assertEqual(result, {"pressure_msl": "decoded"})
        self.assertEqual(client.get.call_args.args[1], (100, 199))
        client.session.head.assert_not_called()
        client.file_size.return_value = 20
        with self.assertRaises(NotReady):
            Provider("aigfs", client).fetch("2026111518", 0, {"pressure_msl"})


class PackingTests(unittest.TestCase):
    @staticmethod
    def field(values, end, error=None):
        metadata = {"startStep": 0, "endStep": end}
        if error is not None:
            metadata["packing_error"] = error
        return Decoded(np.array([values], dtype=np.float32), np.array([30.]),
                       np.arange(len(values), dtype=float), metadata)

    def test_observed_aifs_minus_0125_is_within_two_packing_errors(self):
        current = self.field([23.5, 29., 0.], 72, .0625)
        previous = self.field([23.625, 25., 0.], 66, .0625)
        np.testing.assert_array_equal(rainfall_interval(current, previous, 72, 6), [[0., 4., 0.]])

    def test_large_negative_unknown_precision_and_bad_grid_still_fail(self):
        for current, previous in ((self.field([1.], 72, .0625), self.field([2.], 66, .0625)),
                                  (self.field([23.5], 72), self.field([23.625], 66))):
            with self.assertRaisesRegex(ValueError, "Negative"):
                rainfall_interval(current, previous, 72, 6)
        current, previous = self.field([1.], 72, .0625), self.field([1.], 66, .0625)
        previous.latitude = np.array([31.])
        with self.assertRaisesRegex(ValueError, "grid differs"):
            rainfall_interval(current, previous, 72, 6)

    def test_rate_integration_scales_packing_uncertainty_too(self):
        current = self.field([.001], 6, 1e-7)
        current.metadata["stepType"] = "avg"
        with patch("public_charts.sources.rainfall_interval", return_value=np.ones((1, 1))) as interval:
            rainfall_from_rate(current, None, 6, 6)
        self.assertAlmostEqual(interval.call_args.args[0].metadata["packing_error"], 1e-7*21600)

    def test_decoding_normalizes_meter_packing_error_to_mm(self):
        handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            for key, value in {"Ni": 2, "Nj": 2, "dataDate": 20261115, "dataTime": 1200,
                               "latitudeOfFirstGridPointInDegrees": 31., "latitudeOfLastGridPointInDegrees": 30.,
                               "longitudeOfFirstGridPointInDegrees": 120., "longitudeOfLastGridPointInDegrees": 121.,
                               "iDirectionIncrementInDegrees": 1., "jDirectionIncrementInDegrees": 1.,
                               "paramId": 228, "stepType": "accum", "startStep": 0, "endStep": 6}.items():
                ec.codes_set(handle, key, value)
            ec.codes_set_values(handle, np.array([0., .001, .002, .003]))
            error = ec.codes_get(handle, "packingError")
            self.assertEqual(ec.codes_get(handle, "units"), "m")
            field = decode(ec.codes_get_message(handle), "2026111512", 6, "precipitation")
        finally:
            ec.codes_release(handle)
        self.assertAlmostEqual(field.metadata["packing_error"], error*1000)
        self.assertAlmostEqual(float(field.values.max()), 3., places=5)


if __name__ == "__main__":
    unittest.main()
