"""Cached additional fields and interval-by-interval experimental winter diagnostics."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import numpy as np
import metpy.calc as mpcalc
from metpy.units import units

from .sources import Decoded,HttpCache,Provider,NotReady,MODELS,atomic_json,collect_rainfall,rainfall_interval
from .diagnostics import interpolate,snow_ratio,wet_bulb_psychrometric
from .chart_style import precipitation_threshold,SNOW_THRESHOLD
from . import policy


class DiagnosticDeferred(NotReady):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ExtraFields:
    def __init__(self,root,model,cycle,*,deadline=None,progress=None):
        self.root=Path(root)/"extended-fields"/cycle/model
        self.model,self.cycle=model,cycle
        self.client=HttpCache(Path(root)/"grib-cache",deadline=deadline)
        self.provider=Provider(model,self.client)
        self.notes=[]
        self.checkpoints=Path(root)/"checkpoints"/cycle/model
        self.deadline=deadline
        self.progress=progress

    def check_budget(self,reason="cumulative_work_budget"):
        from .sources import check_cancelled
        check_cancelled()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise DiagnosticDeferred(reason)

    @contextmanager
    def checkpoint_lock(self, *, commit=False):
        """Only cumulative-prefix calculation is serialized, not map rendering."""
        self.checkpoints.mkdir(parents=True,exist_ok=True)
        with (self.checkpoints/".writer.lock").open("a+b") as handle:
            commit_deadline=time.monotonic()+5
            def check_wait():
                if commit:
                    if time.monotonic() >= commit_deadline:
                        raise DiagnosticDeferred("checkpoint_commit_budget")
                else:
                    self.check_budget("checkpoint_wait_budget")
            if os.name=="nt":
                import msvcrt
                while True:
                    check_wait()
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                        break
                    except OSError as error:
                        if error.errno not in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):
                            raise
                        time.sleep(.05)
                # Initialize only after locking; simultaneous first writers otherwise race.
                handle.seek(0,os.SEEK_END)
                if handle.tell()==0:
                    handle.write(b"0");handle.flush()
            else:
                import fcntl
                while True:
                    check_wait()
                    try:
                        fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(.05)
            try:
                yield
            finally:
                if os.name=="nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(handle,fcntl.LOCK_UN)

    def checkpoint(self,name):
        path=self.checkpoints/name
        metadata=path.with_suffix(".json")
        if not path.exists() or not metadata.exists():
            return None
        try:
            info=json.loads(metadata.read_text(encoding="utf-8"))
            if (info.get("model"),info.get("cycle"),info.get("version"))!=(self.model,self.cycle,1):
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest()!=info.get("sha256"):
                return None
            with np.load(path,allow_pickle=False) as data:
                return {key:data[key] for key in data.files}
        except (OSError,ValueError,KeyError):
            return None

    def save_checkpoint(self,name,arrays):
        path=self.checkpoints/name
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+f".{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(stream,**arrays)
        temporary.replace(path)
        atomic_json(path.with_suffix(".json"),{"model":self.model,"cycle":self.cycle,"version":1,
                    "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})

    def get(self,hour,key,*,sflux=False):
        self.check_budget()
        directory=self.root/f"{hour:03}"
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(("sflux_" if sflux else "")+key+".npz")
        meta_path=path.with_suffix(".json")
        if path.exists() and meta_path.exists():
            meta=json.loads(meta_path.read_text(encoding="utf-8"))
            if (meta.get("model"),meta.get("cycle"),meta.get("field",{}).get("endStep"))==(self.model,self.cycle,hour) and meta.get("sha256")==hashlib.sha256(path.read_bytes()).hexdigest():
                with np.load(path,allow_pickle=False) as data:
                    return Decoded(data["values"],data["latitude"],data["longitude"],meta["field"])
        field=(self.provider.sflux_surface(self.cycle,hour,{key}) if sflux else self.provider.fetch(self.cycle,hour,{key}))[key]
        self.save(path,field)
        return field

    def prefetch(self,hour,keys,*,sflux=False):
        """Share one source inventory lookup for a group of same-hour fields."""
        self.check_budget()
        missing=set()
        for key in keys:
            path=self.root/f"{hour:03}"/(("sflux_" if sflux else "")+key+".npz")
            try:
                meta=json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                valid=((meta.get("model"),meta.get("cycle"),meta.get("field",{}).get("endStep"))
                       ==(self.model,self.cycle,hour) and meta.get("sha256")==hashlib.sha256(path.read_bytes()).hexdigest())
            except (OSError,ValueError):
                valid=False
            if not valid:
                missing.add(key)
        if not missing:
            return
        fields=(self.provider.sflux_surface(self.cycle,hour,missing) if sflux
                else self.provider.fetch(self.cycle,hour,missing))
        for key in missing:
            self.save(self.root/f"{hour:03}"/(("sflux_" if sflux else "")+key+".npz"),fields[key])

    def save(self,path,field):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez_compressed(stream,values=field.values,latitude=field.latitude,longitude=field.longitude)
        temporary.replace(path)
        atomic_json(path.with_suffix(".json"),{"model":self.model,"cycle":self.cycle,"field":field.metadata,
                                               "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})

    def total_rain(self,hour):
        if hour<=0:
            raise ValueError("No total precipitation interval exists at +000h")
        cadence=MODELS[self.model]["rain"]
        legacy=self.root/f"{hour:03}"/"total_precipitation.npz"
        if self.checkpoint(f"rain/{hour:03}.npz") is None and legacy.exists() and legacy.with_suffix(".json").exists():
            info=json.loads(legacy.with_suffix(".json").read_text(encoding="utf-8"))
            if (info.get("model"),info.get("cycle"))==(self.model,self.cycle) and info.get("sha256")==hashlib.sha256(legacy.read_bytes()).hexdigest():
                with np.load(legacy,allow_pickle=False) as data:
                    field=regional(Decoded(data["values"],data["latitude"],data["longitude"],{}))
                self.save_checkpoint(f"rain/{hour:03}.npz",{"values":field.values,"latitude":field.latitude,"longitude":field.longitude})
        start=0
        total=None
        for end in range(hour,0,-cadence):
            self.check_budget()
            total=self.checkpoint(f"rain/{end:03}.npz")
            if total is not None:
                start=end
                break
        for end in range(start+cadence,hour+1,cadence):
            self.check_budget()
            rain,reference,_=collect_rainfall(self.provider,self.cycle,end,cadence)
            field=regional(Decoded(rain,reference.latitude,reference.longitude,{}))
            if total is not None:
                if not np.array_equal(total["latitude"],field.latitude) or not np.array_equal(total["longitude"],field.longitude):
                    raise ValueError("Cumulative precipitation grids differ")
                rain=total["values"]+field.values
            else:
                rain=field.values.astype(np.float64)
            total={"values":rain,"latitude":field.latitude,"longitude":field.longitude}
            with self.checkpoint_lock(commit=True):
                self.save_checkpoint(f"rain/{end:03}.npz",total)
            if self.progress:
                self.progress("diagnostic_step_ready",{"kind":"rain","step_hour":end})
        return Decoded(total["values"].astype(np.float32),total["latitude"],total["longitude"],
                       {"startStep":0,"endStep":hour,"units":"mm"})


def regional(field):
    rows=np.flatnonzero((field.latitude>=29)&(field.latitude<=45))
    cols=np.flatnonzero((field.longitude>=115)&(field.longitude<=138))
    return Decoded(field.values[np.ix_(rows,cols)],field.latitude[rows],field.longitude[cols],field.metadata)


def surface_moisture_fields(extra,base,hour):
    """Own-model near-surface humidity; retain GFS's native Gaussian sampling."""
    if extra.model=="aigfs":
        raise NotReady("AIGFS publishes no near-surface humidity; no 1000hPa proxy is used")
    sflux=extra.model=="gfs"
    temperature=extra.get(hour,"temperature_2m",sflux=sflux)
    lat,lon=temperature.latitude,temperature.longitude
    def values(key):
        field=extra.get(hour,key,sflux=sflux)
        if not np.array_equal(field.latitude,lat) or not np.array_equal(field.longitude,lon):
            raise ValueError(f"Near-surface {key} differs from the native temperature grid")
        return field.values
    if sflux:
        q=values("specific_humidity_2m")
        pressure=values("surface_pressure")
        dew=mpcalc.dewpoint_from_specific_humidity(pressure*units.Pa,np.maximum(q,1e-9)*units("kg/kg")).to("K").m
    elif extra.model in {"aifs","ecmwf"}:
        dew=values("dewpoint_2m")
    else:
        rh=values("relative_humidity_2m")
        dew=mpcalc.dewpoint_from_relative_humidity(temperature.values*units.K,np.clip(rh,.01,100)/100).to("K").m
    result={"latitude":lat,"longitude":lon,"temperature_2m":temperature.values,
            "dewpoint_2m":np.minimum(dew,temperature.values).astype(np.float32),
            "wind_u_10m":values("wind_u_10m"),"wind_v_10m":values("wind_v_10m")}
    # Only buffered outer source rows may fall beyond the coarser model grid.
    rows=np.flatnonzero((lat>=base["latitude"].min())&(lat<=base["latitude"].max()))
    cols=np.flatnonzero((lon>=base["longitude"].min())&(lon<=base["longitude"].max()))
    result={key:value[rows] if key=="latitude" else value[cols] if key=="longitude"
            else value[np.ix_(rows,cols)] for key,value in result.items()}
    for key in ("pressure_msl","terrain_height"):
        result[key]=interpolate(Decoded(base[key],base["latitude"],base["longitude"],{}),result["latitude"],result["longitude"])
    return result


