"""Regional ASDI input and incremental, mass-bounded UKMO winter diagnostics."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

import numpy as np
import requests

from . import policy
from .dates import parse_cycle
from .diagnostics import snow_ratio,wet_bulb_psychrometric
from .extended_sources import ExtraFields,DiagnosticDeferred
from .sources import NotReady,SourceDeferred,atomic_json

BASE="https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com"
BOUNDS=(29,45,115,138)
PROFILE_LEVELS=(1000,975,950,925,900,850,800,750,700)
DIAGNOSTIC_VERSION="ukmo-winter-v1"


def source_url(cycle,hour,token):
    run=parse_cycle(cycle)
    maximum=69 if run.hour in (6,18) else 168
    if hour<0 or hour>maximum or hour>54 and hour%native_interval(hour):
        raise ValueError("UKMO forecast hour is outside the supported native cadence")
    valid=run+timedelta(hours=hour)
    return f"{BASE}/global-deterministic-10km/{run:%Y%m%dT%H%MZ}/{valid:%Y%m%dT%H%MZ}-PT{hour:04}H00M-{token}.nc"


def native_interval(hour):
    return 1 if hour<=54 else 3 if hour<=144 else 6


def diagnostic_ends(start,end):
    return [hour for hour in (*range(3,145,3),*range(150,169,6)) if start<hour<=end]


class NativeReader:
    def __init__(self,root,cycle,*,deadline=None,fallback=None,bounds=BOUNDS):
        self.root,self.cycle=Path(root),cycle
        self.deadline=deadline
        self.fallback=Path(fallback) if fallback is not None else None
        self.bounds=tuple(bounds)
        self.records=[]
        self.requests=self.bytes=0
        self.grid=None

    def check(self):
        from .sources import check_cancelled
        check_cancelled()
        if self.deadline is not None and time.monotonic()>=self.deadline:
            raise DiagnosticDeferred("ukmo_input_budget")

    def path(self,hour,token,levels=()):
        suffix="_"+"-".join(map(str,levels)) if levels else ""
        directory=self.root if self.bounds==BOUNDS else self.root/"asia"
        return directory/f"{self.cycle}_{hour:03}_{token}{suffix}.npz"

    def cached(self,path,hour,token,levels):
        try:
            meta=json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            if (meta.get("cycle"),meta.get("hour"),meta.get("token"),meta.get("levels"))!=(self.cycle,hour,token,list(levels)):
                return None
            if tuple(meta.get("bounds",BOUNDS))!=self.bounds:
                return None
            if meta.get("sha256")!=hashlib.sha256(path.read_bytes()).hexdigest():
                return None
            with np.load(path,allow_pickle=False) as data:
                arrays={key:data[key] for key in data.files}
            self.validate_grid(arrays)
            return arrays,meta
        except (OSError,ValueError,KeyError):
            return None

    @staticmethod
    def validate_grid(arrays):
        lat,lon=arrays["latitude"],arrays["longitude"]
        if len(lat)<2 or len(lon)<2 or not (np.diff(lat)>0).all() or not (np.diff(lon)>0).all():
            raise ValueError("Invalid UKMO native coordinates")
        for key,values in arrays.items():
            if not np.isfinite(values).all() or key not in {"latitude","longitude"} and values.shape!=(len(lat),len(lon)):
                raise ValueError("Invalid UKMO regional field")

    def field(self,hour,token,levels=(),*,unit=None,duration=None):
        self.check()
        path=self.path(hour,token,levels)
        result=self.cached(path,hour,token,levels)
        if result is None and self.fallback is not None:
            result=self.cached(self.fallback/path.relative_to(self.root),hour,token,levels)
        if result is None:
            result=self.download(hour,token,levels)
            arrays,meta=result
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name(path.name+f".{uuid.uuid4().hex}.tmp")
            with temporary.open("wb") as stream:
                np.savez_compressed(stream,**arrays)
            temporary.replace(path)
            meta={**meta,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
            atomic_json(path.with_suffix(".json"),meta)
        arrays,meta=result
        if self.grid is not None and (not np.array_equal(arrays["latitude"],self.grid[0]) or not np.array_equal(arrays["longitude"],self.grid[1])):
            raise ValueError("UKMO fields do not share the same native grid")
        self.grid=arrays["latitude"],arrays["longitude"]
        if unit is not None and meta["attrs"]["units"]!=unit:
            raise ValueError(f"Unexpected UKMO units for {token}")
        if duration is not None and meta.get("bounds_seconds")!=[(hour-duration)*3600,hour*3600]:
            raise ValueError("UKMO native amount/mean interval differs from requested interval")
        return arrays,meta

    def value(self,hour,token,unit,*,duration=None):
        return self.field(hour,token,unit=unit,duration=duration)[0]["value"]

    def download(self,hour,token,levels):
        import h5py
        from fsspec.implementations.http_sync import HTTPFileSystem
        reader=self
        before_bytes=self.bytes
        transport={}
        class Session(requests.Session):
            def request(self,*args,**kwargs):
                reader.check()
                remaining=max(.1,reader.deadline-time.monotonic()) if reader.deadline is not None else 15
                kwargs["timeout"]=(min(3,remaining),min(15,remaining))
                response=super().request(*args,**kwargs)
                reader.requests+=1
                if response.status_code in (404,410):
                    response.close()
                    raise NotReady("UKMO field has not arrived")
                if response.status_code in (429,500,502,503,504):
                    response.close()
                    raise SourceDeferred("ukmo_source_transient")
                response.raise_for_status()
                reader.bytes+=len(response.content)
                if response.headers.get("Last-Modified"):
                    transport["last_modified"]=response.headers["Last-Modified"]
                return response
        url=source_url(self.cycle,hour,token)
        started=datetime.now(timezone.utc).isoformat()
        run=parse_cycle(self.cycle)
        with Session() as session:
            session.headers["User-Agent"]="CDS-UKMO-regional/1.0"
            block_size=262144 if self.bounds==BOUNDS else 1048576
            fs=HTTPFileSystem(block_size=block_size)
            fs.session,fs.js=session,False
            with fs.open(url,"rb",block_size=block_size) as stream,h5py.File(stream,"r") as data:
                if (int(data["forecast_period"][()])!=hour*3600 or int(data["forecast_reference_time"][()])!=int(run.timestamp())
                        or int(data["time"][()])!=int((run+timedelta(hours=hour)).timestamp())):
                    raise ValueError("UKMO initialization/valid-time mismatch")
                lat,lon=data["latitude"][()],data["longitude"][()]
                yi=np.flatnonzero((lat>=self.bounds[0])&(lat<=self.bounds[1]))
                xi=np.flatnonzero((lon>=self.bounds[2])&(lon<=self.bounds[3]))
                candidates=[(name,value) for name,value in data.items() if isinstance(value,h5py.Dataset)
                            and value.ndim in (2,3) and value.shape[-2:]==(len(lat),len(lon))
                            and name not in {"flag","status_flag"}]
                if len(candidates)!=1 or len(yi)<2 or len(xi)<2:
                    raise ValueError("Ambiguous or empty UKMO regional field")
                name,variable=candidates[0]
                if bool(levels)!=(variable.ndim==3):
                    raise ValueError("Unexpected UKMO pressure coordinate")
                arrays={"latitude":lat[yi],"longitude":lon[xi]}
                for level in levels or (None,):
                    select=(slice(yi[0],yi[-1]+1),slice(xi[0],xi[-1]+1))
                    if level is not None:
                        indices=np.flatnonzero(np.isclose(data["pressure"][()],level*100))
                        if len(indices)!=1:
                            raise ValueError("Missing UKMO pressure level")
                        select=(int(indices[0]),*select)
                    values=np.asarray(variable[select],dtype=np.float64)
                    if "_FillValue" in variable.attrs:
                        values[values==variable.attrs["_FillValue"]]=np.nan
                    suffix="" if level is None else f"_{level}"
                    arrays["value"+suffix]=values
                    flag="flag" if "flag" in data else "status_flag" if "status_flag" in data else None
                    if level is not None:
                        if flag is None:
                            raise ValueError("UKMO pressure field lacks its terrain status flag")
                        arrays["flag"+suffix]=np.asarray(data[flag][select],dtype=np.int8)
                if (np.diff(arrays["latitude"])<0).all():
                    arrays={key:value[::-1] if key=="latitude" else value if key=="longitude" else value[::-1,:]
                            for key,value in arrays.items()}
                self.validate_grid(arrays)
                def plain(value):
                    if isinstance(value,bytes): return value.decode()
                    if isinstance(value,np.ndarray): return value.tolist()
                    if isinstance(value,np.generic): return value.item()
                    return value
                meta={"url":url,"cycle":self.cycle,"hour":hour,"token":token,"levels":list(levels),"variable":name,
                      "bounds":list(self.bounds),
                      "attrs":{key:plain(value) for key,value in variable.attrs.items() if key not in {"DIMENSION_LIST","REFERENCE_LIST"}},
                      "bounds_seconds":plain(data["forecast_period_bnds"][()]) if "forecast_period_bnds" in data else None}
        self.records.append({"source_host":"met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com",
                             "kind":"ukmo_netcdf","request_started_at":started,
                             "observed_at":datetime.now(timezone.utc).isoformat(),"bytes":self.bytes-before_bytes,**transport})
        return arrays,meta


def estimate_surface_pressure(heights,terrain,levels=PROFILE_LEVELS):
    z=np.stack([heights[f"value_{p}"] for p in levels])
    if not (np.diff(z,axis=0)>0).all():
        raise ValueError("Nonmonotonic UKMO geopotential profile")
    lower=np.clip((z<=terrain).sum(axis=0)-1,0,len(levels)-2)
    z0=np.take_along_axis(z,lower[None],axis=0)[0]
    z1=np.take_along_axis(z[1:],lower[None],axis=0)[0]
    logp=np.log(np.asarray(levels)*100.)
    pressure=np.exp(logp[lower]+(terrain-z0)/(z1-z0)*(logp[lower+1]-logp[lower]))
    if not np.isfinite(pressure).all() or ((pressure<(50000 if levels==PROFILE_LEVELS else 25000))|(pressure>110000)).any():
        raise ValueError("Unphysical estimated UKMO surface pressure")
    return pressure


def collect_ukmo(cycle,hour,root,*,deadline=None,upper_level=None):
    root=Path(root)
    full=policy.ukmo_full_catalog(cycle,hour)
    upper_level=upper_level or (300 if "gph300" in policy.products(cycle) else 200)
    version=2 if full else 1
    directory=root/"fields"/cycle/"ukmo"/f"{hour:03}"
    path=directory/"fields.npz"
    try:
        meta=json.loads((directory/"metadata.json").read_text(encoding="utf-8"))
        if ((meta.get("model"),meta.get("cycle"),meta.get("hour"),meta.get("collector_version"))==("ukmo",cycle,hour,version)
                and (not full or meta.get("upper_level")==upper_level)):
            if meta.get("sha256")==hashlib.sha256(path.read_bytes()).hexdigest():
                with np.load(path,allow_pickle=False) as data:
                    return {key:data[key] for key in data.files},meta,{"requests":0,"bytes":0}
    except (OSError,ValueError,KeyError):
        pass
    from .sources import BOUNDS as ASIA_BOUNDS
    source=NativeReader(root/"ukmo-native",cycle,deadline=deadline,bounds=ASIA_BOUNDS if full else BOUNDS)
    height_levels=tuple(sorted({*PROFILE_LEVELS,500,upper_level},reverse=True)) if full else PROFILE_LEVELS
    temperature_levels=(925,850,700,500,upper_level) if full else (925,850)
    wind_levels=temperature_levels if full else (925,)
    heights,_=source.field(hour,"geopotential_height_on_pressure_levels",height_levels,unit="m")
    temperature,_=source.field(hour,"temperature_on_pressure_levels",temperature_levels,unit="K")
    speed,_=source.field(hour,"wind_speed_on_pressure_levels",wind_levels,unit="m s-1")
    direction,_=source.field(hour,"wind_direction_on_pressure_levels",wind_levels,unit="degrees")
    terrain=source.value(0,"height_of_orography","m")
    arrays={"latitude":heights["latitude"],"longitude":heights["longitude"],"terrain_height":terrain,
            "pressure_msl":source.value(hour,"pressure_at_mean_sea_level","Pa"),
            "temperature_2m":source.value(hour,"temperature_at_screen_level","K"),
            "relative_humidity_2m":source.value(hour,"relative_humidity_at_screen_level","1")*100,
            "surface_pressure":estimate_surface_pressure(heights,terrain,height_levels),
            **{f"temperature_{level}":temperature[f"value_{level}"] for level in temperature_levels},
            "valid_925":speed["flag_925"]==0,"valid_850":temperature["flag_850"]==0,
            **{f"wind_u_{level}":-speed[f"value_{level}"]*np.sin(np.deg2rad(direction[f"value_{level}"])) for level in wind_levels},
            **{f"wind_v_{level}":-speed[f"value_{level}"]*np.cos(np.deg2rad(direction[f"value_{level}"])) for level in wind_levels},
            **{f"geopotential_{level}":heights[f"value_{level}"]*9.80665 for level in height_levels}}
    if full:
        import metpy.calc as mpcalc
        from metpy.units import units
        humidity,_=source.field(hour,"relative_humidity_on_pressure_levels",(925,850,700),unit="1")
        for level in (925,850,700):
            dew=mpcalc.dewpoint_from_relative_humidity(arrays[f"temperature_{level}"]*units.K,
                    np.clip(humidity[f"value_{level}"],.0001,1)).to("K")
            arrays[f"specific_humidity_{level}"]=mpcalc.specific_humidity_from_dewpoint(level*units.hPa,dew).to("kg/kg").m
        for level in temperature_levels:
            arrays[f"valid_{level}"]=(heights[f"flag_{level}"]==0)&(temperature[f"flag_{level}"]==0)&(speed[f"flag_{level}"]==0)
    if not all(np.isfinite(value).all() for value in arrays.values()):
        raise ValueError("Nonfinite UKMO base inputs")
    directory.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(".tmp")
    with temporary.open("wb") as stream: np.savez_compressed(stream,**arrays)
    temporary.replace(path)
    meta={"model":"ukmo","cycle":cycle,"hour":hour,"collector_version":version,"upper_level":upper_level if full else None,
          "sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"sources":source.records}
    atomic_json(directory/"metadata.json",meta)
    return arrays,meta,{"requests":source.requests,"bytes":source.bytes}


def bounded_swe(snow,precipitation):
    if not np.isfinite(snow).all() or not np.isfinite(precipitation).all() or (snow<0).any() or (precipitation<0).any():
        raise ValueError("Invalid UKMO snow/precipitation amount")
    return np.minimum(snow,precipitation)


class RainDiagnostics(ExtraFields):
    def __init__(self,root,cycle,base,*,checkpoint_root=None,deadline=None,asia=False):
        super().__init__(root,"ukmo",cycle,deadline=deadline)
        if checkpoint_root is not None: self.checkpoints=Path(checkpoint_root)/cycle/"ukmo"
        from .sources import BOUNDS as ASIA_BOUNDS
        self.native=NativeReader(self.checkpoints/"native",cycle,deadline=deadline,fallback=Path(root)/"ukmo-native",
                                 bounds=ASIA_BOUNDS if asia else BOUNDS)
        self.native.grid=base["latitude"],base["longitude"]
        self.shape=base["pressure_msl"].shape
        self.prefix_name="rain-asia-v1" if asia else "rain-korea-v1"

    def step(self,end):
        name=f"{self.prefix_name}/steps/{end:03}.npz"
        cached=self.checkpoint(name)
        if cached is not None: return cached["rain"]
        rain=np.zeros(self.shape)
        for hour in (range(end-2,end+1) if end<=54 else (end,)):
            duration=native_interval(hour)
            rain+=self.native.value(hour,f"precipitation_accumulation-PT{duration:02}H","m",duration=duration)*1000
        if not np.isfinite(rain).all() or (rain<0).any(): raise ValueError("Invalid UKMO precipitation")
        with self.checkpoint_lock(commit=True): self.save_checkpoint(name,{"rain":rain})
        return rain

    def window(self,start,end):
        total=np.zeros(self.shape)
        for hour in diagnostic_ends(start,end): total+=self.step(hour)
        return total

    def total(self,end):
        total=np.zeros(self.shape);start=0
        for previous in reversed(diagnostic_ends(0,end)):
            saved=self.checkpoint(f"{self.prefix_name}/totals/{previous:03}.npz")
            if saved is not None: total=saved["rain"];start=previous;break
        for hour in diagnostic_ends(start,end):
            self.check_budget()
            total=total+self.step(hour)
            with self.checkpoint_lock(commit=True): self.save_checkpoint(f"{self.prefix_name}/totals/{hour:03}.npz",{"rain":total})
        return total


class WinterDiagnostics(ExtraFields):
    def __init__(self,root,cycle,base,hour,*,checkpoint_root=None,deadline=None,progress=None):
        super().__init__(root,"ukmo",cycle,deadline=deadline,progress=progress)
        if checkpoint_root is not None: self.checkpoints=Path(checkpoint_root)/cycle/"ukmo"
        self.native=NativeReader(self.checkpoints/"native",cycle,deadline=deadline,fallback=Path(root)/"ukmo-native")
        self.native.grid=base["latitude"],base["longitude"]
        self.base,self.hour=base,hour

    def step(self,end):
        name=f"{DIAGNOSTIC_VERSION}/steps/{end:03}.npz"
        cached=self.checkpoint(name)
        if cached is not None: return cached
        source=self.native
        empty=np.zeros_like(self.base["pressure_msl"],dtype=np.float64)
        rain,snow,raw=empty.copy(),empty.copy(),empty.copy()
        for hour in (range(end-2,end+1) if end<=54 else (end,)):
            duration=native_interval(hour)
            now=source.value(hour,"snowfall_rate","m s-1")
            before=source.value(hour-duration,"snowfall_rate","m s-1")
            conv=source.value(hour,f"snowfall_rate_from_convection_mean-PT{duration:02}H","m s-1",duration=duration)
            q=source.value(hour,f"precipitation_accumulation-PT{duration:02}H","m",duration=duration)*1000
            s=((before+now)/2+conv)*duration*3600000
            rain+=q
            snow+=bounded_swe(s,q)
            raw+=s
        t=source.value(end,"temperature_at_screen_level","K")
        rh=source.value(end,"relative_humidity_at_screen_level","1")*100
        t850=source.field(end,"temperature_on_pressure_levels",(850,),unit="K")[0]
        if ((rh<-.2)|(rh>150)).any(): raise ValueError("Unphysical UKMO screen RH")
        valid=(rh>=-.2)&(rh<=100.2)&(t850["flag_850"]==0)
        ratio,depth,_=snow_ratio(rain,t-273.15,np.clip(rh,0,100),t850["value_850"]-273.15,interval_hours=max(3,native_interval(end)))
        result={"rain":rain,"swe":snow,"raw_swe":raw,"depth":depth,"sr_valid":valid}
        with self.checkpoint_lock(commit=True): self.save_checkpoint(name,result)
        if self.progress:
            self.progress("ukmo_snow_qc",{"hour":end,"limited_cells":int((raw>snow+1e-6).sum()),
                                         "limited_swe_sum":float((raw-snow).sum()),"sr_invalid_cells":int((~valid).sum())})
        return result

    def prefix(self,end):
        prefix=f"{DIAGNOSTIC_VERSION}/totals"
        total=None;start=0
        for previous in reversed(diagnostic_ends(0,end)):
            total=self.checkpoint(f"{prefix}/{previous:03}.npz")
            if total is not None: start=previous;break
        if total is None:
            zero=np.zeros_like(self.base["pressure_msl"],dtype=np.float64)
            total={key:zero.copy() for key in ("rain","swe","depth")}
            total["sr_valid"]=np.ones_like(zero,dtype=bool)
        for hour in diagnostic_ends(start,end):
            self.check_budget()
            step=self.step(hour)
            total={key:total[key]+step[key] for key in ("rain","swe","depth")}|{"sr_valid":total["sr_valid"]&step["sr_valid"]}
            with self.checkpoint_lock(commit=True): self.save_checkpoint(f"{prefix}/{hour:03}.npz",total)
        return total

    def fields(self,interval,*,cumulative):
        import metpy.calc as mpcalc
        from metpy.units import units
        zero=np.zeros_like(self.base["pressure_msl"],dtype=np.float64)
        window={key:zero.copy() for key in ("rain","swe","depth")}
        window["sr_valid"]=np.ones_like(zero,dtype=bool)
        for hour in diagnostic_ends(self.hour-interval,self.hour):
            step=self.step(hour)
            for key in ("rain","swe","depth"): window[key]+=step[key]
            window["sr_valid"]&=step["sr_valid"]
        total=self.prefix(self.hour) if cumulative else window
        rh=self.base["relative_humidity_2m"]
        dew=mpcalc.dewpoint_from_relative_humidity(self.base["temperature_2m"]*units.K,np.clip(rh,.01,100)/100).to("K").m
        wet=wet_bulb_psychrometric(self.base["temperature_2m"],dew,self.base["surface_pressure"])
        arrays={**self.base,"precipitation":window["rain"],"snow_expected":(window["swe"]>.01).astype(float),
                "snowfall_water_equivalent":window["swe"],"snowfall_depth_diagnostic":window["depth"],
                "total_snowfall_water_equivalent":total["swe"],"total_snowfall_depth_diagnostic":total["depth"],
                "wet_bulb_2m":wet}
        masks={925:self.base["valid_925"],"snowfall_depth_diagnostic":window["sr_valid"],
               "total_snowfall_depth_diagnostic":total["sr_valid"],"wet_bulb_2m":(rh>=-.2)&(rh<=100.2)}
        return arrays,total["rain"],masks


def produce_ukmo(cycle,hour,root,existing,publish,*,force=False,checkpoint_root=None,progress=None):
    from .producer import compose,encode,crop
    from .plotter import render
    from .chart_style import PUBLIC_PROFILE
    from .sources import accumulation_hours
    started=time.monotonic()
    base,meta,_=collect_ukmo(cycle,hour,root,deadline=started+180)
    if progress: progress("base",{"observed_at":datetime.now(timezone.utc).isoformat(),"sources":meta["sources"],"seconds":time.monotonic()-started})
    interval=accumulation_hours("ukmo",hour) or 0
    completed=set(existing);timings={"plot_seconds":0.,"encode_seconds":0.}
    full=policy.ukmo_full_catalog(cycle,hour)
    def emit(product,domain,frame=None,alias_of=None):
        identity=policy.key("ukmo",cycle,hour,product,domain)
        if not force and identity in completed: return
        path=Path(root)/"images"/"ukmo"/f"{cycle}_{hour:03}_{product}_{domain}.webp"
        if frame is not None:
            began=time.monotonic();encode(frame,path)
            timings["encode_seconds"]+=time.monotonic()-began
        publish(product,domain,path if frame is not None else None,**({"alias_of":alias_of} if alias_of else {}))
        completed.add(identity)
        if path.exists(): path.unlink()
    def draw(arrays,product,domain,*,accumulation=None,masks=None):
        from .quality import closed_contour_predicate
        began=time.monotonic()
        if progress: progress("product_started",{"product":product,"domain":domain})
        frame=render(arrays,cycle,hour,product,domain,None,accumulation=accumulation,
                     model_name=policy.TITLES["ukmo"],precipitation_hours=interval,precipitation_field="precipitation",
                     source_note="Data sourced from UK Met Office",profile=PUBLIC_PROFILE,physical_barb_spacing=True,
                     model_id="ukmo",field_masks=masks,contour_filter=closed_contour_predicate(arrays),
                     diagnostic_snowfall=True,diagnostic_label="Diagnostic",return_frame=True)
        timings["plot_seconds"]+=time.monotonic()-began
        if progress: progress("product_finished",{"product":product,"domain":domain,"seconds":time.monotonic()-began})
        return frame
    try:
        if full:
            for domain in ("asia","hkor"):
                selected=crop(base,domain)
                masks={level:selected[f"valid_{level}"] for level in (200,300,500,700,850,925) if f"valid_{level}" in selected}
                for product in policy.products(cycle):
                    if force or policy.key("ukmo",cycle,hour,product,domain) not in completed:
                        emit(product,domain,draw(selected,product,domain,masks=masks))
            if force or policy.key("ukmo",cycle,hour,"surfce","asia") not in completed:
                rain=RainDiagnostics(root,cycle,base,checkpoint_root=checkpoint_root,deadline=time.monotonic()+180,asia=True)
                amount=rain.window(hour-interval,hour)
                if progress: progress("surface",{"observed_at":datetime.now(timezone.utc).isoformat(),"sources":rain.native.records})
                emit("surfce","asia",draw({**base,"precipitation":amount},"surfce","asia"))
            base=crop(base,"hkor")
        if not policy.winter("ukmo",cycle):
            rain=RainDiagnostics(root,cycle,base,checkpoint_root=checkpoint_root,deadline=time.monotonic()+300)
            arrays={**base,"precipitation":rain.window(hour-interval,hour)}
            emit("surfce","hkor",draw(arrays,"surfce","hkor"))
            for name in ("acptot","acptot1"): emit(name,"hkor",alias_of=("surfce","hkor"))
            if force or policy.key("ukmo",cycle,hour,"acrain","hkor") not in completed:
                total=rain.total(hour)
                emit("acrain","hkor",draw(arrays,"acrain","hkor",accumulation=total))
            emit("acrain1","hkor",alias_of=("acrain","hkor"))
            return {"complete":policy.expected_keys("ukmo",cycle,hour)<=completed,**timings}
        diag=WinterDiagnostics(root,cycle,base,hour,checkpoint_root=checkpoint_root,deadline=time.monotonic()+300,progress=progress)
        for name in ("acptot","acrain"):
            if not force and all(policy.key("ukmo",cycle,hour,name+s,"hkor") in completed for s in ("","1")): continue
            arrays,total,masks=diag.fields(interval,cumulative=name=="acrain")
            if progress: progress("window" if name=="acptot" else "total",{"observed_at":datetime.now(timezone.utc).isoformat(),"sources":diag.native.records})
            frames=[]
            for product in (name,name+"_snow",name+"_snowdepth"):
                frames.append(draw(arrays,product,"hkor",accumulation=total,masks=masks))
            for suffix,vertical in (("",False),("1",True)):
                product=name+suffix
                if not force and policy.key("ukmo",cycle,hour,product,"hkor") in completed: continue
                emit(product,"hkor",compose(frames,vertical))
        if full: emit("surfce","hkor",alias_of=("acptot","hkor"))
    except NotReady as error:
        return {"complete":False,"pending":str(error),"pending_reason":getattr(error,"reason",None),**timings}
    return {"complete":policy.expected_keys("ukmo",cycle,hour)<=completed,**timings}
