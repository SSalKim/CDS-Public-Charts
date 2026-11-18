"""Pure scheduling, product, and retention policy for the public chart pipeline."""
from datetime import datetime, timedelta, timezone
import re

from .dates import parse_cycle
from .chart_style import ASIA_LAYOUT_CUTOVER

RETENTION = {"aifs": 9, "gfs": 9, "aigfs": 9, "cmc": 5, "ecmwf": 5, "icon": 9, "ukmo": 9}
CYCLE_STEP = {"aifs": 6, "gfs": 6, "aigfs": 6, "cmc": 12, "ecmwf": 12, "icon": 6, "ukmo": 6}
SOURCE_CHECK_DELAY = timedelta(hours=3)
MODEL_SOURCE_CHECK_DELAY = {"icon": timedelta(hours=2, minutes=30)}
ZERO_ACRAIN_CUTOVER = "2026111612"
MODEL_START_CYCLE = {"ecmwf": "2026111606", "icon": "2026111612", "ukmo": "2026111800"}
TITLES = {"aifs": "ECMWF AIFS-Single (N320)", "gfs": "NCEP GFS (C768 L127)",
          "aigfs": "NCEP AIGFS (0.25\u00b0)", "cmc": "CMC GDPS (GEM 0.135\u02da L84)",
          "ecmwf": "ECMWF IFS (TCo1279 L137)"}
TITLES["icon"] = "DWD ICON (R03B07 L120)"
TITLES["ukmo"] = "UKMO (UM N1280 L70)"
MAX_IMAGE_BYTES = 14_000_000_000
PIPELINE_VERSION = 7
WINTER_DIAGNOSTIC_CUTOVER = "2026111800"
KEY_PATTERN = re.compile(r"(aifs|gfs|aigfs|cmc|ecmwf|icon|ukmo)/(\d{10})/(\d{3})/([a-z0-9_]+)/(asia|hkor)")


def render_version(cycle,model=None,product=None,domain=None):
    if (model in {"aifs","gfs","cmc","ecmwf","icon","ukmo"} and winter(model,cycle) and domain=="hkor"
            and product in {"surfce","acptot","acptot1","acrain","acrain1"}):
        return 8
    if cycle >= WINTER_DIAGNOSTIC_CUTOVER:
        if model in {"gfs","ukmo"} and domain=="hkor" and product in {"surfce","acptot","acptot1","acrain","acrain1"}:
            return 7
        return 5 if model=="ukmo" else 4
    return 3 if cycle >= ASIA_LAYOUT_CUTOVER else 2


def ukmo_full_catalog(cycle,hour):
    return parse_cycle(cycle).hour in (6,18) or hour>144


def ukmo_gate_hour(cycle,hour):
    return None if parse_cycle(cycle).hour in (6,18) else min(hour,144)


def ukmo_public(row):
    reference=ukmo_gate_hour(row["cycle"],row["hour"])
    return reference is None or bool(row.get("kma_confirmed_at")) and (row["hour"]<=144 or row.get("kma_reference_hour")==144)


def hours(model, cycle=None):
    step = 6 if model in {"aifs", "aigfs"} else 3
    if model not in RETENTION and model != "icon":
        raise ValueError("Unknown model")
    maximum = 144 if model in {"ecmwf","ukmo"} else 240
    if model=="ukmo":
        if cycle and parse_cycle(cycle).hour in (6,18):
            return tuple(range(0,70,3))
        if cycle and not winter(model,cycle):
            return (156,168)
        maximum=168
    if model == "icon":
        maximum = 120 if cycle and parse_cycle(cycle).hour in (6, 18) else 180
    return tuple(range(0, 85, step)) + (90, 96) + tuple(range(108, maximum + 1, 12))


def supports_cycle(model, cycle):
    hour = parse_cycle(cycle).hour
    if model=="ukmo":
        return hour in (0,6,12,18)
    if model == "icon":
        return hour in (0, 6, 12, 18)
    return hour in ((6, 18) if model == "ecmwf" else range(0, 24, CYCLE_STEP[model]))


def start_cycle(model, configured=None):
    return max(configured or "", MODEL_START_CYCLE.get(model, ""))


def hourly_gfs_snow(cycle):
    return cycle >= WINTER_DIAGNOSTIC_CUTOVER


def retention_current(model,cycle,now=None):
    now=now or datetime.now(timezone.utc)
    return model!="ukmo" or timedelta(0)<=now-parse_cycle(cycle)<=timedelta(hours=48)


def source_check_delay(model=None):
    return MODEL_SOURCE_CHECK_DELAY.get(model, SOURCE_CHECK_DELAY)