def current_snowfall(extra,base,hour,interval):
    """Own-model snowfall only; GFS is an explicit endpoint-phase diagnostic."""
    if extra.model=="aigfs":
        raise NotReady("AIGFS publishes no native snowfall or categorical snow")
    if extra.model=="gfs":
        if policy.hourly_gfs_snow(extra.cycle):
            return gfs_sampled_snow(extra,hour,interval,base["latitude"],base["longitude"])
        flag=interpolate(extra.get(hour,"categorical_snow"),base["latitude"],base["longitude"],nearest=True)>=.5
        rain_lat=base.get("precipitation_latitude",base["latitude"])
        rain_lon=base.get("precipitation_longitude",base["longitude"])
        # Native Gaussian and regular grids differ at the unused outer source-buffer edges.
        rain=interpolate(Decoded(base["precipitation"],rain_lat,rain_lon,{}),
                         np.clip(base["latitude"],rain_lat.min(),rain_lat.max()),
                         np.clip(base["longitude"],rain_lon.min(),rain_lon.max()))
        return Decoded(rain*flag,base["latitude"],base["longitude"],{"source":"endpoint CSNOW diagnostic","units":"kg/m2"})
    current=extra.get(hour,"snowfall_water_equivalent")
    previous=None if int(current.metadata["startStep"])==hour-interval else extra.get(hour-interval,"snowfall_water_equivalent")
    values=rainfall_interval(current,previous,hour,interval)
    return Decoded(values,current.latitude,current.longitude,{"source":"native snowfall water equivalent","units":"kg/m2"})


