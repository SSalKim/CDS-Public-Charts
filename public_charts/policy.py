"""Pure scheduling, product, and retention policy for the public chart pipeline."""
from datetime import datetime, timedelta, timezone
import re

from .dates import parse_cycle
from .chart_style import ASIA_LAYOUT_CUTOVER

# ECMWF keeps up to 9 six-hourly cycles, and retention_current removes any older than 48 h.
RETENTION = {"aifs": 9, "gfs": 9, "aigfs": 9, "cmc": 5, "ecmwf": 9, "icon": 9, "ukmo": 9}
CYCLE_STEP = {"aifs": 6, "gfs": 6, "aigfs": 6, "cmc": 12, "ecmwf": 6, "icon": 6, "ukmo": 6}
# From this cycle GFS, AIFS and AIGFS run to 360 h, and ECMWF 00/12Z adds its own 252-360 h charts after the
# KMA ECMWF images, which stop at 240 h.
EXTENSION_CUTOVER = "2026112212"
ECMWF_EXTENSION_HOURS = tuple(range(252, 361, 12))
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
WINTER_REGION_CUTOVER = "2026111812"
UKMO_DATELINE_CUTOVER = "2026111812"
# 256-colour palette encoding starts here; earlier cycles keep their images and versions.
PALETTE_CUTOVER = "2026111912"
PALETTE_RENDER_STEP = 100
# KMA-style drawing: terrain-weighted MSLP smoothing and one km-based extrema policy for every
# model. Earlier cycles keep their images and versions.
KMA_STYLE_CUTOVER = "2026111912"
KMA_STYLE_RENDER_STEP = 200
# Model-calibrated display smoothing (see quality.MODEL_TERRAIN_KM): km-based contour smoothing,
# per-model high-terrain MSLP smoothing, below-ground smoothing of 925-700 hPa, no plateau ring pruning.
SMOOTHING_CUTOVER = "2026112018"
SMOOTHING_RENDER_STEP = 400
KEY_PATTERN = re.compile(r"(aifs|gfs|aigfs|cmc|ecmwf|icon|ukmo)/(\d{10})/(\d{3})/([a-z0-9_]+)/(asia|hkor)")


def winter_composite(cycle,model,product,domain):
    return (model in {"aifs","gfs","cmc","ecmwf","icon","ukmo"} and winter(model,cycle) and domain=="hkor"
            and product in {"surfce","acptot","acptot1","acrain","acrain1"})


def palette_enabled(cycle):
    return cycle>=PALETTE_CUTOVER


def kma_style(cycle):
    return cycle>=KMA_STYLE_CUTOVER


def model_smoothing(cycle):
    return cycle>=SMOOTHING_CUTOVER


def render_version(cycle,model=None,product=None,domain=None):
    base=_layout_render_version(cycle,model,product,domain)
    return (base+(PALETTE_RENDER_STEP if palette_enabled(cycle) else 0)
            +(KMA_STYLE_RENDER_STEP if kma_style(cycle) else 0)
            +(SMOOTHING_RENDER_STEP if model_smoothing(cycle) else 0))


def _layout_render_version(cycle,model=None,product=None,domain=None):
    if winter_composite(cycle,model,product,domain):
        if cycle>=WINTER_REGION_CUTOVER and product in {"acrain","acrain1"}:
            return 12
        return 10 if cycle>=WINTER_REGION_CUTOVER else 9 if model=="ukmo" else 8
    if model=="ukmo" and domain=="asia" and ukmo_dateline_enabled(cycle):
        return 11
    if cycle >= WINTER_DIAGNOSTIC_CUTOVER:
        if model in {"gfs","ukmo"} and domain=="hkor" and product in {"surfce","acptot","acptot1","acrain","acrain1"}:
            return 7
        return 5 if model=="ukmo" else 4
    return 3 if cycle >= ASIA_LAYOUT_CUTOVER else 2


