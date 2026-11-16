"""Render ICON previews locally without reading Drive credentials or publishing URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from public_charts import policy
from public_charts.producer import produce_lead
from public_charts.sources import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", required=True, help="Exact UTC YYYYMMDDHH; no latest-cycle substitution")
    parser.add_argument("--hours", nargs="+", type=int, default=[0])
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    for hour in args.hours:
        policy.key("icon", args.cycle, hour, "surfce", "asia")
    root = args.workdir.resolve()
    if root == Path(__file__).resolve().parent or root == root.parent:
        parser.error("Use a dedicated preview directory")
    output = root / "preview"
    output.mkdir(parents=True, exist_ok=True)
    os.environ["PUBLIC_CHARTS_ICON_GRID_DIR"] = str(root / "grid-cache")
    path = output / "manifest.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = {}
    for row in saved.get("charts", []):
        target = output / row["filename"]
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == row["sha256"]:
            rows[policy.row_key(row)] = row
    for hour in args.hours:
        started = time.monotonic()
        def publish(product, domain, source, alias_of=None):
            key = policy.key("icon", args.cycle, hour, product, domain)
            if alias_of:
                original = rows[policy.key("icon", args.cycle, hour, *alias_of)]
                filename, digest, size = original["filename"], original["sha256"], original["size"]
            else:
                filename = f"icon_{args.cycle}_{hour:03}_{product}_{domain}.webp"
                target = output / filename
                shutil.copyfile(source, target)
                digest, size = hashlib.sha256(target.read_bytes()).hexdigest(), target.stat().st_size
            rows[key] = {"model": "icon", "cycle": args.cycle, "hour": hour, "product": product,
                         "domain": domain, "filename": filename, "sha256": digest, "size": size,
                         "file_id": "local-" + digest, "render_version": policy.render_version(args.cycle),
                         "url": (output / filename).as_uri()}
            atomic_json(path, {"schema_version": 1, "preview_only": True,
                              "models": {"icon": {"title": policy.TITLES["icon"], "published": False,
                                                   "planned_retention_cycles": 9}},
                              "charts": list(rows.values())})
            print(json.dumps({"event": "local_image_ready", "product": product, "domain": domain,
                              "hour": hour, "filename": filename, "bytes": size}), flush=True)
        result = produce_lead("icon", args.cycle, hour, root / "raw-cache", set(rows), publish,
                              checkpoint_root=root / "checkpoints",
                              progress=lambda stage, values: print(json.dumps(
                                  {"event": "inputs_ready", "stage": stage, "seconds": values["seconds"]}), flush=True))
        print(json.dumps({"event": "preview_lead_done", "cycle": args.cycle, "hour": hour,
                          "seconds": round(time.monotonic() - started, 3), **result}), flush=True)
        if not result["complete"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