def gfs_sampled_snow(extra,hour,interval,latitude,longitude):
    """Hourly endpoint phase through f120, native 3h endpoints thereafter."""
    start=hour-interval
    if start<0 or (hour>120 and hour%3) or (start>120 and start%3):
        raise ValueError("GFS snow interval is outside the native phase cadence")
    total=np.zeros((len(latitude),len(longitude)),dtype=np.float64)
    korea=(np.min(latitude)>=29 and np.max(latitude)<=45 and np.min(longitude)>=115 and np.max(longitude)<=138)
    region="hkor" if korea else "asia"
    steps=[]
    end=start
    while end<hour:
        extra.check_budget()
        duration=1 if end<120 else 3
        end+=duration
        if end>hour:
            raise ValueError("GFS phase interval cannot exactly cover the requested window")
        name=f"gfs-hourly-v1/phase-{region}/{end:03}.npz"
        cached=extra.checkpoint(name)
        if cached is None:
            rain,reference,_=collect_rainfall(extra.provider,extra.cycle,end,duration)
            # A phase checkpoint has one deterministic regional Gaussian grid.
            native_rain=Decoded(rain,reference.latitude,reference.longitude,{})
            regional_rain=regional(native_rain) if korea else native_rain
            flag=interpolate(extra.get(end,"categorical_snow"),regional_rain.latitude,regional_rain.longitude,nearest=True)>=.5
            cached={"latitude":regional_rain.latitude,"longitude":regional_rain.longitude,
                    "swe":regional_rain.values.astype(np.float64)*flag,
                    "duration":np.array(duration)}
            with extra.checkpoint_lock(commit=True):
                extra.save_checkpoint(name,cached)
        if int(cached["duration"])!=duration:
            raise ValueError("Cached GFS phase cadence differs")
        field=Decoded(cached["swe"],cached["latitude"],cached["longitude"],{})
        total+=interpolate(field,np.clip(latitude,field.latitude.min(),field.latitude.max()),
                           np.clip(longitude,field.longitude.min(),field.longitude.max()))
        steps.append(duration)
    return Decoded(total.astype(np.float32),latitude,longitude,
                   {"source":"GFS interval rain x native endpoint CSNOW; hourly through f120, 3h thereafter",
                    "startStep":start,"endStep":hour,"phase_intervals_hours":sorted(set(steps)),"units":"kg/m2"})


