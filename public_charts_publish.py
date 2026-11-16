"""Publish only the verified manifest, preserving unrelated staged changes."""
import json
from pathlib import Path
import subprocess
import time

MANIFEST = "data/public_charts_manifest.json"


class PublicationError(RuntimeError):
    pass


def publish_manifest(root):
    root = Path(root).resolve()
    path = root / MANIFEST
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise PublicationError("Unsafe public manifest path")
    expected = json.loads(path.read_text(encoding="utf-8"))

    def git(*args, check=True):
        return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *args], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", check=check, timeout=120)

    try:
        git("add", "--", MANIFEST)
        changed = git("diff", "--cached", "--quiet", "--", MANIFEST, check=False).returncode
        if changed not in (0, 1):
            raise PublicationError("Cannot inspect the public manifest diff")
        if changed:
            git("-c", "user.name=github-actions[bot]", "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "--only", "-m", "Update public chart availability", "--", MANIFEST)
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
    except (OSError, subprocess.SubprocessError, ValueError):
        raise PublicationError("Public manifest publication failed; cleanup is blocked") from None
    print(json.dumps({"event": "manifest_published", "charts": len(published.get("charts", [])),
                      "changed": bool(changed)}), flush=True)
    return published


if __name__ == "__main__":
    publish_manifest(Path(__file__).resolve().parent)
