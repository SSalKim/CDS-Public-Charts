"""Bounded, verified regional arrays shared with the next coordinator batch."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

TTL_SECONDS = 24*3600
MAX_BYTES = 512*1024**2


def location(work, target):
    from . import policy
    policy.expected_keys(target["model"], target["cycle"], target["hour"])
    root=Path(work).resolve()
    result=root/"raw-cache"/"prepared"/target["model"]/target["cycle"]/f'{target["hour"]:03}'
    if not result.resolve().is_relative_to(root) or any(path.is_symlink() for path in (result,*result.parents) if path != root and root in path.parents):
        raise ValueError("Unsafe prepared input path")
    return result


def valid(directory, target, now=None):
    try:
        if directory.is_symlink() or any((directory/name).is_symlink() for name in ("fields.npz", "metadata.json")):
            return False
        meta = json.loads((directory/"metadata.json").read_text(encoding="utf-8"))
        return (all(meta.get(name) == target[name] for name in ("model", "cycle", "hour"))
                and 0 <= (now if now is not None else time.time())-meta["cached_at"] <= TTL_SECONDS
                and meta["prepared_sha256"] == hashlib.sha256((directory/"fields.npz").read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def restore(work, target, raw):
    source = location(work, target)
    destination = Path(raw)/"fields"/target["cycle"]/target["model"]/f'{target["hour"]:03}'
    if destination.exists() or not valid(source, target):
        return False
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("fields.npz", "metadata.json"):
        shutil.copyfile(source/name, destination/name)
    return True


def save(work, target, raw):
    source = Path(raw)/"fields"/target["cycle"]/target["model"]/f'{target["hour"]:03}'
    if not (source/"fields.npz").is_file() or not (source/"metadata.json").is_file():
        return
    metadata = json.loads((source/"metadata.json").read_text(encoding="utf-8"))
    if not all(metadata.get(name) == target[name] for name in ("model", "cycle", "hour")):
        raise ValueError("Prepared input identity mismatch")
    destination = location(work, target)
    destination.mkdir(parents=True, exist_ok=True)
    metadata.update(cached_at=time.time(), prepared_sha256=hashlib.sha256((source/"fields.npz").read_bytes()).hexdigest())
    tag = f"{os.getpid()}-{uuid.uuid4().hex}"
    temporary = destination/f"{tag}.tmp"
    shutil.copyfile(source/"fields.npz", temporary)
    temporary.replace(destination/"fields.npz")
    temporary = destination/f"{tag}.json.tmp"
    temporary.write_text(json.dumps(metadata), encoding="utf-8")
    temporary.replace(destination/"metadata.json")


def prune(work, *, now=None, max_bytes=MAX_BYTES):
    workspace=Path(work).resolve()
    root=workspace/"raw-cache"/"prepared"
    if root.is_symlink() or not root.resolve().is_relative_to(workspace):
        raise ValueError("Unsafe prepared cache root")
    if not root.exists():
        return
    candidates = []
    for directory in root.glob("*/*/*"):
        if not directory.is_dir():
            continue
        meta=directory/"metadata.json"
        if directory.is_symlink() or not directory.resolve().is_relative_to(root):
            raise ValueError("Unsafe prepared cache directory")
        try:
            target = dict(zip(("model", "cycle", "hour"), directory.relative_to(root).parts))
            target["hour"] = int(target["hour"])
            location(work, target)
            good = valid(directory, target, now)
        except (ValueError, TypeError):
            raise ValueError("Unexpected prepared input identity") from None
        if not good:
            shutil.rmtree(directory)
            continue
        candidates.append((meta.stat().st_mtime, directory, sum(path.stat().st_size for path in directory.iterdir() if path.is_file())))
    total = sum(size for _, _, size in candidates)
    for _, directory, size in sorted(candidates):
        if total <= max_bytes:
            break
        shutil.rmtree(directory)
        total -= size
