"""DWD ICON native triangular fields; isolated from the active production roster."""
import bz2
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import eccodes as ec
import metpy.calc as mpcalc
from metpy.units import units
import numpy as np
import requests

from .dates import parse_cycle
from .sources import BOUNDS, Decoded, GRAVITY, Provider, atomic_json


BASE = "https://opendata.dwd.de/weather/nwp"
GRID_VERSION = 1
_REMAPPERS = {}
_GRID_LOCK = threading.Lock()
_FEED_LOCK = threading.Lock()
_FEED_FAILURES = {}
FEED_COOLDOWN_SECONDS = 300
SURFACE = {"pressure_msl": "PMSL", "surface_pressure": "PS", "terrain_height": "HSURF",
           "temperature_2m": "T_2M", "dewpoint_2m": "TD_2M", "relative_humidity_2m": "RELHUM_2M",
           "wind_u_10m": "U_10M", "wind_v_10m": "V_10M", "precipitation": "TOT_PREC"}
PRESSURE = {"geopotential": "FI", "temperature": "T", "wind_u": "U", "wind_v": "V",
            "relative_humidity": "RELHUM"}


def source_urls(cycle, hour, parameter, level=None, *, constant=False):
    date = parse_cycle(cycle)
    maximum = 120 if date.hour in (6, 18) else 180
    if date.hour not in (0, 6, 12, 18) or hour < 0 or hour > maximum or (hour > 78 and hour % 3):
        raise ValueError("Unsupported ICON cycle or native forecast step")
    step = 0 if constant else hour
    run = date.strftime("%Y-%m-%dT%H") + "%3A00"
    pressure = f"lvt1/100/lv1/{level * 100}/" if level is not None else ""
    primary = f"{BASE}/v1/m/icon/p/{parameter}/{pressure}r/{run}/s/PT{step:03}H00M.grib2"
    kind = "time-invariant" if constant else "pressure-level" if level is not None else "single-level"
    suffix = "" if constant else f"_{step:03}" + (f"_{level}" if level is not None else "")
    legacy = (f"{BASE}/icon/grib/{cycle[8:]}/{parameter.lower()}/"
              f"icon_global_icosahedral_{kind}_{cycle}{suffix}_{parameter}.grib2.bz2")
    return primary, legacy


def read_native(payload, cycle, hour, parameter, level=None, *, constant=False):
    if payload.startswith(b"BZh"):
        try:
            payload = bz2.decompress(payload)
        except (OSError, EOFError) as error:
            raise ValueError("Invalid compressed ICON GRIB") from error
    if (not payload.startswith(b"GRIB") or not payload.endswith(b"7777") or len(payload) < 20
            or int.from_bytes(payload[8:16], "big") != len(payload)):
        raise ValueError("Expected exactly one complete ICON GRIB message")
    handle = ec.codes_new_from_message(payload)
    try:
        def get(key):
            try:
                return ec.codes_get(handle, key)
            except ec.CodesInternalError:
                return None
        original_step_units = get("stepUnits")
        ec.codes_set(handle, "stepUnits", "h")
        meta = {name: get(name) for name in ("dataDate", "dataTime", "startStep", "endStep", "stepUnits",
                 "stepType", "units", "shortName", "gridType", "uuidOfHGrid", "numberOfDataPoints",
                 "typeOfLevel", "level", "uvRelativeToGrid", "numberOfMissing", "packingError")}
        if (str(meta["dataDate"]) != cycle[:8] or int(meta["dataTime"] or 0) != int(cycle[8:]) * 100
                or float(meta["endStep"]) != (0 if constant else hour) or meta["stepUnits"] not in (1, "h")):
            raise ValueError("ICON field has the wrong initialization or forecast step")
        if meta["gridType"] != "unstructured_grid" or not meta["uuidOfHGrid"]:
            raise ValueError("Expected an identified ICON unstructured grid")
        if level is not None and (meta["typeOfLevel"] != "isobaricInhPa" or int(meta["level"]) != level):
            raise ValueError("ICON pressure level mismatch")
        wind_names = {"U": {"u"}, "V": {"v"}, "U_10M": {"10u"}, "V_10M": {"10v"}}
        if parameter in wind_names:
            # GRIB template 3.101 omits component flags; DWD's named U/V are zonal/meridional.
            if meta["uvRelativeToGrid"] not in (0, None) or (meta["uvRelativeToGrid"] is None and
                                                          meta["shortName"] not in wind_names[parameter]):
                raise ValueError("ICON winds must be identified Earth-relative components")
        values = np.asarray(ec.codes_get_values(handle), dtype=np.float32)
        if (values.size != meta["numberOfDataPoints"] or meta["numberOfMissing"]
                or not np.isfinite(values).all() or np.any(np.abs(values) >= 1e15)):
            raise ValueError("Missing or invalid ICON source values")
        return values, {**meta, "icon_parameter": parameter, "original_step_units": original_step_units}
    finally:
        ec.codes_release(handle)


