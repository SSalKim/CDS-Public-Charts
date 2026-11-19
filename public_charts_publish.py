"""Publish only the verified manifest, preserving unrelated staged changes."""
import json
from pathlib import Path
import subprocess
import tempfile
import time

MANIFEST = "data/public_charts_manifest.json"
BOT_CONFIG = ("-c", "user.name=github-actions[bot]", "-c",
              "user.email=41898282+github-actions[bot]@users.noreply.github.com")


class PublicationError(RuntimeError):
    pass


def publish_manifest(root):
    root = Path(root).resolve()
    path = root / MANIFEST
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise PublicationError("Unsafe public manifest path")
    payload = path.read_bytes()
    expected = json.loads(payload)

    def git(*args, check=True, cwd=root):
        return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}",
                               "-c",f"safe.directory={Path(cwd).as_posix()}",*BOT_CONFIG, *args], cwd=cwd,
                              capture_output=True, text=True, encoding="utf-8", check=check, timeout=120)

    try:
        # Never rebase the checkout used by live Python processes and lazy imports.
        git("fetch", "origin", "main")
        published = json.loads(git("show", f"origin/main:{MANIFEST}").stdout)
        changed = expected != published
        if expected != published:
            files = set(git("log", "--format=", "--name-only", "origin/main..HEAD").stdout.split())
            if files - {MANIFEST}:
                raise PublicationError("Unpublished changes are not a manifest-only commit")
            with tempfile.TemporaryDirectory(prefix="chart-manifest-publish-") as temporary:
                publication = Path(temporary).resolve()/"checkout"
                git("worktree","add","--detach",str(publication),"origin/main")
                try:
                    destination=publication/MANIFEST
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    destination.write_bytes(payload)
                    git("add","--",MANIFEST,cwd=publication)
                    git("commit","--only","-m","Update public chart availability","--",MANIFEST,cwd=publication)
                    for attempt in range(3):
                        if git("push", "origin", "HEAD:main", check=False,cwd=publication).returncode == 0:
                            break
                        if attempt == 2:
                            raise PublicationError("Public manifest push failed; cleanup is blocked")
                        git("pull", "--rebase", "origin", "main",cwd=publication)
                        time.sleep(3)
                finally:
                    # Only this call's freshly created temporary worktree is removed.
                    git("worktree","remove","--force",str(publication),check=False)
            git("fetch", "origin", "main")
            published = json.loads(git("show", f"origin/main:{MANIFEST}").stdout)
        if expected != published:
            raise PublicationError("Remote manifest differs; cleanup is blocked")
    except subprocess.CalledProcessError as error:
        operation = next((word for word in error.cmd if word in {
            "add", "diff", "commit", "fetch", "show", "log", "push", "pull", "worktree"}), "git")
        stderr = (error.stderr or "").lower()
        reason = next((label for pattern, label in (
            ("identity unknown", "missing_bot_identity"),
            ("unstaged changes", "dirty_worktree"),
            ("uncommitted changes", "dirty_index"),
            ("conflict", "rebase_conflict"),
            ("could not resolve", "network_resolution"),
            ("authentication failed", "authentication"),
            ("permission denied", "permission"),
            ("index.lock", "index_lock"),
        ) if pattern in stderr), "git_error")
        raise PublicationError(f"Public manifest {operation} failed ({reason}, exit {error.returncode}); cleanup is blocked") from None
    except (OSError, subprocess.SubprocessError, ValueError):
        raise PublicationError("Public manifest publication failed; cleanup is blocked") from None
    print(json.dumps({"event": "manifest_published", "charts": len(published.get("charts", [])),
                      "changed": bool(changed)}), flush=True)
    return published


def publish_r2(root, env=None, publisher=None):
    """Final R2 publication before Drive cleanup.

    In ``required`` mode a failure blocks cleanup, so R2 readers never see deleted images.
    In ``shadow`` mode a failure is reported and Git remains the only gate.
    """
    from public_charts import r2
    selected, publisher = (r2.mode(env), publisher) if publisher else r2.publisher_from_env(env)
    if selected == "off":
        return "off"
    manifest = json.loads((Path(root).resolve() / MANIFEST).read_bytes())
    status = r2.safe_publish(publisher, manifest, force=True)
    if status == "failed" and selected == "required":
        raise PublicationError("R2 manifest publication failed; cleanup is blocked")
    return status


if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parent
    publish_manifest(ROOT)
    publish_r2(ROOT)
