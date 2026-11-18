import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from public_charts import field_cache
from public_charts.icon import IconProvider
from public_charts.sources import Decoded, HttpCache, NotReady


class FieldCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name)
        self.root=self.work/"raw-cache/partial-fields"
        self.signature={"version":1,"grid_version":1,"cycle":"2026111800","hour":3,
                        "parameter":"PMSL","level":None,"bounds":[0,.125,0,.125],"spacing":.125}
        self.field=Decoded(np.full((2,2),100000,dtype=np.float32),np.array([0,.125]),np.array([0,.125]),
                           {"native_grid_uuid":"verified","units":"Pa"})

    def test_round_trip_identity_hash_and_ttl(self):
        field_cache.save(self.root,self.signature,self.field,[{"last_modified":"source-time"}])
        cached=field_cache.read(self.root,self.signature)
        np.testing.assert_array_equal(cached[0],self.field.values)
        self.assertEqual(cached[4],[{"last_modified":"source-time"}])
        path,meta=field_cache.paths(self.root,self.signature)
        info=json.loads(meta.read_text())
        self.assertIsNone(field_cache.read(self.root,self.signature,now=info["cached_at"]+86401))
        self.assertIsNone(field_cache.read(self.root,{**self.signature,"hour":6}))
        path.write_bytes(b"corrupt")
        self.assertIsNone(field_cache.read(self.root,self.signature))

    def test_pruning_is_bounded_and_does_not_touch_unrelated_files(self):
        field_cache.save(self.root,self.signature,self.field,[])
        unrelated=self.work/"personal.txt"
        unrelated.write_text("keep")
        self.assertEqual(field_cache.prune(self.work,max_bytes=0),0)
        self.assertFalse(field_cache.paths(self.root,self.signature)[0].exists())
        self.assertEqual(unrelated.read_text(),"keep")

    def test_interrupted_atomic_writes_do_not_escape_the_cache_budget(self):
        path,meta=field_cache.paths(self.root,self.signature)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"orphan")
        temporary=meta.with_name(meta.name+"123-"+"a"*32+".tmp")
        temporary.write_bytes(b"interrupted")
        self.assertEqual(field_cache.prune(self.work),0)
        self.assertFalse(path.exists())
        self.assertFalse(temporary.exists())

    def test_partial_fetch_resumes_without_redownloading_a_verified_field(self):
        class Remapper:
            latitude=longitude=np.array([0,.125])
            def __call__(self,values):
                return values.reshape(2,2).copy()
        reads=[]
        def native(client,cycle,hour,parameter,level=None,**kwargs):
            reads.append(parameter)
            if parameter=="T" and reads.count("T")==1:
                raise NotReady("next variable not ready")
            client.records.append({"last_modified":"source-time","parameter":parameter})
            return np.full(4,273.15 if parameter=="T" else 100000,dtype=np.float32),{
                "uuidOfHGrid":"verified","gridType":"unstructured_grid","packingError":0,
                "units":"K" if parameter=="T" else "Pa"}
        with patch.dict("os.environ",{"PUBLIC_CHARTS_FIELD_CACHE_DIR":str(self.root)}), \
             patch("public_charts.icon.BOUNDS",(0,.125,0,.125)), \
             patch("public_charts.icon._REMAPPERS",{}), \
             patch("public_charts.icon.IconRemapper",return_value=Remapper()), \
             patch("public_charts.icon.native",side_effect=native):
            first=IconProvider(HttpCache(self.work/"first"))
            with self.assertRaises(NotReady):
                first.fetch("2026111800",3,{"pressure_msl","temperature_850"})
            second=IconProvider(HttpCache(self.work/"second"))
            fields=second.fetch("2026111800",3,{"pressure_msl","temperature_850"})
            self.assertEqual(reads,["PMSL","T","T"])
            self.assertEqual(second.client.field_cache_hits,1)
            np.testing.assert_array_equal(fields["pressure_msl"].values,self.field.values)


if __name__=="__main__":
    unittest.main()
