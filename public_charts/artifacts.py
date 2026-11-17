"""Short-lived Actions artifacts; no Drive credentials or public-state writes."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
import zipfile
import zlib

import requests

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
SESSION_PATTERN = re.compile(r"[1-9][0-9]*-[1-9][0-9]*\Z")


class ArtifactError(RuntimeError):
    def __init__(self, message, *, operation=None, http_status=None):
        super().__init__(message)
        self.details = {key:value for key,value in {"operation":operation,"http_status":http_status}.items() if value is not None}


def validate_session(value):
    if not isinstance(value, str) or not SESSION_PATTERN.fullmatch(value):
        raise ValueError("Invalid coordinator session")
    return value


def unpack(payload):
    """Read a bounded flat archive without extracting paths from its contents."""
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ArtifactError("Artifact archive exceeds its byte limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (not 1 <= len(entries) <= 25 or len(names) != len(set(names))
                    or "manifest.json" not in names or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES):
                raise ArtifactError("Invalid artifact archive entries")
            for item in entries:
                if (not re.fullmatch(r"manifest\.json|[a-f0-9]{64}\.webp", item.filename)
                        or item.flag_bits & 1 or (item.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ArtifactError("Unsafe artifact archive entry")
                if item.filename == "manifest.json" and item.file_size > MAX_METADATA_BYTES:
                    raise ArtifactError("Artifact metadata exceeds its byte limit")
            files = {item.filename: archive.read(item) for item in entries}
            manifest = json.loads(files.pop("manifest.json"))
            if not isinstance(manifest, dict):
                raise ValueError()
            return manifest, files
    except (zipfile.BadZipFile, UnicodeError, ValueError, KeyError, OSError, RuntimeError, zlib.error):
        raise ArtifactError("Invalid artifact payload") from None


class GitHubArtifacts:
    def __init__(self, repository=None, token=None):
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid artifact repository")
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        if not self.token:
            raise ArtifactError("Actions token unavailable")
        self.http = requests.Session()
        self.http.headers.update({"Authorization": "Bearer " + self.token,
                                  "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.created = 0

    def api(self, method, endpoint, **kwargs):
        if not endpoint.startswith(f"repos/{self.repository}/actions/"):
            raise ArtifactError("Unexpected Actions endpoint")
        operation=("artifact_lookup" if endpoint.endswith("/zip") else "artifact_list" if endpoint.endswith("/artifacts")
                   else "helper_dispatch" if endpoint.endswith("/dispatches") else "helper_discovery" if endpoint.endswith("/runs")
                   else "artifact_delete" if method == "DELETE" else "actions_request")
        try:
            response = self.http.request(method, "https://api.github.com/" + endpoint,
                                         timeout=(10, 30), allow_redirects=False, **kwargs)
            if response.status_code not in (200, 201, 202, 204, 302):
                raise ArtifactError(f"Actions request failed: {response.status_code}",operation=operation,http_status=response.status_code)
            return response
        except requests.RequestException as error:
            raise ArtifactError(f"Actions transport failed: {type(error).__name__}",operation=operation) from None

    def list(self, run_id, prefix):
        result = []
        for page in range(1, 7):
            response = self.api("GET", f"repos/{self.repository}/actions/runs/{int(run_id)}/artifacts",
                                params={"per_page": 100, "page": page}).json()
            entries = response.get("artifacts") if isinstance(response,dict) else None
            if (not isinstance(entries, list) or any(not isinstance(item,dict)
                    or not isinstance(item.get("name"),str) or type(item.get("id")) is not int for item in entries)):
                raise ArtifactError("Invalid Actions artifact list")
            result.extend(item for item in entries if item.get("name", "").startswith(prefix)
                          and not item.get("expired"))
            if len(entries) < 100:
                return result
        raise ArtifactError("Actions artifact pagination exceeded its limit")

    def read(self, item):
        response = self.api("GET", f"repos/{self.repository}/actions/artifacts/{int(item['id'])}/zip")
        location = response.headers.get("Location", "")
        host = urlsplit(location)
        if (host.scheme != "https" or host.username or host.password or host.port not in (None, 443)
                or not host.hostname or not host.hostname.endswith((".blob.core.windows.net", ".actions.githubusercontent.com",
                                                                  ".githubusercontent.com", ".amazonaws.com"))):
            raise ArtifactError("Unexpected artifact download destination")
        # The signed object URL must never receive the GitHub authorization header.
        try:
            with requests.get(location, stream=True, allow_redirects=False, timeout=(10, 60)) as download:
                if download.status_code != 200:
                    raise ArtifactError("Artifact download failed",operation="artifact_download",http_status=download.status_code)
                chunks, size = [], 0
                for chunk in download.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ArtifactError("Artifact download exceeds its byte limit")
                    chunks.append(chunk)
            payload = b"".join(chunks)
        except requests.RequestException as error:
            raise ArtifactError(f"Artifact download transport failed: {type(error).__name__}",operation="artifact_download") from None
        digest = item.get("digest")
        if digest and digest != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise ArtifactError("Actions artifact digest mismatch")
        return unpack(payload)

    def upload(self, name, manifest, files=None):
        if self.created >= 400:
            raise ArtifactError("Artifact production limit reached")
        if not re.fullmatch(r"chart-(?:control|result)-[1-9][0-9]*-[1-9][0-9]*-[0-9]{6}", name):
            raise ArtifactError("Unexpected artifact name")
        with tempfile.TemporaryDirectory(prefix="chart-transfer-") as directory:
            root = Path(directory)
            metadata = json.dumps(manifest, allow_nan=False).encode("utf-8")
            if len(metadata) > MAX_METADATA_BYTES or len(files or {}) > 24:
                raise ArtifactError("Transfer metadata exceeds its limit")
            (root / "manifest.json").write_bytes(metadata)
            size = len(metadata)
            for filename, source in (files or {}).items():
                if not re.fullmatch(r"[a-f0-9]{64}\.webp", filename):
                    raise ArtifactError("Unexpected transfer filename")
                size += Path(source).stat().st_size
                if size > MAX_ARCHIVE_BYTES:
                    raise ArtifactError("Transfer exceeds its byte limit")
                target = root / filename
                target.write_bytes(Path(source).read_bytes())
            command = ["node", str(Path(__file__).parent / "transport/upload.cjs"), name, str(root)]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=120)
            if result.returncode:
                raise ArtifactError("Actions artifact upload failed",operation="artifact_upload")
            marker = next((line for line in result.stdout.splitlines() if line.startswith("CHART_ARTIFACT_RESPONSE ")), None)
            if marker is None:
                raise ArtifactError("Actions artifact upload returned no ID")
            self.created += 1
            return json.loads(marker.partition(" ")[2])["id"]

    def delete(self, artifact_id):
        self.api("DELETE", f"repos/{self.repository}/actions/artifacts/{int(artifact_id)}")

    def dispatch_helper(self, session, code_sha, smoke=False, smoke_runner="github-hosted"):
        validate_session(session)
        if not re.fullmatch(r"[a-f0-9]{40}", code_sha) or smoke_runner not in {"github-hosted","laptop"}:
            raise ValueError("Invalid helper code revision")
        self.api("POST", f"repos/{self.repository}/actions/workflows/public_charts_helper.yml/dispatches",
                 json={"ref": "main", "inputs": {"session": session, "code_sha": code_sha,
                       "smoke": str(smoke).lower(),"smoke_runner":smoke_runner}})

    def helper_run(self, session):
        payload = self.api("GET", f"repos/{self.repository}/actions/workflows/public_charts_helper.yml/runs",
                           params={"per_page": 30, "event": "workflow_dispatch"}).json()
        return next((item for item in payload.get("workflow_runs", [])
                     if item.get("display_title") == "Chart compute helper " + session), None)

    def cancel(self, run_id):
        self.api("POST", f"repos/{self.repository}/actions/runs/{int(run_id)}/cancel")
