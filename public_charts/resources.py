"""Conservative worker sizing, laptop VTG priority, and read-only Linux metrics."""
import os
from pathlib import Path
from threading import Event, Thread


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
    cap = 4 if free is not None and free >= 6*1024**3 else 3 if free is not None and free >= 4*1024**3 else 2
    if free is not None and free < 2*1024**3:
        cap = 1
    cap = min(cap, cpus, 4)
    return cap if requested == "auto" else min(int(requested), cap)


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
            return not self.vtg_busy()
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

    def sample(self):
        free = available_memory()
        if free is not None:
            self.lowest_free = free if self.lowest_free is None else min(self.lowest_free, free)
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
                "lowest_available_memory_mib": round(self.lowest_free/1024**2, 1) if self.lowest_free is not None else None}
