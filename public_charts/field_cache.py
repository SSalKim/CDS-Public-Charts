"""Verified regional ICON fields survive an interrupted multi-variable preparation."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import uuid
import zipfile

TTL_SECONDS = 24*3600
MAX_BYTES = 256*1024**2


def paths(root, signature):
    if Path(root).is_symlink():
        raise ValueError("Unsafe partial field cache root")
    root = Path(root).resolve()
    cycle, hour = signature["cycle"], signature["hour"]
    from .dates import parse_cycle
    if parse_cycle(cycle).strftime("%Y%m%d%H") != cycle or type(hour) is not int or not 0 <= hour <= 180:
        raise ValueError("Invalid partial field identity")
    tag = hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
    directory = root/cycle/f"{hour:03}"
    if not directory.resolve().is_relative_to(root) or any(path.is_symlink() for path in (directory,directory.parent)):
        raise ValueError("Unsafe partial field cache path")
    return directory/f"{tag}.npz", directory/f"{tag}.json"


def read(root, signature, *, now=None):
    import numpy as np
    path, meta = paths(root,signature)
    try:
        if path.is_symlink() or meta.is_symlink():
            return None
        info = json.loads(meta.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now)-info["cached_at"]
        if info["signature"] != signature or not 0 <= age <= TTL_SECONDS or path.stat().st_size > 32*1024**2:
            return None
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != info["sha256"]:
            return None
        with np.load(io.BytesIO(payload),allow_pickle=False) as archive:
            values, lat, lon = (archive[name] for name in ("values","latitude","longitude"))
        south,north,west,east = signature["bounds"]
        spacing = signature["spacing"]
        if (values.shape != (round((north-south)/spacing)+1,round((east-west)/spacing)+1)
                or lat.ndim != 1 or lon.ndim != 1 or values.shape != (lat.size,lon.size)
                or not all(np.isfinite(a).all() for a in (values,lat,lon))
                or not np.allclose(lat,np.arange(south,north+spacing/2,spacing),rtol=0,atol=1e-6)
                or not np.allclose(lon,np.arange(west,east+spacing/2,spacing),rtol=0,atol=1e-6)
                or not isinstance(info["metadata"].get("native_grid_uuid"),str)
                or not info["metadata"]["native_grid_uuid"]):
            return None
        return values,lat,lon,info["metadata"],info["sources"]
    except (OSError,ValueError,KeyError,TypeError,EOFError,zipfile.BadZipFile):
        return None


def save(root, signature, field, sources):
    import numpy as np
    path, meta = paths(root,signature)
    path.parent.mkdir(parents=True,exist_ok=True)
    tag = f"{os.getpid()}-{uuid.uuid4().hex}.tmp"
    temporary = path.with_name(path.name+tag)
    try:
        with temporary.open("wb") as stream:
            np.savez_compressed(stream,values=field.values,latitude=field.latitude,longitude=field.longitude)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(path)
        temporary = meta.with_name(meta.name+tag)
        temporary.write_text(json.dumps({"signature":signature,"sha256":digest,"cached_at":time.time(),
                                          "metadata":field.metadata,"sources":sources}),encoding="utf-8")
        temporary.replace(meta)
    finally:
        temporary.unlink(missing_ok=True)


def prune(work, *, now=None, max_bytes=MAX_BYTES, verify_hash=True):
    from .input_cache import location, valid
    workspace = Path(work).resolve()
    root = workspace/"raw-cache"/"partial-fields"
    if root.is_symlink() or not root.resolve().is_relative_to(workspace):
        raise ValueError("Unsafe partial field cache root")
    now = time.time() if now is None else now
    candidates, completeness = [], {}
    for path in root.glob("*/*/*"):
        if (not path.resolve().is_relative_to(root) or path.is_symlink()
                or any(p.is_symlink() for p in (path.parent,path.parent.parent))):
            raise ValueError("Unsafe partial field cache entry")
        orphan = re.fullmatch(r"[a-f0-9]{64}\.npz",path.name) and not path.with_suffix(".json").exists()
        temporary = re.fullmatch(r"[a-f0-9]{64}\.(?:npz|json)[0-9]+-[a-f0-9]{32}\.tmp",path.name)
        if path.is_file() and (orphan or temporary):
            path.unlink()
    for meta in root.glob("*/*/*.json"):
        if meta.is_symlink() or any(p.is_symlink() for p in (meta.parent,meta.parent.parent)):
            raise ValueError("Unsafe partial field cache entry")
        path = meta.with_suffix(".npz")
        if path.is_symlink():
            raise ValueError("Unsafe partial field payload")
        good, complete, stamp = False, False, 0
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            signature = info["signature"]
            expected, _ = paths(root,signature)
            stamp = info["cached_at"]
            good = (expected == path and 0 <= now-stamp <= TTL_SECONDS
                    and path.is_file() and (not verify_hash or
                                           info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()))
            target = {"model":"icon","cycle":signature["cycle"],"hour":signature["hour"]}
            identity = target["cycle"],target["hour"]
            if good and identity not in completeness:
                completeness[identity] = valid(location(work,target),target,now,verify_hash=verify_hash)
            complete = completeness.get(identity,False)
        except (OSError,ValueError,KeyError,TypeError):
            pass
        if good:
            candidates.append((not complete,stamp,path,meta,path.stat().st_size+meta.stat().st_size))
        else:
            path.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
    total = sum(item[-1] for item in candidates)
    for _,_,path,meta,size in sorted(candidates):
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        total -= size
    return total