def feed_health_path(feed):
    root=os.environ.get("PUBLIC_CHARTS_FIELD_CACHE_DIR")
    if not root:
        return None
    directory=Path(root)/".feed-health"
    if Path(root).is_symlink() or directory.is_symlink():
        raise ValueError("Unsafe ICON feed health path")
    return directory/f"{feed}.json"


def feed_unhealthy(feed,now):
    with _FEED_LOCK:
        until=_FEED_FAILURES.get(feed,(0,0))[1]
    path=feed_health_path(feed)
    if path is not None:
        try:
            if path.is_symlink():
                raise ValueError("Unsafe ICON feed health file")
            value=json.loads(path.read_text(encoding="utf-8"))
            shared=value.get("until",0)
            if type(shared) in (int,float) and now<shared<=now+FEED_COOLDOWN_SECONDS:
                until=max(until,shared)
        except (OSError,ValueError,TypeError):
            pass
    return now<until<=now+FEED_COOLDOWN_SECONDS


def native(client, cycle, hour, parameter, level=None, *, constant=False):
    client.context={"model":"icon","cycle":cycle,"hour":hour,"parameter":parameter,"level":level}
    def retrieve(url):
        # Switch exact-cycle feeds promptly instead of retrying a stalled socket for minutes.
        started = time.monotonic()
        try:
            payload = client.get(url,attempts=1,timeout=(5,8),total_timeout=15)
        except requests.RequestException as error:
            feed = "v1" if "/v1/" in url else "legacy"
            with _FEED_LOCK:
                count, _ = _FEED_FAILURES.get(feed, (0, 0))
                until=time.time()+FEED_COOLDOWN_SECONDS
                _FEED_FAILURES[feed] = (count+1,until)
            path=feed_health_path(feed)
            if path is not None:
                try:
                    atomic_json(path,{"until":until})
                except OSError:
                    pass
            print(json.dumps({"event":"icon_feed_retry","model":"icon","cycle":cycle,"hour":hour,
                              "parameter":parameter,"level":level,"feed":"v1" if "/v1/" in url else "legacy",
                              "error":type(error).__name__,"seconds":round(time.monotonic()-started,3)}),flush=True)
            raise
        result = read_native(payload, cycle, hour, parameter, level, constant=constant)
        return result
    urls = source_urls(cycle, hour, parameter, level, constant=constant)
    now=time.time()
    health={feed:feed_unhealthy(feed,now) for feed in ("v1","legacy")}
    urls = sorted(urls,key=lambda url:(health["v1" if "/v1/" in url else "legacy"],"/v1/" in url))
    return Provider._mirrors(urls, retrieve)


def inverse_distance(distance):
    return 1 / np.maximum(distance, 1.) ** 2


