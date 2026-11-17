"""Render one ready lead, publishing completed products without waiting for rain."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import gc
import time

import numpy as np
from PIL import Image

from . import policy
from .chart_style import PUBLIC_PROFILE, PUBLIC_IMAGE_SIZES
from .diagnostics import interpolate
from .extended_sources import ExtraFields, winter_fields
from .plotter import render
from .quality import closed_contour_predicate
from .sources import MODELS, collect, accumulation_hours, NotReady

WINTER_COMPOSITE_SIZES = {False: (1750, 660), True: (600, 2000)}
CUMULATIVE_WORK_BUDGET_SECONDS = 300


def crop(arrays, domain):
    if domain == "asia":
        return arrays
    latitude, longitude = arrays["latitude"], arrays["longitude"]
    rows = np.flatnonzero((latitude >= 29) & (latitude <= 45))
    cols = np.flatnonzero((longitude >= 115) & (longitude <= 138))
    selected = {key: value[rows] if key == "latitude" else value[cols] if key == "longitude"
                else value[np.ix_(rows, cols)] for key, value in arrays.items()
                if key not in {"precipitation", "precipitation_latitude", "precipitation_longitude"}}
    if "precipitation" in arrays:
        rain_lat = arrays.get("precipitation_latitude", latitude)
        rain_lon = arrays.get("precipitation_longitude", longitude)
        yi = np.flatnonzero((rain_lat >= 29) & (rain_lat <= 45))
        xi = np.flatnonzero((rain_lon >= 115) & (rain_lon <= 138))
        selected["precipitation"] = arrays["precipitation"][np.ix_(yi, xi)]
        if "precipitation_latitude" in arrays:
            selected.update(precipitation_latitude=rain_lat[yi], precipitation_longitude=rain_lon[xi])
    return selected


def compose(frames, vertical):
    width, height = frames[0].size
    if any(frame.size != (width, height) for frame in frames):
        raise ValueError("Winter panel sizes differ")
    result = Image.new("RGB", (width, height * len(frames)) if vertical else (width * len(frames), height), "white")
    for index, frame in enumerate(frames):
        result.paste(frame, (0, index * height) if vertical else (index * width, 0))
    return result.resize(WINTER_COMPOSITE_SIZES[vertical], Image.Resampling.LANCZOS) if len(frames) == 3 else result


def encode(frame, path):
    """One final lossless encode; no intermediate PNG and no second map render."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    frame.save(temporary, format="WEBP", lossless=True, quality=80, method=4)
    with Image.open(temporary) as saved:
        saved.load()
        if saved.format != "WEBP" or saved.size != frame.size:
            raise ValueError("Invalid encoded chart")
        if np.asarray(saved.convert("RGB")).std() < 10:
            raise ValueError("Encoded chart is blank")
    temporary.replace(path)


def prepare_surface(model, cycle, hour, root, base, completed, force, checkpoint_root, progress):
    """Fetch winter/rain inputs while the main renderer draws upper-air charts."""
    started = time.monotonic()
    interval = accumulation_hours(model, hour) or 0
    metadata = {}
    if hour:
        base, metadata, _ = collect(model, cycle, hour, root)
    else:
        base = {**base, "precipitation": np.zeros_like(base["pressure_msl"], dtype=np.float64)}
    extra = ExtraFields(root, model, cycle, deadline=started+CUMULATIVE_WORK_BUDGET_SECONDS)
    if checkpoint_root is not None:
        extra.checkpoints = Path(checkpoint_root) / cycle / model
    selected = crop(base, "hkor")
    def done(product):
        return not force and policy.key(model, cycle, hour, product, "hkor") in completed
    needs_total = hour and not (done("acrain") and done("acrain1"))
    winter_products = (("acptot", "acptot1", "acrain", "acrain1")
                       if policy.supports_acrain(cycle, hour) else ("acptot", "acptot1"))
    needs_winter = policy.winter(model, cycle) and not all(done(name) for name in winter_products)
    total = None
    if needs_total or needs_winter:
        if progress:
            progress("surface_phase", {"phase": "checkpoint_wait"})
        with extra.checkpoint_lock():
            if needs_total:
                if progress:
                    progress("surface_phase", {"phase": "total_precipitation"})
                total = extra.total_rain(hour)
            if needs_winter:
                if progress:
                    progress("surface_phase", {"phase": "winter_diagnostics"})
                selected, _ = winter_fields(extra, base, hour, interval)
    if progress:
        progress("surface", {"observed_at": datetime.now(timezone.utc).isoformat(),
                             "seconds": round(time.monotonic() - started, 3),
                             "sources": metadata.get("sources", []) + extra.client.records})
    return base, selected, total


