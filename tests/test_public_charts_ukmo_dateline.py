from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import h5py
import numpy as np

from public_charts import policy
from public_charts.dates import parse_cycle
from public_charts.ukmo import BOUNDS,NativeReader,RainDiagnostics,collect_ukmo
from public_charts_run import current_render_keys
from test_public_charts_ukmo_extensions import Native,base


ASIA=(29,45,160,210)
LONGITUDES=np.array([-175.,-165.,-155.,115.,125.,135.,165.,175.])


def fixture(path,cycle,*,pressure=False,longitudes=LONGITUDES):
    run=parse_cycle(cycle)
    latitude=np.array([50.,40.,30.,20.])
    raw=np.arange(4*len(longitudes),dtype=float).reshape(4,len(longitudes))
    values=np.stack((raw,raw+1000)) if pressure else raw
    flags=(np.arange(values.size).reshape(values.shape)%3).astype(np.int8)
    with h5py.File(path,"w") as source:
        source["latitude"],source["longitude"]=latitude,longitudes
        source["forecast_reference_time"]=int(run.timestamp())
        source["forecast_period"]=3*3600
        source["time"]=int((run+timedelta(hours=3)).timestamp())
        source["air_temperature" if pressure else "pressure_at_mean_sea_level"]=values
        if pressure:
            source["pressure"]=np.array([85000.,50000.])
            source["status_flag"]=flags
    return values,flags


