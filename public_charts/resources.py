"""Conservative worker sizing, laptop VTG priority, and read-only Linux metrics."""
from collections import deque
import os
from pathlib import Path
import subprocess
from threading import Event, Thread, Lock, RLock
import time

_windows_memory_lock = Lock()
_windows_memory = {"at": float("-inf"), "value": None}


def windows_available_memory(*, force=False):
    """A WSL VM's free allowance is not necessarily free physical RAM on its host."""
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not powershell.exists():
        return None
    with _windows_memory_lock:
        now = time.monotonic()
        if force or now - _windows_memory["at"] >= 10:
            try:
                value = subprocess.check_output(
                    [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                     "[Console]::Write([string]([long](Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory*1024))"],
                    text=True, timeout=5).strip()
                _windows_memory["value"] = int(value)
            except (OSError, ValueError, subprocess.SubprocessError):
                _windows_memory["value"] = None
            _windows_memory["at"] = now
        return _windows_memory["value"]


def available_memory():
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        free = int(next(line.split()[1] for line in lines if line.startswith("MemAvailable:"))) * 1024
        try:
            limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            if limit != "max":
                current = int(Path("/sys/fs/cgroup/memory.current").read_text())
                free = min(free, max(0, int(limit) - current))
        except (OSError, ValueError):
            pass
        return free
    except (OSError, ValueError, StopIteration):
        return None


def cpu_capacity():
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return max(1, count)


def worker_count(requested="auto", *, local=False, memory=None, cpus=None):
    if local and requested == "auto":
        return 1
    free = available_memory() if memory is None else memory
    cpus = cpu_capacity() if cpus is None else cpus
    cap = 4 if free is not None and free >= 8*1024**3 else 3 if free is not None and free >= 6*1024**3 else 2
    if free is not None and free < 3*1024**3:
        cap = 1
    cap = min(cap, cpus, 4)
    return cap if requested == "auto" else min(int(requested), cap)


def process_rss(pid):
    try:
        lines = Path(f"/proc/{int(pid)}/status").read_text().splitlines()
        return int(next(line.split()[1] for line in lines if line.startswith("VmRSS:"))) * 1024
    except (OSError, ValueError, StopIteration):
        return None


class BatchWindow:
    """Allow one bounded rollover deferral for already-ready operational work."""

    def __init__(self,minutes,*,adaptive=False,clock=None):
        self.clock=clock or time.monotonic
        self.minutes,self.adaptive,self.extra_seconds=minutes,adaptive,0
        self.deadline=self.clock()+minutes*60 if minutes else None

    def extend(self,ready):
        if (not ready or not self.adaptive or self.extra_seconds or not 10<=self.minutes<30
                or not self.deadline-90<=self.clock()<self.deadline):
            return 0
        self.extra_seconds=min(600,(30-self.minutes)*60)
        self.deadline+=self.extra_seconds
        return self.extra_seconds