def source_check_due(cycle, now=None, *, model=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    return parse_cycle(cycle) + source_check_delay(model) <= now


def cycle_candidates(model, now=None, depth=2):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware time required")
    now = now.astimezone(timezone.utc)
    step = CYCLE_STEP[model]
    offset = 6 if model == "ecmwf" else 0
    latest = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        hours=(now.hour - offset) // step * step + offset)
    return [(latest - timedelta(hours=step * n)).strftime("%Y%m%d%H") for n in range(depth)]


def products(cycle):
    upper = "gph300" if int(cycle[4:6]) in (11, 12, 1, 2, 3, 4) else "gph200"
    return (upper, "gph500", "gph700", "ept850", "ept925", "wnd850", "wnd925")


def winter(model, cycle):
    return model != "aigfs" and int(cycle[4:6]) in (10, 11, 12, 1, 2, 3, 4)


def supports_acrain(cycle, hour):
    return hour > 0 or cycle >= ZERO_ACRAIN_CUTOVER


def key(model, cycle, hour, product, domain):
    value = f"{model}/{cycle}/{hour:03}/{product}/{domain}"
    if not KEY_PATTERN.fullmatch(value) or hour not in hours(model, cycle):
        raise ValueError("Invalid chart identity")
    if product not in set(products(cycle)) | {"surfce", "acptot", "acptot1", "acrain", "acrain1"}:
        raise ValueError("Product is outside the active nine-product catalog")
    if model=="ukmo" and not ukmo_full_catalog(cycle,hour) and (domain!="hkor" or product not in {"acptot","acptot1","acrain","acrain1"}):
        raise ValueError("UKMO only replaces Korea winter precipitation")
    if not supports_cycle(model, cycle):
        raise ValueError("Invalid model cycle")
    return value


def expected_keys(model, cycle, hour):
    if model=="ukmo" and not ukmo_full_catalog(cycle,hour):
        names=("acptot","acptot1","acrain","acrain1") if supports_acrain(cycle,hour) else ("acptot","acptot1")
        return {key(model,cycle,hour,product,"hkor") for product in names}
    keys = {key(model, cycle, hour, product, domain)
            for product in products(cycle) for domain in ("asia", "hkor")}
    keys.add(key(model, cycle, hour, "surfce", "asia"))
    # Korea's rain-only aliases share one physical file.
    for product in ("surfce", "acptot", "acptot1"):
        keys.add(key(model, cycle, hour, product, "hkor"))
    if supports_acrain(cycle, hour):
        for product in ("acrain", "acrain1"):
            keys.add(key(model, cycle, hour, product, "hkor"))
    return keys


def row_key(row):
    return key(row["model"], row["cycle"], int(row["hour"]), row["product"], row["domain"])


def unique_bytes(rows):
    files = {}
    for row in rows:
        file_id = str(row["file_id"])
        size = int(row["size"])
        if size < 1 or file_id in files and files[file_id] != size:
            raise ValueError("Inconsistent Drive image size")
        files[file_id] = size
    return sum(files.values())


def retain(rows, max_bytes=MAX_IMAGE_BYTES, *, account_budgets=None,now=None):
    """Evict whole old cycles, deduplicating aliases and preserving latest per model."""
    by_key = {row_key(row): dict(row) for row in rows}
    cycles = {model: sorted({row["cycle"] for row in by_key.values() if row["model"] == model}, reverse=True)
              for model in RETENTION}
    selected = {(model, cycle) for model, values in cycles.items() for cycle in values[:RETENTION[model]] if retention_current(model,cycle,now)}

    def selected_rows():
        return [row for row in by_key.values() if (row["model"], row["cycle"]) in selected]

    def over_budget():
        current = selected_rows()
        if account_budgets is None:
            return {None} if unique_bytes(current) > max_bytes else set()
        if any(row.get("storage_account", "primary") not in account_budgets for row in current):
            raise ValueError("Unknown image storage account")
        return {account for account, budget in account_budgets.items()
                if unique_bytes(row for row in current if row.get("storage_account", "primary") == account) > budget}

    while exceeded := over_budget():
        affected = {(row["model"], row["cycle"]) for row in selected_rows()
                    if None in exceeded or row.get("storage_account", "primary") in exceeded}
        removable = [(cycle, model) for model, cycle in selected
                     if (model, cycle) in affected and cycle != cycles[model][0]]
        if not removable:
            raise ValueError("Latest cycles alone exceed the image budget; cleanup refused")
        cycle, model = min(removable)
        selected.remove((model, cycle))
    kept = sorted(selected_rows(), key=lambda row: row_key(row))
    retained_ids = {row["file_id"] for row in kept}
    delete_ids = sorted({row["file_id"] for row in by_key.values()} - retained_ids)
    return kept, delete_ids


def public_manifest(rows,*,now=None):
    rows = [row for row in rows if row["model"]!="ukmo" or ukmo_public(row) and retention_current("ukmo",row["cycle"],now)]
    public = [{k: row[k] for k in ("model", "cycle", "hour", "product", "domain", "url", "size", "sha256", "file_id")}
              for row in rows]
    for source, target in zip(rows, public):
        target["render_version"] = source.get("render_version", 0)
        if source["model"]=="ukmo":
            for name in ("kma_confirmed_at","kma_reference_hour"):
                if name in source: target[name]=source[name]
    return {"schema_version": 1, "pipeline_version": PIPELINE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": {model: {"title": TITLES[model], "published": any(r["model"] == model for r in public),
                                "retention_cycles": RETENTION[model]}
                       for model in RETENTION}, "charts": public}