def snow_checkpoint_name(extra,name):
    return "gfs-hourly-v1/"+name if extra.model=="gfs" and policy.hourly_gfs_snow(extra.cycle) else name


def near_surface_fields(extra,end):
    keys=("temperature_2m","specific_humidity_2m","surface_pressure") if extra.model=="gfs" else (
        "temperature_2m","dewpoint_2m" if extra.model in {"aifs","ecmwf"} else "relative_humidity_2m","surface_pressure")
    extra.prefetch(end,keys,sflux=extra.model=="gfs")
    surface=regional(extra.get(end,"temperature_2m",sflux=extra.model=="gfs"))
    lat,lon=surface.latitude,surface.longitude
    t=surface.values
    if extra.model=="gfs":
        q=regional(extra.get(end,"specific_humidity_2m",sflux=True)).values
        pressure=regional(extra.get(end,"surface_pressure",sflux=True)).values
        rh=np.clip(mpcalc.relative_humidity_from_specific_humidity(pressure*units.Pa,t*units.K,q*units("kg/kg")).to("percent").m,0,100)
    elif extra.model in {"aifs","ecmwf"}:
        dew=regional(extra.get(end,"dewpoint_2m")).values
        rh=np.clip(mpcalc.relative_humidity_from_dewpoint(t*units.K,dew*units.K).to("percent").m,0,100)
        pressure=regional(extra.get(end,"surface_pressure")).values
    else:
        rh=regional(extra.get(end,"relative_humidity_2m")).values
        pressure=regional(extra.get(end,"surface_pressure")).values
    return {"latitude":lat,"longitude":lon,"temperature_2m":t,
            "relative_humidity_2m":rh,"surface_pressure":pressure}


