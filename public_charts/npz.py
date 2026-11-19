"""Fast compressed .npz writing for checkpoints and prepared fields.

numpy's savez_compressed always uses DEFLATE level 6. On operational GFS checkpoints
(108 files per lead) level 1 writes 2.3x faster (6.6 s -> 2.8 s) for about 9% more
bytes, which matters because preparation is the coordinator's bottleneck. Files stay
ordinary .npz archives that np.load reads unchanged.
"""
import zipfile

LEVEL = 1


def save(stream, **arrays):
    import numpy as np  # some importers (field_cache) avoid importing numpy at module load
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=LEVEL) as archive:
        for name, value in arrays.items():
            with archive.open(name + ".npy", "w", force_zip64=True) as member:
                np.lib.format.write_array(member, np.asanyarray(value), allow_pickle=False)
