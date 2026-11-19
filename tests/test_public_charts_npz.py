import io
import unittest
import zipfile

import numpy as np

from public_charts import npz


class FastNpzTests(unittest.TestCase):
    def test_round_trip_matches_numpy_savez_compressed(self):
        arrays = {"values": np.linspace(-3, 3, 4000, dtype=np.float32).reshape(40, 100),
                  "latitude": np.arange(40.0), "mask": np.arange(100) % 3 == 0, "label": np.array(7, dtype=np.int16)}
        fast, reference = io.BytesIO(), io.BytesIO()
        npz.save(fast, **arrays)
        np.savez_compressed(reference, **arrays)
        with np.load(io.BytesIO(fast.getvalue())) as loaded, np.load(io.BytesIO(reference.getvalue())) as expected:
            self.assertEqual(sorted(loaded.files), sorted(expected.files))
            for name in expected.files:
                np.testing.assert_array_equal(loaded[name], expected[name])
                self.assertEqual(loaded[name].dtype, expected[name].dtype)

    def test_members_are_deflated_and_object_arrays_are_refused(self):
        stream = io.BytesIO()
        npz.save(stream, values=np.zeros((50, 50)))
        with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as archive:
            self.assertEqual([item.compress_type for item in archive.infolist()], [zipfile.ZIP_DEFLATED])
            self.assertLess(archive.infolist()[0].compress_size, archive.infolist()[0].file_size)
        with self.assertRaises(ValueError):
            npz.save(io.BytesIO(), bad=np.array([{"a": 1}], dtype=object))


if __name__ == "__main__":
    unittest.main()