def snow_step(extra,end,cadence):
    extra.prefetch(end,("temperature_850","categorical_snow") if extra.model=="gfs" else
                   ("temperature_850","snowfall_water_equivalent"))
    surface=near_surface_fields(extra,end)
    lat,lon=surface["latitude"],surface["longitude"]
    t,rh,pressure=surface["temperature_2m"],surface["relative_humidity_2m"],surface["surface_pressure"]
    rain_values,rain_reference,_=collect_rainfall(extra.provider,extra.cycle,end,cadence)
    rain=interpolate(Decoded(rain_values,rain_reference.latitude,rain_reference.longitude,{}),lat,lon)
    t850=interpolate(extra.get(end,"temperature_850"),lat,lon)-273.15
    ratio,depth,_=snow_ratio(rain,t-273.15,rh,t850,interval_hours=cadence)
    if extra.model=="gfs":
        if policy.hourly_gfs_snow(extra.cycle):
            snow=gfs_sampled_snow(extra,end,cadence,lat,lon).values
            snow_flag=snow>.01
        else:
            snow_flag=interpolate(extra.get(end,"categorical_snow"),lat,lon,nearest=True)>=.5
            snow=rain*snow_flag
    else:
        current=extra.get(end,"snowfall_water_equivalent")
        previous=None if int(current.metadata["startStep"])==end-cadence else extra.get(end-cadence,"snowfall_water_equivalent")
        amount=rainfall_interval(current,previous,end,cadence)
        snow=interpolate(Decoded(amount,current.latitude,current.longitude,{}),lat,lon)
        snow_flag=snow>.01
    return {"latitude":lat,"longitude":lon,"temperature_2m":t,"relative_humidity_2m":rh,
            "surface_pressure":pressure,"snow_ratio":ratio,"snow_expected":snow_flag.astype(float),
            "snowfall_water_equivalent":snow,"snowfall_depth_diagnostic":depth}


def cached_snow_step(extra,end,cadence):
    name=snow_checkpoint_name(extra,f"steps/{end:03}.npz")
    current=extra.checkpoint(name)
    if current is None:
        current=snow_step(extra,end,cadence)
        extra.save_checkpoint(name,current)
        if extra.progress:
            extra.progress("diagnostic_step_ready",{"kind":"snow","step_hour":end})
    return current