class IconRemapper:
    """Reuse pyresample's spherical neighbour lookup for all fields of one grid."""
    def __init__(self, client, cycle, grid_uuid, count, root, *, spacing=.125, bounds=BOUNDS):
        from pyresample import geometry, kd_tree
        south, north, west, east = bounds
        self.latitude = np.arange(south, north + spacing / 2, spacing)
        self.longitude = np.arange(west, east + spacing / 2, spacing)
        self.shape = (len(self.latitude), len(self.longitude))
        self.count = count
        signature = {"version": GRID_VERSION, "uuid": grid_uuid, "count": count,
                     "spacing": spacing, "bounds": list(bounds), "neighbours": 4, "radius_m": 35000}
        tag = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        path, meta = root / f"{tag}.npz", root / f"{tag}.json"
        if path.exists() and meta.exists():
            info = json.loads(meta.read_text(encoding="utf-8"))
            if info.get("signature") == signature and info.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest():
                with np.load(path, allow_pickle=False) as data:
                    self.neighbours = tuple(data[name] for name in ("input", "output", "index", "distance"))
                self.validate()
                return
        coordinates = []
        for parameter in ("CLAT", "CLON"):
            values, metadata = native(client, cycle, 0, parameter, constant=True)
            if metadata["uuidOfHGrid"] != grid_uuid or len(values) != count:
                raise ValueError("ICON cell coordinates do not match the data grid")
            if metadata["units"] in {"rad", "radian", "radians"}:
                values = np.rad2deg(values.astype(np.float64))
            elif metadata["units"] not in {"degree", "degrees", "degrees_north", "degrees_east", "Degree N", "Degree E"}:
                raise ValueError("Unknown ICON cell-coordinate units")
            coordinates.append(values)
        latitude, longitude = coordinates
        if np.any(np.abs(latitude) > 90) or np.any(np.abs(longitude) > 360):
            raise ValueError("Invalid ICON cell coordinates")
        longitude = (longitude + 180) % 360 - 180
        x, y = np.meshgrid((self.longitude + 180) % 360 - 180, self.latitude)
        source = geometry.SwathDefinition(lons=longitude, lats=latitude)
        target = geometry.GridDefinition(lons=x, lats=y)
        self.neighbours = kd_tree.get_neighbour_info(source, target, 35000, neighbours=4,
                                                   epsilon=0, reduce_data=False, nprocs=1, segments=8)
        self.validate()
        temporary = path.with_name(f"{path.stem}-{os.getpid()}.tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(stream, **dict(zip(("input", "output", "index", "distance"), self.neighbours)))
        temporary.replace(path)
        metadata_temp = meta.with_name(f"{meta.stem}-{os.getpid()}.tmp")
        metadata_temp.write_text(json.dumps({"signature": signature,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}), encoding="utf-8")
        metadata_temp.replace(meta)

    def validate(self):
        source, target, index, distance = self.neighbours
        if (source.shape != (self.count,) or source.dtype.kind != "b" or target.dtype.kind != "b"
                or target.shape != (np.prod(self.shape),) or index.dtype.kind not in "iu"
                or index.shape != distance.shape or index.shape != (np.count_nonzero(target), 4)
                or np.any(index < 0) or np.any(index > np.count_nonzero(source))
                or np.any(np.isnan(distance)) or np.any(distance < 0)
                or not np.all(target) or np.any(~np.isfinite(distance[:, 0]))):
            raise ValueError("Incomplete or invalid ICON remapping weights")

    def __call__(self, values):
        from pyresample import kd_tree
        if values.shape != (self.count,):
            raise ValueError("ICON field and remapping grid sizes differ")
        result = kd_tree.get_sample_from_neighbour_info(
            "custom", self.shape, values, *self.neighbours[:3], distance_array=self.neighbours[3],
            weight_funcs=inverse_distance, fill_value=np.nan)
        if not np.isfinite(result).all():
            raise ValueError("ICON remapping left a gap in the chart source domain")
        return np.asarray(result, dtype=np.float32)


class IconProvider:
    def __init__(self, client, grid_root=None):
        self.client = client
        self.grid_root = Path(grid_root or os.environ.get("PUBLIC_CHARTS_ICON_GRID_DIR", client.root.parent / "icon-grid"))
        self.field_cache_root = os.environ.get("PUBLIC_CHARTS_FIELD_CACHE_DIR")

    def field(self, cycle, hour, parameter, level=None):
        from . import field_cache
        signature = {"version":1,"grid_version":GRID_VERSION,"cycle":cycle,"hour":hour,
                     "parameter":parameter,"level":level,"bounds":list(BOUNDS),"spacing":.125}
        if self.field_cache_root:
            cached = field_cache.read(self.field_cache_root,signature)
            if cached is not None:
                data,lat,lon,meta,sources = cached
                self.client.records.extend(sources)
                self.client.field_cache_hits = getattr(self.client,"field_cache_hits",0)+1
                return Decoded(data,lat,lon,meta)
        record_start = len(self.client.records)
        constant = parameter == "HSURF"
        values, meta = native(self.client, cycle, hour, parameter, level, constant=constant)
        identity = (str(self.grid_root.resolve()), meta["uuidOfHGrid"], len(values))
        with _GRID_LOCK:
            if identity not in _REMAPPERS:
                _REMAPPERS[identity] = IconRemapper(self.client, cycle, meta["uuidOfHGrid"], len(values), self.grid_root)
            remap = _REMAPPERS[identity]
        data = remap(values)
        unit = meta["units"]
        if parameter == "FI":
            if unit in {"m", "gpm"}:
                data *= GRAVITY
            elif unit not in {"m**2 s**-2", "m2 s-2"}:
                raise ValueError("Unknown ICON geopotential unit")
        elif parameter in {"PMSL", "PS"} and unit != "Pa":
            raise ValueError("Expected ICON pressure in Pa")
        elif parameter in {"T", "T_2M", "TD_2M"} and unit != "K":
            raise ValueError("Expected ICON temperature in K")
        elif parameter in {"U", "V", "U_10M", "V_10M"} and unit not in {"m s**-1", "m s-1"}:
            raise ValueError("Expected ICON wind in m/s")
        elif parameter in {"RELHUM", "RELHUM_2M"} and unit != "%":
            raise ValueError("Expected ICON relative humidity in percent")
        elif parameter == "HSURF" and unit != "m":
            raise ValueError("Expected ICON terrain height in m")
        elif parameter in {"TOT_PREC", "SNOW_GSP", "SNOW_CON"}:
            if unit not in {"kg m**-2", "kg m-2", "mm"} or meta["stepType"] != "accum":
                raise ValueError("Expected ICON accumulated precipitation mass")
        result = Decoded(data, remap.latitude, remap.longitude,
                       {**meta, "native_grid_type": meta["gridType"], "native_grid_uuid": meta["uuidOfHGrid"],
                        "gridType": "regular_ll", "grid_degrees": .125, "packing_error": float(meta["packingError"] or 0),
                        "remapping": "pyresample four-neighbour inverse-distance; not area-conservative"})
        if self.field_cache_root:
            field_cache.save(self.field_cache_root,signature,result,self.client.records[record_start:])
        return result

    def fetch(self, cycle, hour, wanted):
        fields = {}
        def get(parameter, level=None):
            token = parameter, level
            if token not in fields:
                fields[token] = self.field(cycle, hour, parameter, level)
            return fields[token]
        result = {}
        for key in sorted(wanted):
            if key in SURFACE:
                result[key] = get(SURFACE[key])
            elif key == "snowfall_water_equivalent":
                a, b = get("SNOW_GSP"), get("SNOW_CON")
                if any(a.metadata[name] != b.metadata[name] for name in ("startStep", "endStep", "native_grid_uuid")):
                    raise ValueError("ICON large-scale and convective snowfall windows differ")
                result[key] = Decoded(a.values + b.values, a.latitude, a.longitude,
                                     {**a.metadata, "components": [a.metadata, b.metadata],
                                      "packing_error": a.metadata["packing_error"] + b.metadata["packing_error"]})
            else:
                family, _, raw_level = key.rpartition("_")
                if not raw_level.isdigit():
                    raise ValueError(f"Unsupported ICON field: {key}")
                level = int(raw_level)
                if family == "specific_humidity":
                    temperature, humidity = get("T", level), get("RELHUM", level)
                    mixing = mpcalc.mixing_ratio_from_relative_humidity(
                        level * units.hPa, temperature.values * units.K,
                        np.clip(humidity.values, 0, 150) * units.percent, phase="liquid").to("kg/kg").m
                    q = np.asarray(mixing / (1 + mixing), dtype=np.float32)
                    if not np.isfinite(q).all() or np.any((q < 0) | (q >= 1)):
                        raise ValueError("Invalid ICON humidity conversion")
                    result[key] = Decoded(q, temperature.latitude, temperature.longitude,
                                         {**humidity.metadata, "units": "kg/kg",
                                          "derived_from": "native pressure-level RELHUM and T; water reference"})
                elif family in PRESSURE:
                    result[key] = get(PRESSURE[family], level)
                else:
                    raise ValueError(f"Unsupported ICON field: {key}")
        return result
