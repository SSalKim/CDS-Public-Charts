"""Best-effort housekeeping for the dedicated, persistent chart runner."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
from threading import Thread
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .resources import windows_available_memory

MIB = 1024**2
COLD_SECONDS = 30*60
RECLAIM_BYTES = 512*MIB
RECLAIM_INTERVAL = 120


def profile_file(work):
    package = Path(__file__).parent
    sources = sorted(package.glob("*.py")) + sorted(package.parent.glob("requirements-*charts.txt"))
    digest = hashlib.sha256(sys.version.encode())
    for path in sources:
        digest.update(path.name.encode()+b"\0"+path.read_bytes())
    return Path(work)/f"memory-profiles-{digest.hexdigest()[:16]}.json"


def load_profiles(path):
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return {}
        profiles = json.loads(path.read_text())
        if not isinstance(profiles,dict):
            return {}
        return {key:deque(values[-8:],maxlen=8) for key,values in profiles.items()
                if re.fullmatch(r"(prepare|render):[a-z]+:[01]:(200|300):(zero|short|long)",key)
                and isinstance(values,list) and 0 < len(values) <= 8 and
                all(type(value) is int and 0 < value <= 32*1024*MIB for value in values)}
    except (OSError,ValueError):
        return {}


def save_profiles(path, profiles):
    if not profiles:
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("x",encoding="ascii") as handle:
        json.dump({key:list(values) for key,values in profiles.items()},handle)
    temporary.replace(path)


def advise_cold_files(work, protected_cycles, *, now=None, max_bytes=RECLAIM_BYTES):
    """Hint only on cold, closed-input candidates; never remove their disk copies."""
    if not hasattr(os,"posix_fadvise"):
        return 0
    now = time.time() if now is None else now
    raw = Path(work).resolve()/"raw-cache"
    if raw.is_symlink():
        return 0
    files = []
    for group, pattern, cycle_index in (("prepared","*/*/*/fields.npz",1),
                                        ("partial-fields","*/*/*.npz",0)):
        root = raw/group
        if root.is_symlink():
            continue
        for path in root.glob(pattern):
            cycle = path.relative_to(root).parts[cycle_index]
            if (not re.fullmatch(r"\d{10}",cycle) or cycle in protected_cycles or path.is_symlink()
                    or path.resolve() != path):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            touched = max(info.st_mtime,info.st_atime)
            if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= max_bytes and now-touched >= COLD_SECONDS:
                files.append((touched,path,info))
    requested = 0
    for _,path,info in sorted(files)[:64]:
        if info.st_size > max_bytes-requested:
            continue
        try:
            fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                current = os.fstat(fd)
                if (current.st_ino,current.st_dev,current.st_size,current.st_atime_ns,current.st_mtime_ns) != (
                    info.st_ino,info.st_dev,info.st_size,info.st_atime_ns,info.st_mtime_ns):
                    continue
                os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED)
                requested += info.st_size
            finally:
                os.close(fd)
        except OSError:
            continue
    return requested


class LaptopReclaim:
    def __init__(self, work, budget, *, clock=time.monotonic, advise=advise_cold_files,
                 host_refresh=lambda:windows_available_memory(force=True)):
        self.work, self.budget, self.clock = work,budget,clock
        self.advise, self.host_refresh = advise,host_refresh
        self.next_attempt, self.thread = 0,None

    def maybe_start(self, candidates, protected_cycles):
        now = self.clock()
        if (now < self.next_attempt or self.thread and self.thread.is_alive()
                or not self.budget.host_pressured(candidates)):
            return
        before = self.budget.snapshot()["windows_available_memory_bytes"]
        self.next_attempt = now+RECLAIM_INTERVAL
        self.thread = Thread(target=self._run,args=(frozenset(protected_cycles),before),daemon=True)
        self.thread.start()

    def _run(self, protected_cycles, before):
        try:
            requested = self.advise(self.work,protected_cycles)
            if requested:
                self.host_refresh()
            after = self.budget.snapshot(force=True)["windows_available_memory_bytes"]
            print(json.dumps({"event":"laptop_cache_reclaim","hinted_bytes":requested,
                              "windows_available_before":before,"windows_available_after":after}),flush=True)
        except (OSError,ValueError) as error:
            print(json.dumps({"event":"laptop_cache_reclaim_skipped","error":type(error).__name__}),flush=True)


def cache_saver_run(process, workspace, repository, current_run):
    """Recognize only this repository's old Actions cache-save entry point."""
    runner = workspace.parents[2]
    try:
        if process.stat().st_uid != os.getuid() or (process/"cwd").resolve(strict=True) != workspace:
            return None
        exe = (process/"exe").resolve(strict=True).relative_to(runner).as_posix()
        if not re.fullmatch(r"externals/node\d+/bin/node",exe):
            return None
        args = (process/"cmdline").read_bytes().split(b"\0")
        if len(args) != 3 or not args[1]:
            return None
        script = Path(os.fsdecode(args[1])).resolve()
        if script != workspace.parents[1]/"_actions/actions/cache/v4/dist/save-only/index.js":
            return None
        fields = dict(line.split(b"=",1) for line in (process/"environ").read_bytes().split(b"\0")
                      if line.startswith((b"GITHUB_RUN_ID=",b"GITHUB_REPOSITORY=",b"GITHUB_RUN_ATTEMPT=")))
        run = fields.get(b"GITHUB_RUN_ID",b"").decode("ascii")
        attempt = fields.get(b"GITHUB_RUN_ATTEMPT",b"").decode("ascii")
        if (fields.get(b"GITHUB_REPOSITORY",b"").lower() != repository.encode("ascii").lower()
                or run == current_run or not re.fullmatch(r"[1-9]\d{0,19}",run)
                or not re.fullmatch(r"[1-9]\d{0,5}",attempt)):
            return None
        return run,int(attempt)
    except (OSError,ValueError,UnicodeError):
        return None