def winter_fields(extra,base,hour,interval,*,cumulative=True):
    if extra.model=="aigfs":
        raise NotReady("AIGFS has no near-surface humidity or native snowfall in its open product")
    version="v3-hourly" if extra.model=="gfs" and policy.hourly_gfs_snow(extra.cycle) else "v2"
    target=extra.root/f"{hour:03}"/(f"winter-{version}.npz" if cumulative else f"winter-window-{version}.npz")
    metadata_path=target.with_suffix(".json")
    if target.exists() and metadata_path.exists():
        metadata=json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("interval")==interval and hashlib.sha256(target.read_bytes()).hexdigest()==metadata.get("sha256"):
            with np.load(target,allow_pickle=False) as archive:
                cached={key:archive[key] for key in archive.files}
            cached["snow_expected"]=(cached["snowfall_water_equivalent"]>SNOW_THRESHOLD).astype(float)
            return cached,metadata
    cadence=MODELS[extra.model]["rain"]
    total_snow=total_depth=window_snow=window_depth=None
    start=0 if cumulative else max(0,hour-interval)
    for previous_end in (range(hour-interval,0,-cadence) if cumulative else ()):
        extra.check_budget()
        previous=extra.checkpoint(snow_checkpoint_name(extra,f"snow/{previous_end:03}.npz"))
        if previous is not None:
            total_snow=previous["total_snowfall_water_equivalent"].copy()
            total_depth=previous["total_snowfall_depth_diagnostic"].copy()
            start=previous_end
            break
    last=None
    if hour==0:
        last=near_surface_fields(extra,0)
        empty=np.zeros_like(last["temperature_2m"],dtype=np.float64)
        last.update({key:empty.copy() for key in ("snow_ratio","snow_expected",
                    "snowfall_water_equivalent","snowfall_depth_diagnostic",
                    "total_snowfall_water_equivalent","total_snowfall_depth_diagnostic")})
    for end in range(start+cadence,hour+1,cadence):
        extra.check_budget()
        current=extra.checkpoint(snow_checkpoint_name(extra,f"snow/{end:03}.npz")) if cumulative else None
        if current is None:
            current=dict(cached_snow_step(extra,end,cadence))
            snow=current["snowfall_water_equivalent"]
            depth=current["snowfall_depth_diagnostic"]
            if total_snow is None:
                total_snow=np.zeros_like(snow,dtype=np.float64)
                total_depth=np.zeros_like(depth,dtype=np.float64)
            total_snow=total_snow+snow
            total_depth=total_depth+depth
            current.update(total_snowfall_water_equivalent=total_snow,
                           total_snowfall_depth_diagnostic=total_depth)
            if cumulative:
                with extra.checkpoint_lock(commit=True):
                    extra.save_checkpoint(snow_checkpoint_name(extra,f"snow/{end:03}.npz"),current)
        total_snow=current["total_snowfall_water_equivalent"]
        total_depth=current["total_snowfall_depth_diagnostic"]
        snow=current["snowfall_water_equivalent"]
        depth=current["snowfall_depth_diagnostic"]
        if window_snow is None:
            window_snow=np.zeros_like(snow)
            window_depth=np.zeros_like(depth)
        if end>hour-interval:
            window_snow+=snow
            window_depth+=depth
        last=dict(current)
        last.update(snowfall_water_equivalent=window_snow,snowfall_depth_diagnostic=window_depth)
    if last is None:
        raise ValueError("Winter diagnostic requires a positive lead on its native cadence")
    last["snow_expected"]=(last["snowfall_water_equivalent"]>SNOW_THRESHOLD).astype(float)
    lat,lon=last["latitude"],last["longitude"]
    for key in ("pressure_msl","terrain_height","geopotential_925","geopotential_850","temperature_925",
                "temperature_850","wind_u_925","wind_v_925"):
        last[key]=interpolate(Decoded(base[key],base["latitude"],base["longitude"],{}),lat,lon)
    last["geopotential_1000"]=interpolate(extra.get(hour,"geopotential_1000"),lat,lon)
    rain=base["precipitation"]
    rain_lat=base.get("precipitation_latitude",base["latitude"])
    rain_lon=base.get("precipitation_longitude",base["longitude"])
    last["precipitation"]=interpolate(Decoded(rain,rain_lat,rain_lon,{}),lat,lon)
    dew=mpcalc.dewpoint_from_relative_humidity(last["temperature_2m"]*units.K,np.clip(last["relative_humidity_2m"],.01,100)/100).to("K").m
    last["wet_bulb_2m"]=wet_bulb_psychrometric(last["temperature_2m"],dew,last["surface_pressure"])
    metadata={"model":extra.model,"cycle":extra.cycle,"hour":hour,"interval":interval,"native_interval":cadence,
              "snowfall_source":("zero-length display window; no accumulation queried" if hour==0 else
                                 ("native endpoint GFS CSNOW: hourly through f120, 3h thereafter; not native snowfall mass"
                                  if policy.hourly_gfs_snow(extra.cycle) else "endpoint GFS CSNOW categorical diagnostic, not native snowfall mass") if extra.model=="gfs" else
                                 "native accumulated snowfall water equivalent"),
              "ratio_convention":"KMA 2024 manual p88; natural logarithm; rain normalized to 3h-equivalent intensity; cm=mm*ratio/10",
              "depth_caveat":"fresh-snow diagnostic; no melting, compaction, blowing snow or existing snowpack",
              "wet_bulb_method":"MetPy WMO psychrometric vapor pressure inversion",
              "cutoff_convention":"display only; light precipitation remains in interval/cumulative physical totals"}
    temporary=target.with_suffix(".tmp")
    target.parent.mkdir(parents=True,exist_ok=True)
    with temporary.open("wb") as stream:
        np.savez_compressed(stream,**last)
    temporary.replace(target)
    metadata["sha256"]=hashlib.sha256(target.read_bytes()).hexdigest()
    atomic_json(metadata_path,metadata)
    return last,metadata
