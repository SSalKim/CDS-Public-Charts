from pathlib import Path
import json
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
    def test_trickling_source_has_an_elapsed_deadline_and_is_not_cached_partially(self):
        value = response(200,b"")
        value.raw = Mock()
        value.raw.read1.side_effect = [b"G",b"R",b"I"]
        value.close = Mock()
        self.client.session.get.return_value = value
        with patch("public_charts.sources.time.monotonic",side_effect=[0,0,1,2,31]), \
                self.assertRaises(requests.ReadTimeout):
            self.client.get("https://example.invalid/source.grib2",attempts=1,total_timeout=30)
        self.assertEqual(value.raw.read1.call_count,2)
        self.assertEqual(list(Path(self.temporary.name).glob("*.bin")),[])
        value.close.assert_called_once()

    def test_bounded_stream_preserves_complete_grib_bytes_and_cache(self):
        message = b"GRIB"+b"\0\0\0\2"+(20).to_bytes(8,"big")+b"7777"
        value = response(200,b"")
        value.raw = Mock()
        value.raw.read1.side_effect = [message[:7],message[7:],b""]
        value.close = Mock()
        self.client.session.get.return_value = value
        with patch("public_charts.sources.time.monotonic",return_value=0):
            result = self.client.get("https://example.invalid/source.grib2",attempts=1,total_timeout=30)
        self.assertEqual(result,message)
        self.assertEqual(self.client.get("https://example.invalid/source.grib2"),message)
        self.assertEqual(self.client.session.get.call_count,1)
        self.assertEqual(len(self.client.records[0]["source_id"]),64)
        self.assertLessEqual(self.client.records[0]["detected_at"],self.client.records[0]["observed_at"])
        self.assertLessEqual(self.client.records[0]["request_started_at"],self.client.records[0]["detected_at"])

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

    def test_redirect_page_instead_of_data_is_deferred_not_a_corrupt_forecast(self):
        # NOMADS answered an AIGFS byte-range request with a 718-byte 302 page (2026-11-18 18Z f132).
        from public_charts.sources import SourceDeferred
        self.client.session.get.return_value = response(302, content=b"<html>moved</html>")
        with patch("sys.stdout"), self.assertRaises(SourceDeferred) as caught:
            self.client.get("https://nomads.example.invalid/aigfs.grib2", (0, 1945345), attempts=1)
        self.assertEqual(caught.exception.reason, "source_redirect")

    def test_redirected_mirror_with_other_missing_copy_is_pending(self):
        from public_charts.sources import SourceDeferred
        client = Mock()
        client.get.side_effect = [SourceDeferred("source_redirect"), NotReady("Missing")]
        with self.assertRaises(NotReady):
            Provider("aigfs", client).fetch("2026111818", 132, {"pressure_msl"})

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

    def test_grib_ranges_also_disable_http_content_compression(self):
        message = b"GRIB" + b"\x00\x00\x00\x02" + (20).to_bytes(8, "big") + b"7777"
        self.client.session.get.return_value = response(206, message, "bytes 100-119/200")
        self.assertEqual(self.client.get("https://example.invalid/data.grib2", (100, 119), cache=False), message)
        self.assertEqual(self.client.session.get.call_args.kwargs["headers"],
                         {"Range": "bytes=100-119", "Accept-Encoding": "identity"})


class CanadaPathTests(unittest.TestCase):
    def test_previous_and_current_runs_use_their_own_date_directory(self):
        for cycle in ("2026111512", "2026111600"):
            with self.subTest(cycle=cycle):
                client = Mock()
                client.get.return_value = b"GRIB"
                with patch("public_charts.sources.decode", return_value="decoded"):
                    result = Provider("cmc", client).fetch(cycle, 42, {"geopotential_200"})
                self.assertEqual(result, {"geopotential_200": "decoded"})
                url = client.get.call_args.args[0]
                self.assertEqual(url, f"https://dd.weather.gc.ca/{cycle[:8]}/WXO-DD/model_gdps/15km/{cycle[8:]}/042/"
                                 f"{cycle[:8]}T{cycle[8:]}Z_MSC_GDPS_GeopotentialHeight_IsbL-0200_LatLon0.15_PT042H.grib2")
                self.assertNotIn("/today/", url)

    def test_total_rain_fallback_stays_in_the_requested_run_date(self):
        client = Mock()
        def get(url, *args, **kwargs):
            if "Precip-Accum3h_Sfc" in url:
                raise NotReady("No standalone three-hour rain")
            return b"GRIB"
        client.get.side_effect = get
        with patch("public_charts.sources.decode", return_value="decoded"):
            Provider("cmc", client).fetch("2026111512", 120, {"precipitation"})
        first, mirror, second = [call.args[0] for call in client.get.call_args_list]
        self.assertIn("Precip-Accum3h_Sfc", first)
        self.assertIn("Precip-Accum3h_Sfc", mirror)
        self.assertIn("hpfx.collab.science.gc.ca", mirror)
        self.assertIn("Precip-Accum_Sfc", second)
        self.assertTrue(all("/20261115/WXO-DD/model_gdps/15km/12/120/" in url for url in (first, second)))