class UkmoDatelineTests(unittest.TestCase):
    def read_fixture(self,root,cycle,*,pressure=False,bounds=ASIA,longitudes=LONGITUDES):
        path=Path(root)/"source.nc"
        values,flags=fixture(path,cycle,pressure=pressure,longitudes=longitudes)
        fs=Mock()
        fs.open.side_effect=lambda *args,**kwargs:path.open("rb")
        reads=[]
        original=h5py.Dataset.__getitem__
        def getitem(dataset,index):
            if dataset.ndim>=2:
                reads.append((dataset.name,index))
            return original(dataset,index)
        reader=NativeReader(Path(root)/"cache",cycle,bounds=bounds)
        with patch("fsspec.implementations.http_sync.HTTPFileSystem",return_value=fs), \
             patch.object(h5py.Dataset,"__getitem__",getitem):
            arrays,meta=reader.download(3,"temperature_on_pressure_levels" if pressure else "pressure_at_mean_sea_level",
                                        (500,850) if pressure else ())
        return reader,arrays,meta,values,flags,reads

    def test_12z_wraps_columns_and_reads_only_two_native_slabs(self):
        with tempfile.TemporaryDirectory() as root:
            reader,arrays,meta,values,_,reads=self.read_fixture(root,"2026111812")
            np.testing.assert_array_equal(arrays["longitude"],[165,175,185,195,205])
            np.testing.assert_array_equal(arrays["latitude"],[30,40])
            np.testing.assert_array_equal(arrays["value"],values[np.ix_([2,1],[6,7,0,1,2])])
            self.assertEqual(meta["longitude_version"],2)
            self.assertEqual(len(reads),2)
            self.assertEqual([(item[-1].start,item[-1].stop) for _,item in reads],[(6,8),(0,3)])
            self.assertEqual(reader.path(3,"field").parent.name,"asia-east-v2")

    def test_pressure_values_and_terrain_flags_follow_identical_column_order(self):
        with tempfile.TemporaryDirectory() as root:
            _,arrays,_,values,flags,reads=self.read_fixture(root,"2026111812",pressure=True)
            for level,index in ((500,1),(850,0)):
                np.testing.assert_array_equal(arrays[f"value_{level}"],values[index][np.ix_([2,1],[6,7,0,1,2])])
                np.testing.assert_array_equal(arrays[f"flag_{level}"],flags[index][np.ix_([2,1],[6,7,0,1,2])])
            self.assertEqual(len(reads),8)

    def test_earlier_cycles_and_korea_keep_the_original_grid(self):
        with tempfile.TemporaryDirectory() as root:
            reader,arrays,meta,values,_,reads=self.read_fixture(root,"2026111806")
            np.testing.assert_array_equal(arrays["longitude"],[165,175])
            np.testing.assert_array_equal(arrays["value"],values[np.ix_([2,1],[6,7])])
            self.assertEqual(meta["longitude_version"],1)
            self.assertEqual(reader.path(3,"field").parent.name,"asia")
            self.assertEqual(len(reads),1)
        with tempfile.TemporaryDirectory() as root:
            reader,arrays,meta,values,_,reads=self.read_fixture(root,"2026111812",bounds=BOUNDS)
            np.testing.assert_array_equal(arrays["longitude"],[115,125,135])
            np.testing.assert_array_equal(arrays["value"],values[np.ix_([2,1],[3,4,5])])
            self.assertFalse(reader.wrap_longitude)
            self.assertEqual(meta["longitude_version"],1)
            self.assertEqual(len(reads),1)

    def test_positive_longitude_sources_need_only_one_contiguous_slab(self):
        longitude=np.array([115.,125.,135.,165.,175.,185.,195.,205.])
        with tempfile.TemporaryDirectory() as root:
            _,arrays,_,values,_,reads=self.read_fixture(root,"2026111812",longitudes=longitude)
            np.testing.assert_array_equal(arrays["longitude"],[165,175,185,195,205])
            np.testing.assert_array_equal(arrays["value"],values[np.ix_([2,1],[3,4,5,6,7])])
            self.assertEqual(len(reads),1)

    def test_old_or_truncated_native_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            reader=NativeReader(root,"2026111812",bounds=ASIA)
            path=reader.path(3,"field")
            path.parent.mkdir(parents=True)
            def save(longitude,version):
                np.savez_compressed(path,latitude=np.array([30.,40.]),longitude=np.asarray(longitude),
                                    value=np.ones((2,len(longitude))))
                meta={"cycle":"2026111812","hour":3,"token":"field","levels":[],"bounds":list(ASIA),
                      "longitude_version":version,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
                path.with_suffix(".json").write_text(json.dumps(meta),encoding="utf-8")
            save([165.,175.,185.,195.,205.],1)
            self.assertIsNone(reader.cached(path,3,"field",()))
            save([165.,175.],2)
            self.assertIsNone(reader.cached(path,3,"field",()))
            save([165.,175.,185.,195.,205.],2)
            self.assertIsNotNone(reader.cached(path,3,"field",()))

    def test_full_collector_cache_and_asia_rain_prefix_change_only_after_cutover(self):
        with tempfile.TemporaryDirectory() as root,patch("public_charts.ukmo.NativeReader",Native):
            _,meta,_=collect_ukmo("2026111818",69,root)
            self.assertEqual(meta["collector_version"],3)
            with patch("public_charts.ukmo.NativeReader",side_effect=AssertionError("Use wrapped cache")):
                self.assertEqual(collect_ukmo("2026111818",69,root)[2]["requests"],0)
        with tempfile.TemporaryDirectory() as root:
            rain=RainDiagnostics(root,"2026111812",base(),asia=True)
            rain.save_checkpoint("rain-asia-v1/steps/003.npz",{"rain":np.full((2,2),999.)})
            rain.save_checkpoint("rain-asia-v1/totals/003.npz",{"rain":np.full((2,2),999.)})
            rain.native=Native()
            np.testing.assert_allclose(rain.total(3),3.)
            self.assertEqual(rain.prefix_name,"rain-asia-east-v2")
            self.assertEqual(RainDiagnostics(root,"2026111806",base(),asia=True).prefix_name,"rain-asia-v1")
            self.assertEqual(RainDiagnostics(root,"2026111812",base()).prefix_name,"rain-korea-v1")

    def test_render_policy_preserves_earlier_asia_and_other_models(self):
        old={"model":"ukmo","cycle":"2026111806","hour":66,"product":"gph500","domain":"asia","render_version":4}
        new={**old,"cycle":"2026111818","render_version":11}
        stale={**new,"render_version":5}
        self.assertEqual(current_render_keys({"old":old,"new":new,"stale":stale}),{"old","new"})
        self.assertEqual(policy.render_version("2026111806","ukmo","gph500","asia"),5)
        self.assertEqual(policy.render_version("2026111812","ukmo","gph500","asia"),11)
        self.assertEqual(policy.render_version("2026111812","gfs","gph500","asia"),4)
        self.assertEqual(policy.render_version("2026111812","ukmo","gph500","hkor"),5)


if __name__=="__main__":
    unittest.main()
