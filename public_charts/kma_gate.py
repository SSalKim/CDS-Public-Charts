"""Nonblocking, product-specific KMA release gate for computed UKMO and ECMWF extensions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
import struct
import time

import requests

from . import policy


def gate_key(row):
    """(model, cycle, KMA lead, KMA product, domain) of the KMA image a computed chart waits for."""
    model=row.get("model")
    if model not in policy.KMA_GATED_MODELS: return None
    reference=policy.kma_gate_hour(model,row["cycle"],row["hour"])
    if reference is None: return None
    product=row.get("product","").removesuffix("1")
    domain=row["domain"]
    policy.key(model,row["cycle"],row["hour"],row["product"],domain)
    # KMA draws 200 hPa where the winter set uses 300 hPa, and has no Korea surface chart.
    if product=="gph300": product="gph200"
    if product=="surfce" and domain=="hkor": product="acptot"
    return (model,row["cycle"],reference,product,domain)


def kma_image(key):
    model,cycle,hour,product,domain=key
    if model=="ecmwf":
        projection="lc20" if domain=="hkor" else "lc40"
        return "ECMW",f"ecmw_{projection}_{domain}_{product}_s{hour:03}_{cycle}.gif"
    return "GDPS",f"ukum_erly_{domain}_{product}_s{hour:03}_{cycle}.png"


def image_header_valid(header):
    if header[:8]==b"\x89PNG\r\n\x1a\n" and len(header)>=24 and header[12:16]==b"IHDR":
        width,height=struct.unpack(">II",header[16:24])
    elif header[:6] in {b"GIF87a",b"GIF89a"} and len(header)>=10:
        width,height=struct.unpack("<HH",header[6:10])
    else: return False
    return 400<=width<=10000 and 400<=height<=10000


def probe_kma(key):
    cycle=key[1]
    folder,filename=kma_image(key)
    for base in ("https://data.kma.go.kr/CHT","https://dmdw.kma.go.kr/map/data/CHT"):
        url=f"{base}/{folder}/{cycle[:6]}/{cycle[6:8]}/{filename}"
        try:
            with requests.get(url,headers={"Range":"bytes=0-63","User-Agent":"CDS-KMA-release-gate/1.0"},
                              stream=True,timeout=(2,3),allow_redirects=False) as response:
                if response.status_code in (200,206) and image_header_valid(response.raw.read(64)):
                    return datetime.now(timezone.utc).isoformat()
        except requests.RequestException:
            continue
    return None


class KmaReleaseGate:
    def __init__(self,*,probe=probe_kma,clock=time.monotonic):
        self.probe,self.clock=probe,clock
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="kma-release-gate")
        self.pending={};self.due={};self.confirmed={}
        self.closed=False

    def poll(self,rows):
        changed=False
        for future,key in list(self.pending.items()):
            if not future.done(): continue
            self.pending.pop(future)
            try: confirmed=future.result()
            except Exception: confirmed=None
            self.due[key]=self.clock()+60
            if confirmed: self.confirmed[key]=confirmed
        waiting={}
        for row in rows.values():
            key=gate_key(row)
            if key is None: continue
            if row.get("kma_confirmed_at") and policy.kma_public(row):
                self.confirmed[key]=row["kma_confirmed_at"]
            elif key in self.confirmed:
                row["kma_confirmed_at"]=self.confirmed[key]
                row["kma_reference_hour"]=key[2]
                changed=True
                print(json.dumps({"event":f"{key[0]}_kma_release_confirmed","cycle":key[1],"hour":key[2],
                                  "product":key[3],"domain":key[4]}),flush=True)
            else: waiting[key]=True
        active=set(self.pending.values())
        for key in sorted(waiting,key=lambda key:(-int(key[1]),key[2],key[0],key[3],key[4])):
            if len(self.pending)>=2 or self.closed: break
            if key in active or self.clock()<self.due.get(key,0): continue
            self.pending[self.pool.submit(self.probe,key)]=key
        return changed

    def close(self):
        self.closed=True
        for future in self.pending: future.cancel()
        self.pool.shutdown(wait=True,cancel_futures=True)