class WorkerMemoryBudget:
    """Reserve growth headroom across tasks, without model-specific limits."""

    GIB = 1024**3
    HEAVY_RSS = GIB
    LINUX_RESERVE = GIB
    WINDOWS_RESERVE = 1536*1024**2
    LEARNED_FLOOR = 512*1024**2
    LEARNED_MARGIN = 128*1024**2
    LEARNED_SAMPLES = 3

    def __init__(self, workers, *, memory_reader=None, host_reader=None, rss_reader=None, clock=None,
                 laptop_profiles=None):
        self.workers = workers
        self.memory_reader = memory_reader or available_memory
        self.host_reader = host_reader or windows_available_memory
        self.rss_reader = rss_reader or process_rss
        self.clock = clock or time.monotonic
        self.lock, self.tasks, self.recent_peaks = RLock(), {}, deque(maxlen=8)
        self.profiles = {}
        self.laptop_profiles = laptop_profiles
        self.phase_limits = {"render":workers, "prepare":workers}
        self.checked, self.values = float("-inf"), {}
        self.pause_started, self.pause_count, self.pause_seconds = None, 0, 0.

    @staticmethod
    def key(phase, target):
        return phase,target["model"],target["cycle"],target["hour"]

    def start(self, phase, target):
        with self.lock:
            self.tasks[self.key(phase,target)] = {"pid":None,"rss":0,"peak":0,
                                                  "profile":(phase,target["model"])}
            self.checked = float("-inf")

    def configure_pipeline(self, render_workers, prepare_workers):
        with self.lock:
            self.workers = render_workers+prepare_workers
            self.phase_limits = {"render":render_workers, "prepare":prepare_workers}
            self.checked = float("-inf")

    def estimate(self, phase="render", target=None):
        """Learn measured workload peaks, never impose a model-specific worker cap."""
        if target is None:
            peaks = [*self.recent_peaks, *(item["peak"] for item in self.tasks.values())]
        else:
            profile = phase,target["model"]
            peaks = [*self.profiles.get(profile,()),
                     *(item["peak"] for item in self.tasks.values() if item["profile"] == profile)]
        peak = max(peaks,default=0)
        if self.laptop_profiles is not None and target is not None:
            samples = self.laptop_profiles.get(self.profile_key(phase,target), ())
            if len(samples) >= self.LEARNED_SAMPLES:
                return max(self.LEARNED_FLOOR, int(max(peak, max(samples))*1.25)+self.LEARNED_MARGIN)
        return max(self.GIB, int(peak*1.25))

    @staticmethod
    def profile_key(phase,target):
        from . import policy
        model,cycle,hour = target["model"],target["cycle"],target["hour"]
        band = "zero" if hour == 0 else "short" if hour <= 120 else "long"
        winter = policy.winter(model,cycle)
        upper = "300" if "gph300" in policy.products(cycle) else "200"
        return f"{phase}:{model}:{int(winter)}:{upper}:{band}"

    def spare_bytes(self, state):
        free, host = (state[k] for k in ("available_memory_bytes","windows_available_memory_bytes"))
        if self.laptop_profiles is not None and (free is None or host is None):
            return 0
        rooms = [value-reserve for value,reserve in ((free,self.LINUX_RESERVE),(host,self.WINDOWS_RESERVE))
                 if value is not None]
        return min(rooms)-self.growth() if rooms else None

    def trial_fits(self, target):
        with self.lock:
            state = self.snapshot()
            if self.laptop_profiles is None or self.tasks or state["capacity"]:
                return False
            spare = self.spare_bytes(state)
            return all(self.estimate(phase,target) <= spare for phase in ("prepare","render"))

    def running(self,target):
        with self.lock:
            return any(self.key(phase,target) in self.tasks for phase in ("prepare","render"))

    def host_pressured(self, targets):
        with self.lock:
            state = self.snapshot()
            free, host = state["available_memory_bytes"],state["windows_available_memory_bytes"]
            if free is None or host is None or not targets:
                return False
            required = min(max(self.estimate(phase,target) for phase in ("prepare","render"))
                           for target in targets) + self.growth()
            return host-self.WINDOWS_RESERVE < required <= free-self.LINUX_RESERVE

    def growth(self):
        return sum(max(0,self.estimate(phase,{"model":model,"cycle":cycle,"hour":hour})-item["rss"])
                   for (phase,model,cycle,hour),item in self.tasks.items())

    def observe(self, phase, target, pid):
        with self.lock:
            item = self.tasks.get(self.key(phase,target))
            if item is not None and type(pid) is int and pid > 0:
                item["pid"] = pid
                self.checked = float("-inf")

    def snapshot(self, force=False):
        with self.lock:
            now = self.clock()
            if not force and now-self.checked < 1:
                return dict(self.values)
            free, host = self.memory_reader(), self.host_reader()
            for item in self.tasks.values():
                rss = self.rss_reader(item["pid"]) if item["pid"] else None
                if rss is not None:
                    item["rss"] = max(0,rss)
                    item["peak"] = max(item["peak"],item["rss"])
            estimate = self.estimate()
            metrics = {"available_memory_bytes":free,"windows_available_memory_bytes":host}
            room = self.spare_bytes(metrics)
            # Lease capacity advertises minimum-sized jobs; can_start enforces each actual profile.
            spare = max(0,room//self.GIB) if room is not None else self.workers
            active = len(self.tasks)
            heavy = sum(item["rss"] >= self.HEAVY_RSS for item in self.tasks.values())
            self.values = {**metrics,"capacity":min(self.workers,active+spare),
                           "active_workers":active,"heavy_workers":min(self.workers,heavy+spare),
                           "active_render_workers":sum(key[0] == "render" for key in self.tasks),
                           "active_prepare_workers":sum(key[0] == "prepare" for key in self.tasks),
                           "heavy_active_workers":heavy,"worker_reserve_mib":round(estimate/1024**2,1),
                           "lease_reserve_mib":1024.,
                           "available_memory_mib":round(free/1024**2,1) if free is not None else None,
                           "windows_available_memory_mib":round(host/1024**2,1) if host is not None else None}
            self.checked = now
            return dict(self.values)

    def can_start(self, phase="render", target=None):
        with self.lock:
            state = self.snapshot()
            room = self.spare_bytes(state)
            slots = (len(self.tasks) < self.workers
                     and sum(key[0] == phase for key in self.tasks) < self.phase_limits.get(phase,0))
            fits = room is None or room >= self.estimate(phase,target)
            if self.laptop_profiles is not None and room is not None and room < self.GIB:
                fits = fits and not self.tasks
            allowed = slots and fits
            pressured = slots and not fits
            now = self.clock()
            if pressured and self.pause_started is None:
                self.pause_started, self.pause_count = now, self.pause_count+1
            elif not pressured and self.pause_started is not None:
                self.pause_seconds += now-self.pause_started
                self.pause_started = None
            return allowed

    def finish(self, phase, target, pid=None, *, successful=False):
        with self.lock:
            self.observe(phase,target,pid)
            self.snapshot(force=True)
            item = self.tasks.pop(self.key(phase,target),None)
            if item and item["peak"]:
                self.recent_peaks.append(item["peak"])
                self.profiles.setdefault(item["profile"],deque(maxlen=8)).append(item["peak"])
                if self.laptop_profiles is not None and successful:
                    key = self.profile_key(phase,target)
                    self.laptop_profiles.setdefault(key,deque(maxlen=8)).append(item["peak"])
            self.checked = float("-inf")
            return {"worker_pid":item["pid"] if item else pid,
                    "worker_peak_rss_mib":round(item["peak"]/1024**2,1) if item else 0,
                    "worker_estimated_mib":round(self.estimate(phase,target)/1024**2,1),
                    "heavy_worker":bool(item and item["peak"] >= self.HEAVY_RSS)}

    def summary(self):
        with self.lock:
            seconds = self.pause_seconds + (self.clock()-self.pause_started if self.pause_started is not None else 0)
            return {"memory_pause_count":self.pause_count,"memory_pause_seconds":round(seconds,1),
                    "memory_profiles_mib":{f"{phase}:{model}":round(max(values)*1.25/1024**2,1)
                                           for (phase,model),values in self.profiles.items()}}


class LaptopLease:
    def __init__(self):
        value = os.environ.get("CDS_VTG_RUNNER_ROOT")
        self.root = Path(value).resolve() if value else None
        self.handle = None
        if self.root is not None and not (self.root / "bin/Runner.Listener").is_file():
            raise RuntimeError("Configured VTG runner root is unavailable")

    def vtg_busy(self):
        if self.root is None:
            return False
        executable = (self.root / "bin/Runner.Worker").resolve()
        for process in Path("/proc").iterdir():
            if process.name.isdigit():
                try:
                    if (process / "exe").resolve(strict=True) == executable:
                        return True
                except (OSError, RuntimeError):
                    pass
        return False

    def acquire(self):
        if self.root is None:
            return True
        import fcntl
        directory = Path.home() / ".cache/cds-render"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = (directory / "owner.lock").open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def close(self):
        if self.handle:
            self.handle.close()


class ResourceSampler:
    def __init__(self):
        self.stop = Event()
        self.thread = None
        self.peak_rss = 0
        self.lowest_free = available_memory()
        self.lowest_windows_free = windows_available_memory()

    def sample(self):
        free = available_memory()
        if free is not None:
            self.lowest_free = free if self.lowest_free is None else min(self.lowest_free, free)
        host = windows_available_memory()
        if host is not None:
            self.lowest_windows_free = host if self.lowest_windows_free is None else min(self.lowest_windows_free, host)
        processes = {}
        try:
            for directory in Path("/proc").iterdir():
                if directory.name.isdigit():
                    try:
                        fields = dict(line.split(":", 1) for line in (directory / "status").read_text().splitlines() if ":" in line)
                        processes[int(directory.name)] = (int(fields["PPid"]), int(fields.get("VmRSS", "0 kB").split()[0])*1024)
                    except (OSError, ValueError, KeyError):
                        pass
            family = {os.getpid()}
            while True:
                added = {pid for pid, (parent, _) in processes.items() if parent in family} - family
                if not added:
                    break
                family.update(added)
            self.peak_rss = max(self.peak_rss, sum(processes.get(pid, (0, 0))[1] for pid in family))
        except OSError:
            pass

    def start(self):
        def watch():
            while not self.stop.is_set():
                self.sample()
                self.stop.wait(2)
        self.thread = Thread(target=watch, name="chart-resource-sampler")
        self.thread.start()

    def finish(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        return {"peak_process_tree_rss_mib": round(self.peak_rss/1024**2, 1),
                "lowest_available_memory_mib": round(self.lowest_free/1024**2, 1) if self.lowest_free is not None else None,
                "lowest_windows_available_memory_mib": round(self.lowest_windows_free/1024**2, 1)
                                                       if self.lowest_windows_free is not None else None}
