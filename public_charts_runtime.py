"""Prepare the fixed chart map cache and prune only derived checkpoints."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

from public_charts.policy import RETENTION


def install_runtime(cache):
    root=Path(__file__).resolve().parent
    files=[root/name for name in ("requirements-public-charts.txt","requirements-icon-charts.txt")]
    wanted={}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith(("#","-r ")):
                name,version=line.strip().split("==")
                wanted[name]=version
    identity=json.dumps([sys.version,sys.executable,sys.platform,platform.machine(),wanted],sort_keys=True)
    signature=hashlib.sha256(identity.encode()).hexdigest()
    directory=Path(cache)/("python-"+signature)
    python=directory/("Scripts/python.exe" if os.name=="nt" else "bin/python")
    marker=directory/".charts-ready"
    started=time.monotonic()
    check="import importlib.metadata as m,json,sys; assert all(m.version(k)==v for k,v in json.loads(sys.argv[1]).items())"
    ready=(python.is_file() and marker.is_file() and marker.read_text()==signature
           and subprocess.run([str(python),"-c",check,json.dumps(wanted)],capture_output=True).returncode==0)
    if not ready:
        if not python.is_file():
            subprocess.run([sys.executable,"-m","venv",str(directory)],check=True)
        subprocess.run([str(python),"-m","pip","install","--disable-pip-version-check","--no-input",
                        "-r",str(files[-1])],check=True)
        subprocess.run([str(python),"-c",check,json.dumps(wanted)],check=True)
        marker.write_text(signature)
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"],"a",encoding="utf-8") as stream:
            stream.write(str(python.parent)+"\n")
    print(json.dumps({"event":"runtime_ready","reused":ready,"seconds":round(time.monotonic()-started,3)}))
    return python


def install_transport(cache):
    root=Path(__file__).resolve().parent/"public_charts/transport"
    version=subprocess.run(["node","--version"],capture_output=True,text=True,check=True).stdout.strip()
    signature=hashlib.sha256((root/"package-lock.json").read_bytes()+(root/"upload.cjs").read_bytes()
                             +version.encode()+sys.platform.encode()).hexdigest()
    directory=Path(cache)/("transport-"+signature)
    directory.mkdir(parents=True,exist_ok=True)
    marker=directory/".charts-ready"
    started=time.monotonic()
    command=["node","--input-type=module","-e","await import('@actions/artifact')"]
    ready=(marker.is_file() and marker.read_text()==signature
           and (directory/"upload.cjs").is_file()
           and subprocess.run(command,cwd=directory,capture_output=True).returncode==0)
    if not ready:
        for name in ("package.json","package-lock.json","upload.cjs"):
            shutil.copy2(root/name,directory/name)
        subprocess.run(["npm","ci","--ignore-scripts","--no-audit","--no-fund"],cwd=directory,check=True)
        subprocess.run(command,cwd=directory,check=True)
        marker.write_text(signature)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"],"a",encoding="utf-8") as stream:
            stream.write("PUBLIC_CHARTS_TRANSPORT_SCRIPT="+str(directory/"upload.cjs")+"\n")
    print(json.dumps({"event":"transport_ready","reused":ready,"seconds":round(time.monotonic()-started,3)}))
    return directory


def prepare_laptop_checkpoints(work, revision):
    if not re.fullmatch(r"[a-f0-9]{64}",revision):
        raise ValueError("Invalid derived checkpoint revision")
    work = Path(work).resolve()
    raw = work/"raw-cache"
    root = raw/"checkpoints"
    marker = raw/".checkpoint-revision"
    if any(path.is_symlink() for path in (raw,root,marker)) or not root.resolve().is_relative_to(work):
        raise ValueError("Unsafe laptop checkpoint path")
    if marker.exists() and marker.read_text(encoding="ascii") == revision:
        return
    if root.exists():
        shutil.rmtree(root)
    raw.mkdir(parents=True,exist_ok=True)
    marker.write_text(revision,encoding="ascii")


def prune_checkpoints(root, *, max_bytes=None):
    root = Path(root).resolve()
    models = {model: [] for model in RETENTION}
    if not root.exists():
        return
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"\d{10}", directory.name):
            raise ValueError("Unexpected checkpoint directory")
        for model in RETENTION:
            if (directory / model).is_dir():
                models[model].append(directory.name)
    def remove(target):
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("Unsafe derived checkpoint path")
        shutil.rmtree(target)

    for model, cycles in models.items():
        for cycle in sorted(cycles, reverse=True)[RETENTION[model] + 1:]:
            remove(root/cycle/model)
    if max_bytes is not None:
        entries = []
        for model,cycles in models.items():
            for cycle in cycles:
                directory = root/cycle/model
                if directory.is_symlink():
                    raise ValueError("Unsafe derived checkpoint path")
                if directory.is_dir():
                    size = sum(path.stat().st_size for path in directory.rglob("*")
                               if path.is_file() and not path.is_symlink())
                    entries.append((cycle,model,size))
        total = sum(size for _,_,size in entries)
        for cycle,model,size in sorted(entries):
            if total <= max_bytes:
                break
            remove(root/cycle/model)
            total -= size
    for directory in root.iterdir():
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-maps", action="store_true")
    parser.add_argument("--prune-checkpoints", type=Path)
    parser.add_argument("--checkpoint-max-mib", type=int)
    parser.add_argument("--prepare-laptop-checkpoints", nargs=2, metavar=("CACHE", "REVISION"))
    parser.add_argument("--prune-inputs", type=Path)
    parser.add_argument("--prune-without-rehashing", action="store_true")
    parser.add_argument("--install-runtime",type=Path)
    parser.add_argument("--install-transport",type=Path)
    args = parser.parse_args()
    if args.prepare_laptop_checkpoints:
        prepare_laptop_checkpoints(*args.prepare_laptop_checkpoints)
    if args.install_runtime:
        install_runtime(args.install_runtime)
    if args.install_transport:
        install_transport(args.install_transport)
    if args.prepare_maps:
        import public_charts.plotter
        from cartopy.io import shapereader
        for resolution, category, name in (("50m", "physical", "land"),
                                            ("10m", "physical", "coastline"),
                                            ("50m", "cultural", "admin_0_boundary_lines_land"),
                                            ("10m", "cultural", "admin_1_states_provinces")):
            shapereader.natural_earth(resolution=resolution, category=category, name=name)
    if args.prune_checkpoints:
        if args.checkpoint_max_mib is not None and args.checkpoint_max_mib <= 0:
            parser.error("--checkpoint-max-mib must be positive")
        prune_checkpoints(args.prune_checkpoints,max_bytes=(
            None if args.checkpoint_max_mib is None else args.checkpoint_max_mib*1024**2))
    if args.prune_inputs:
        from public_charts.input_cache import prune
        prune(args.prune_inputs,verify_hash=not args.prune_without_rehashing)


if __name__ == "__main__":
    main()