def render_is_current(row):
    cycle,model,product,domain=row["cycle"],row["model"],row.get("product"),row.get("domain")
    if cycle<WINTER_REGION_CUTOVER and winter_composite(cycle,model,product,domain):
        return True
    if model=="ukmo" and domain=="asia" and not ukmo_dateline_enabled(cycle):
        return True
    return row.get("render_version")==render_version(cycle,model,product,domain)


def ukmo_dateline_enabled(cycle):
    return cycle>=UKMO_DATELINE_CUTOVER


def ukmo_full_catalog(cycle,hour):
    return parse_cycle(cycle).hour in (6,18) or hour>144


def ukmo_gate_hour(cycle,hour):
    return None if parse_cycle(cycle).hour in (6,18) else min(hour,144)


def ukmo_public(row):
    reference=ukmo_gate_hour(row["cycle"],row["hour"])
    return reference is None or bool(row.get("kma_confirmed_at")) and (row["hour"]<=144 or row.get("kma_reference_hour")==144)


def extended(cycle):
    return bool(cycle) and cycle >= EXTENSION_CUTOVER


def hours(model, cycle=None):
    step = 6 if model in {"aifs", "aigfs"} else 3
    if model not in RETENTION and model != "icon":
        raise ValueError("Unknown model")
    maximum = 144 if model in {"ecmwf","ukmo"} else 240
    if model in {"aifs", "aigfs", "gfs"} and extended(cycle):
        maximum = 360
    if model == "ecmwf" and cycle and parse_cycle(cycle).hour in (0, 12):
        return ECMWF_EXTENSION_HOURS
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
    if model == "ecmwf":
        return hour in (6, 18) or hour in (0, 12) and extended(cycle)
    return hour in range(0, 24, CYCLE_STEP[model])


def start_cycle(model, configured=None):
    return max(configured or "", MODEL_START_CYCLE.get(model, ""))


def hourly_gfs_snow(cycle):
    return cycle >= WINTER_DIAGNOSTIC_CUTOVER


# UKMO and ECMWF charts (ECMWF 06/18Z and 00/12Z 252-360 h) are shown for 48 h after the cycle, then removed;
# the viewer falls back to KMA's own images (UKMO to 144 h, ECMWF 00/12Z to 240 h).
PUBLIC_WINDOW_MODELS = frozenset({"ukmo", "ecmwf"})
PUBLIC_WINDOW = timedelta(hours=48)


def retention_current(model,cycle,now=None):
    now=now or datetime.now(timezone.utc)
    return model not in PUBLIC_WINDOW_MODELS or timedelta(0)<=now-parse_cycle(cycle)<=PUBLIC_WINDOW


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
    # Walk back six-hourly and keep only cycles the model produces (ECMWF 00/12Z join at the extension cutover).
    latest = now.replace(hour=now.hour // 6 * 6, minute=0, second=0, microsecond=0)
    result = []
    for n in range(4 * depth + 8):
        cycle = (latest - timedelta(hours=6 * n)).strftime("%Y%m%d%H")
        if supports_cycle(model, cycle):
            result.append(cycle)
            if len(result) == depth:
                break
    return result


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
    rows = [row for row in rows if retention_current(row["model"],row["cycle"],now) and (row["model"]!="ukmo" or ukmo_public(row))]
    public = [{k: row[k] for k in ("model", "cycle", "hour", "product", "domain", "url", "size", "sha256", "file_id")}
              for row in rows]
    for source, target in zip(rows, public):
        target["render_version"] = source.get("render_version", 0)
        if source["model"]=="ukmo":
            for name in ("kma_confirmed_at","kma_reference_hour"):
                if name in source: target[name]=source[name]
        if source.get("r2"):
            target["r2"]=source["r2"]
    return {"schema_version": 1, "pipeline_version": PIPELINE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "models": {model: {"title": TITLES[model], "published": any(r["model"] == model for r in public),
                                "retention_cycles": RETENTION[model]}
                       for model in RETENTION}, "charts": public}
