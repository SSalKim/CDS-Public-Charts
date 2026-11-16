"""Publish only the verified manifest, preserving unrelated staged changes."""
import json
from pathlib import Path
import subprocess
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
    expected = json.loads(path.read_text(encoding="utf-8"))

    def git(*args, check=True):
        return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *BOT_CONFIG, *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", check=check, timeout=120)

    try:
        git("add", "--", MANIFEST)
        changed = git("diff", "--cached", "--quiet", "--", MANIFEST, check=False).returncode
        if changed not in (0, 1):
            raise PublicationError("Cannot inspect the public manifest diff")
        if changed:
            git("commit", "--only", "-m", "Update public chart availability", "--", MANIFEST)
        # Preserve the path back to the runner's shallow boundary when main moves.
        git("fetch", "origin", "main")
        published = json.loads(git("show", f"origin/main:{MANIFEST}").stdout)
        if expected != published:
            files = set(git("log", "--format=", "--name-only", "origin/main..HEAD").stdout.split())
            if files != {MANIFEST}:
                raise PublicationError("Unpublished changes are not a manifest-only commit")
            for attempt in range(3):
                if git("push", "origin", "HEAD:main", check=False).returncode == 0:
                    break
                if attempt == 2:
                    raise PublicationError("Public manifest push failed; cleanup is blocked")
                git("pull", "--rebase", "origin", "main")
                time.sleep(3)
            git("fetch", "origin", "main")
            published = json.loads(git("show", f"origin/main:{MANIFEST}").stdout)
        if expected != published:
            raise PublicationError("Remote manifest differs; cleanup is blocked")
    except subprocess.CalledProcessError as error:
        operation = next((word for word in error.cmd if word in {
            "add", "diff", "commit", "fetch", "show", "log", "push", "pull"}), "git")
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


if __name__ == "__main__":
    publish_manifest(Path(__file__).resolve().parent)