def completed_run(repository, run, attempt, token):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = Request(f"https://api.github.com/repos/{repository}/actions/runs/{run}",
                      headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json",
                               "User-Agent":"CDS-laptop-cache-cleanup"})
    with build_opener(NoRedirect).open(request,timeout=3) as reply:
        data = json.load(reply)
    return (data.get("id") == int(run) and data.get("status") == "completed"
            and type(data.get("run_attempt")) is int and data["run_attempt"] >= attempt)


def reap_cache_savers(*, proc=Path("/proc"), env=os.environ, verify=completed_run):
    if (env.get("RUNNER_ENVIRONMENT") != "self-hosted" or not hasattr(os,"pidfd_open")
            or not hasattr(signal,"pidfd_send_signal")):
        return
    repository,current_run,token = (env.get(key,"") for key in ("GITHUB_REPOSITORY","GITHUB_RUN_ID","GH_TOKEN"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repository) or not current_run.isdecimal() or not token:
        return
    workspace = Path(env["GITHUB_WORKSPACE"]).resolve(strict=True)
    if len(workspace.parents) < 3 or workspace.name != repository.split("/")[1]:
        return
    checked, reaped = {}, 0
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        match = cache_saver_run(process,workspace,repository,current_run)
        if not match:
            continue
        try:
            fd = os.pidfd_open(int(process.name))
            try:
                if cache_saver_run(process,workspace,repository,current_run) != match:
                    continue
                if match not in checked:
                    if len(checked) >= 4:
                        break
                    checked[match] = verify(repository,*match,token)
                if not checked[match]:
                    continue
                signal.pidfd_send_signal(fd,signal.SIGTERM)
                wait = select.poll()
                wait.register(fd,select.POLLIN)
                if not wait.poll(3000):
                    signal.pidfd_send_signal(fd,signal.SIGKILL)
                print(json.dumps({"event":"laptop_cache_process_reaped","pid":int(process.name),
                                  "run_id":match[0]}),flush=True)
                reaped += 1
                if reaped >= 8:
                    break
            finally:
                os.close(fd)
        except (OSError,ValueError) as error:
            checked.setdefault(match,False)
            print(json.dumps({"event":"laptop_cache_process_cleanup_skipped","error":type(error).__name__}),flush=True)


if __name__ == "__main__":
    reap_cache_savers()