def produce_lead(model, cycle, hour, root, existing, publish, *, force=False, checkpoint_root=None,
                 progress=None):
    """publish(product, domain, path, alias_of=None) commits one verified image."""
    root = Path(root)
    completed = set(existing)
    interval = accumulation_hours(model, hour) or 0
    timings = {"plot_seconds": 0., "encode_seconds": 0.}

    def done(product, domain):
        return not force and policy.key(model, cycle, hour, product, domain) in completed

    def emit(product, domain, frame=None, alias_of=None):
        if done(product, domain):
            return
        path = root / "images" / model / f"{cycle}_{hour:03}_{product}_{domain}.webp"
        if frame is not None:
            started = time.monotonic()
            encode(frame, path)
            timings["encode_seconds"] += time.monotonic() - started
        publish(product, domain, path if frame is not None else None, alias_of=alias_of)
        completed.add(policy.key(model, cycle, hour, product, domain))
        if path.exists():
            path.unlink()

    def draw(arrays, product, domain, *, accumulation=None, pressure_only=False):
        started = time.monotonic()
        if progress:
            progress("product_started", {"product": product, "domain": domain})
        frame = render(arrays, cycle, hour, product, domain, None, accumulation,
                      model_name=MODELS[model]["name"], precipitation_hours=interval,
                      precipitation_field="precipitation", source_note=MODELS[model]["attribution"],
                      pressure_only=pressure_only, upper_level_override=200 if product == "gph200" else None,
                      physical_barb_spacing=True, profile=PUBLIC_PROFILE, model_id=model,
                      contour_filter=closed_contour_predicate(arrays), diagnostic_snowfall=model == "gfs",
                      return_frame=True)
        gc.collect()
        timings["plot_seconds"] += time.monotonic() - started
        if progress:
            progress("product_finished", {"product": product, "domain": domain,
                                           "seconds": round(time.monotonic()-started,3)})
        return frame

    started = time.monotonic()
    base, metadata, _ = collect(model, cycle, hour, root, include_precipitation=False)
    if progress:
        progress("base", {"observed_at": datetime.now(timezone.utc).isoformat(),
                          "seconds": round(time.monotonic() - started, 3),
                          "sources": metadata.get("sources", [])})
    surface_needed = any(key not in completed or force for key in policy.expected_keys(model, cycle, hour)
                         if key.split("/")[-2] not in policy.products(cycle))
    # Only immutable arrays are shared; all HTTP and checkpoint writes stay on one thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="chart-rain-prefetch") as downloads:
        surface = (downloads.submit(prepare_surface, model, cycle, hour, root, base, set(completed),
                                    force, checkpoint_root, progress) if surface_needed else None)
        cropped = {domain: crop(base, domain) for domain in ("asia", "hkor")}
        for product in policy.products(cycle):
            for domain, selected in cropped.items():
                if not done(product, domain):
                    emit(product, domain, draw(selected, product, domain))
        if surface is None:
            return {"complete": True, **{key: round(value, 3) for key, value in timings.items()}}
        try:
            base, selected, total = surface.result()
        except NotReady as error:
            return {"complete": False, "pending": str(error),
                    "pending_reason": getattr(error,"reason",None),
                    **{key: round(value, 3) for key, value in timings.items()}}

    try:
        if not done("surfce", "asia"):
            emit("surfce", "asia", draw(base, "surfce", "asia"))
        winter = policy.winter(model, cycle)
        if winter:
            required = (("acptot", "acptot1", "acrain", "acrain1")
                        if policy.supports_acrain(cycle, hour) else ("acptot", "acptot1"))
            if all(done(name, "hkor") for name in required):
                emit("surfce", "hkor", alias_of=("acptot", "hkor"))
                return {"complete": policy.expected_keys(model, cycle, hour) <= completed}
        if winter:
            amount = interpolate(total, selected["latitude"], selected["longitude"]) if total is not None else None
            if hour == 0:
                amount = np.zeros_like(selected["pressure_msl"], dtype=np.float64)
            for name, components in (("acptot", ("acptot", "acptot_snow", "acptot_snowdepth")),
                                     ("acrain", ("acrain", "acrain_snow", "acrain_snowdepth"))):
                if name == "acrain" and not policy.supports_acrain(cycle, hour):
                    continue
                if done(name, "hkor") and done(name + "1", "hkor"):
                    continue
                panels = [draw(selected, product, "hkor", accumulation=amount if product == "acrain" else None)
                          for product in components]
                for suffix, vertical in (("", False), ("1", True)):
                    if not done(name + suffix, "hkor"):
                        emit(name + suffix, "hkor", compose(panels, vertical))
            emit("surfce", "hkor", alias_of=("acptot", "hkor"))
        else:
            if not done("surfce", "hkor"):
                emit("surfce", "hkor", draw(selected, "surfce", "hkor"))
            for name in ("acptot", "acptot1"):
                emit(name, "hkor", alias_of=("surfce", "hkor"))
            if policy.supports_acrain(cycle, hour) and not done("acrain", "hkor"):
                amount = (np.zeros_like(selected["pressure_msl"], dtype=np.float64) if hour == 0 else
                          interpolate(total, selected["latitude"], selected["longitude"]))
                emit("acrain", "hkor", draw(selected, "acrain", "hkor", accumulation=amount))
            if policy.supports_acrain(cycle, hour):
                emit("acrain1", "hkor", alias_of=("acrain", "hkor"))
    except NotReady as error:
        return {"complete": False, "pending": str(error)}
    return {"complete": policy.expected_keys(model, cycle, hour) <= completed,
            **{key: round(value, 3) for key, value in timings.items()}}