class MirrorTests(unittest.TestCase):
    @staticmethod
    def aifs_index(offset=100, parameter="msl"):
        return json.dumps({"param":parameter,"levtype":"sfc","_offset":offset,"_length":40}).encode()

    def test_aifs_prefers_aws_and_uses_short_single_attempt_inventory_probe(self):
        client=Mock()
        client.get.side_effect=lambda url,*args,**kwargs:self.aifs_index() if url.endswith(".index") else b"GRIB"
        with patch("public_charts.sources.decode",return_value="decoded"):
            result=Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"})
        self.assertEqual(result,{"pressure_msl":"decoded"})
        self.assertTrue(all("ecmwf-forecasts.s3.eu-central-1.amazonaws.com" in call.args[0] for call in client.get.call_args_list))
        self.assertEqual(client.get.call_args_list[0].kwargs,{"cache":False,"attempts":1,"timeout":(5,15)})

    def test_ifs_uses_same_indexed_reader_but_ifs_model_and_only_025_grid(self):
        client=Mock()
        client.get.side_effect=lambda url,*args,**kwargs:self.aifs_index() if url.endswith(".index") else b"GRIB"
        with patch("public_charts.sources.decode",return_value="decoded"):
            result=Provider("ecmwf",client).fetch("2026111606",24,{"pressure_msl"})
        self.assertEqual(result,{"pressure_msl":"decoded"})
        self.assertTrue(all("/20261116/06z/ifs/0p25/oper/" in call.args[0] for call in client.get.call_args_list))
        self.assertTrue(client.get.call_args.args[0].endswith("20261116060000-24h-oper-fc.grib2"))

    def test_aifs_incomplete_primary_index_uses_complete_google_copy(self):
        client=Mock()
        def get(url,*args,**kwargs):
            if url.endswith(".index"):
                return self.aifs_index(parameter="2t") if "amazonaws" in url else self.aifs_index(200)
            return b"GRIB"
        client.get.side_effect=get
        with patch("public_charts.sources.decode",return_value="decoded"):
            Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"})
        data_calls=[call for call in client.get.call_args_list if len(call.args)>1]
        self.assertEqual(len(data_calls),1)
        self.assertIn("storage.googleapis.com/ecmwf-open-data",data_calls[0].args[0])
        self.assertEqual(data_calls[0].args[1],(200,239))

    def test_data_failure_reloads_secondary_index_never_reuses_primary_offsets(self):
        client=Mock()
        def get(url,*args,**kwargs):
            if url.endswith(".index"):
                return self.aifs_index(100 if "amazonaws" in url else 300)
            if "amazonaws" in url:
                raise NotReady("Data has not followed the index yet")
            return b"GRIB"
        client.get.side_effect=get
        with patch("public_charts.sources.decode",return_value="decoded"):
            Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"})
        self.assertEqual(client.get.call_args.args[1],(300,339))
        self.assertIn("storage.googleapis.com",client.get.call_args.args[0])

    def test_all_missing_is_pending_but_real_errors_are_not_hidden(self):
        client=Mock();client.get.side_effect=NotReady("Missing")
        with self.assertRaises(NotReady):
            Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"})
        self.assertEqual(client.get.call_count,3)
        client.reset_mock();client.get.side_effect=[requests.HTTPError("403"),NotReady("Missing"),NotReady("Missing")]
        with self.assertRaises(requests.HTTPError):
            Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"})

    def test_throttled_mirror_with_other_missing_copies_is_pending_not_failure(self):
        response=requests.Response();response.status_code=503
        error=requests.HTTPError("SlowDown",response=response)
        client=Mock();client.get.side_effect=[error,NotReady("Missing"),NotReady("Missing")]
        with self.assertRaises(NotReady):
            Provider("ecmwf",client).fetch("2026111606",0,{"pressure_msl"})
        client.get.side_effect=error
        with self.assertRaises(NotReady):
            Provider("ecmwf",client).fetch("2026111606",0,{"pressure_msl"})

    def test_wrong_cycle_primary_does_not_contaminate_secondary_result(self):
        client=Mock()
        client.get.side_effect=lambda url,*args,**kwargs:self.aifs_index() if url.endswith(".index") else b"GRIB"
        with patch("public_charts.sources.decode",side_effect=[ValueError("Wrong cycle"),"decoded"]):
            self.assertEqual(Provider("aifs",client).fetch("2026111600",24,{"pressure_msl"}),{"pressure_msl":"decoded"})
        self.assertIn("storage.googleapis.com",client.get.call_args.args[0])

    def test_gfs_missing_aws_field_falls_back_to_nomads(self):
        client=Mock()
        def get(url,*args,**kwargs):
            if url.endswith(".idx"):
                name="TMP:surface" if "amazonaws" in url else "PRMSL:mean sea level"
                return f"1:0:d=2026111600:{name}:anl:\n2:100:d=2026111600:TMP:surface:anl:\n".encode()
            return b"GRIB"
        client.get.side_effect=get
        with patch("public_charts.sources.decode",return_value="decoded"):
            self.assertEqual(Provider("gfs",client).fetch("2026111600",0,{"pressure_msl"}),{"pressure_msl":"decoded"})
        self.assertIn("nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.20261116/00/",client.get.call_args.args[0])

    def test_aigfs_prefers_nomads_then_uses_s3_when_missing(self):
        client=Mock()
        def get(url,*args,**kwargs):
            if "nomads.ncep" in url:raise NotReady("Not published")
            return b"1:0:d=2026111600:PRMSL:mean sea level:anl:\n2:100:d=2026111600:TMP:surface:anl:\n" if url.endswith(".idx") else b"GRIB"
        client.get.side_effect=get
        with patch("public_charts.sources.decode",return_value="decoded"):
            Provider("aigfs",client).fetch("2026111600",0,{"pressure_msl"})
        self.assertIn("nomads.ncep",client.get.call_args_list[0].args[0])
        self.assertIn("noaa-nws-graphcastgfs-pds.s3.amazonaws.com",client.get.call_args.args[0])

    def test_sflux_falls_back_without_changing_model_grid_or_filename(self):
        client=Mock()
        def get(url,*args,**kwargs):
            if "amazonaws" in url:raise NotReady("Not published")
            return b"1:0:d=2026111600:PRATE:surface:0-3 hour ave fcst:\n2:100:d=2026111600:TMP:surface:anl:\n" if url.endswith(".idx") else b"GRIB"
        client.get.side_effect=get
        with patch("public_charts.sources.decode",return_value="decoded") as decode_field:
            self.assertEqual(Provider("gfs",client).sflux("2026111600",3),"decoded")
        self.assertIn("nomads.ncep",client.get.call_args.args[0])
        self.assertTrue(client.get.call_args.args[0].endswith("gfs.t00z.sfluxgrbf003.grib2"))
        self.assertEqual(decode_field.call_args.args[-1],"precipitation_rate")

    def test_cmc_primary_failure_uses_same_dated_hpfx_file(self):
        client=Mock()
        client.get.side_effect=[NotReady("Not published"),b"GRIB"]
        with patch("public_charts.sources.decode",return_value="decoded"):
            Provider("cmc",client).fetch("2026111600",24,{"geopotential_500"})
        self.assertIn("hpfx.collab.science.gc.ca/20261116/WXO-DD/",client.get.call_args.args[0])

    def test_cmc_wrong_cycle_primary_tries_validated_hpfx_copy(self):
        client=Mock();client.get.return_value=b"GRIB"
        with patch("public_charts.sources.decode",side_effect=[ValueError("Wrong cycle"),"decoded"]):
            self.assertEqual(Provider("cmc",client).fetch("2026111600",24,{"geopotential_500"}),
                             {"geopotential_500":"decoded"})
        self.assertIn("hpfx.collab.science.gc.ca/20261116/WXO-DD/",client.get.call_args.args[0])


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

    def test_ifs_native_snowfall_water_equivalent_normalizes_to_kg_per_m2(self):
        handle=ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            for key,value in {"Ni":2,"Nj":2,"dataDate":20261116,"dataTime":600,
                              "latitudeOfFirstGridPointInDegrees":31.,"latitudeOfLastGridPointInDegrees":30.,
                              "longitudeOfFirstGridPointInDegrees":120.,"longitudeOfLastGridPointInDegrees":121.,
                              "iDirectionIncrementInDegrees":1.,"jDirectionIncrementInDegrees":1.,
                              "paramId":144,"stepType":"accum","startStep":0,"endStep":3}.items():
                ec.codes_set(handle,key,value)
            ec.codes_set_values(handle,np.array([0.,.001,.002,.003]))
            error=ec.codes_get(handle,"packingError")
            self.assertEqual(ec.codes_get(handle,"units"),"m of water equivalent")
            field=decode(ec.codes_get_message(handle),"2026111606",3,"snowfall_water_equivalent")
        finally:
            ec.codes_release(handle)
        self.assertAlmostEqual(field.metadata["packing_error"],error*1000)
        self.assertAlmostEqual(float(field.values.max()),3.,places=5)


if __name__ == "__main__":
    unittest.main()
