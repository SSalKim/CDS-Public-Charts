"""Conservative worker sizing, laptop VTG priority, and read-only Linux metrics."""
from collections import deque
import os
from pathlib import Path
import subprocess
from threading import Event, Thread, Lock, RLock
import time

_windows_memory_lock = Lock()
_windows_memory = {"at": float("-inf"), "value": None}


def windows_available_memory():
    """A WSL VM's free allowance is not necessarily free physical RAM on its host."""
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not powershell.exists():
        return None
    with _windows_memory_lock:
        now = time.monotonic()
        if now - _windows_memory["at"] >= 10:
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


class WorkerMemoryBudget:
    """Reserve growth headroom across tasks, without model-specific limits."""

    GIB = 1024**3
    HEAVY_RSS = GIB
    LINUX_RESERVE = GIB
    WINDOWS_RESERVE = 1536*1024**2

    def __init__(self, workers, *, memory_reader=None, host_reader=None, rss_reader=None, clock=None):
        self.workers = workers
        self.memory_reader = memory_reader or available_memory
        self.host_reader = host_reader or windows_available_memory
        self.rss_reader = rss_reader or process_rss
        self.clock = clock or time.monotonic
        self.lock, self.tasks, self.recent_peaks = RLock(), {}, deque(maxlen=8)
        self.checked, self.values = float("-inf"), {}
        self.pause_started, self.pause_count, self.pause_seconds = None, 0, 0.

    @staticmethod
    def key(phase, target):
        return phase,target["model"],target["cycle"],target["hour"]

    def start(self, phase, target):
        with self.lock:
            self.tasks[self.key(phase,target)] = {"pid":None,"rss":0,"peak":0}
            self.checked = float("-inf")

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
            observed_peak = max([*self.recent_peaks,*(item["peak"] for item in self.tasks.values())],default=0)
            estimate = max(self.GIB,int(observed_peak*1.25))
            growth = sum(max(0,estimate-item["rss"]) for item in self.tasks.values())
            rooms = [value-reserve for value,reserve in ((free,self.LINUX_RESERVE),(host,self.WINDOWS_RESERVE))
                     if value is not None]
            spare = max(0,int((min(rooms)-growth)//estimate)) if rooms else self.workers
            active = len(self.tasks)
            heavy = sum(item["rss"] >= self.HEAVY_RSS for item in self.tasks.values())
            self.values = {"capacity":min(self.workers,active+spare),
                           "active_workers":active,"heavy_workers":min(self.workers,heavy+spare),
                           "heavy_active_workers":heavy,"worker_reserve_mib":round(estimate/1024**2,1),
                           "available_memory_mib":round(free/1024**2,1) if free is not None else None,
                           "windows_available_memory_mib":round(host/1024**2,1) if host is not None else None}
            self.checked = now
            return dict(self.values)

    def can_start(self):
        with self.lock:
            state = self.snapshot()
            allowed = state["active_workers"] < state["capacity"]
            pressured = not allowed and state["active_workers"] < self.workers
            now = self.clock()
            if pressured and self.pause_started is None:
                self.pause_started, self.pause_count = now, self.pause_count+1
            elif not pressured and self.pause_started is not None:
                self.pause_seconds += now-self.pause_started
                self.pause_started = None
            return allowed

    def finish(self, phase, target, pid=None):
        with self.lock:
            self.observe(phase,target,pid)
            self.snapshot(force=True)
            item = self.tasks.pop(self.key(phase,target),None)
            if item and item["peak"]:
                self.recent_peaks.append(item["peak"])
            self.checked = float("-inf")
            return {"worker_pid":item["pid"] if item else pid,
                    "worker_peak_rss_mib":round(item["peak"]/1024**2,1) if item else 0,
                    "heavy_worker":bool(item and item["peak"] >= self.HEAVY_RSS)}

    def summary(self):
        with self.lock:
            seconds = self.pause_seconds + (self.clock()-self.pause_started if self.pause_started is not None else 0)
            return {"memory_pause_count":self.pause_count,"memory_pause_seconds":round(seconds,1)}


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
