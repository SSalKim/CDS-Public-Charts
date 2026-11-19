"""Publish the public manifest to Cloudflare R2 as a small index plus per-run fragments.

Readers poll ``manifest/index.json`` (tiny, short cache) and fetch a run fragment only
when its revision changes.  Fragments are content-addressed and never overwritten, so
the index is the single switch that makes a new revision visible.

This module only ever runs on the GitHub-hosted coordinator.  The compute helper must
never receive R2 credentials (see ``public_charts_helper.main``).
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import quote

import requests

from . import policy

SCHEMA = 2
INDEX_KEY = "manifest/index.json"
INDEX_CACHE = "public, max-age=15"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MIN_INDEX_SECONDS = 30
# Rewrite an unchanged index periodically so readers can trust generated_at as a clock.
INDEX_REFRESH_SECONDS = 300
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MODES = ("off", "shadow", "required")
ENV_PREFIX = "PUBLIC_CHARTS_R2_"
IMAGE_STORE = "drive"
DRIVE_URL = "https://drive.usercontent.google.com/download?id={}&export=view&authuser=0"
EXTRA_FIELDS = ("kma_confirmed_at", "kma_reference_hour")
RUN_PATTERN = re.compile(r"(aifs|gfs|aigfs|cmc|ecmwf|icon|ukmo)/\d{10}\Z")
KEY_PATTERN = re.compile(r"manifest/(?:index|runs/[a-z]+/\d{10}\.[a-f0-9]{16})\.json\Z")


class R2Error(RuntimeError):
    def __init__(self, message, *, operation=None, http_status=None):
        super().__init__(message)
        self.details = {key: value for key, value in
                        {"operation": operation, "http_status": http_status}.items() if value is not None}


def _hmac(key, text):
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method, host, path, headers, payload_hash, access_key, secret_key, *,
                  region="auto", service="s3", now=None):
    """Return request headers with an AWS Signature Version 4 Authorization header.

    ``path`` must already be URI-encoded.  Every header in ``headers`` is signed.
    """
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    signed = {name.lower(): " ".join(str(value).strip().split()) for name, value in headers.items()}
    signed.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
    names = sorted(signed)
    canonical = "\n".join([method, path, "", *(f"{name}:{signed[name]}" for name in names), "",
                           ";".join(names), payload_hash])
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode("utf-8"), day), region), service), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = {name: value for name, value in signed.items() if name != "host"}
    result["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                               f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return result


class R2Client:
    """Minimal S3-compatible client limited to one bucket and the manifest prefix."""

    def __init__(self, account_id, access_key, secret_key, bucket, *, session=None, attempts=3, sleep=time.sleep):
        if not re.fullmatch(r"[a-f0-9]{32}", account_id or ""):
            raise ValueError("Invalid R2 account ID")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket or ""):
            raise ValueError("Invalid R2 bucket name")
        if not access_key or not secret_key:
            raise ValueError("R2 credentials are incomplete")
        self.host = f"{account_id}.r2.cloudflarestorage.com"
        self.bucket, self.access_key, self.secret_key = bucket, access_key, secret_key
        self.http = session or requests.Session()
        self.attempts, self.sleep = attempts, sleep
        self.writes = 0
        self.reads = 0

    def _request(self, method, key, body=b"", headers=None):
        if not KEY_PATTERN.fullmatch(key):
            raise R2Error("Unexpected R2 object key", operation=method.lower())
        path = "/" + quote(self.bucket) + "/" + quote(key, safe="/-_.~")
        payload_hash = hashlib.sha256(body).hexdigest()
        last = None
        for attempt in range(self.attempts):
            signed = sigv4_headers(method, self.host, path, headers or {}, payload_hash,
                                   self.access_key, self.secret_key)
            try:
                response = self.http.request(method, f"https://{self.host}{path}", data=body or None,
                                             headers=signed, timeout=(10, 30), allow_redirects=False)
            except requests.RequestException as error:
                last = R2Error(f"R2 transport failed: {type(error).__name__}", operation=method.lower())
            else:
                if method == "PUT":
                    self.writes += 1
                else:
                    self.reads += 1
                if response.status_code in (200, 204) or method == "GET" and response.status_code == 404:
                    return response
                last = R2Error(f"R2 request failed: {response.status_code}", operation=method.lower(),
                               http_status=response.status_code)
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise last
            if attempt + 1 < self.attempts:
                self.sleep(2 ** attempt)
        raise last

    def put(self, key, body, *, cache_control, content_type="application/json"):
        if len(body) > MAX_OBJECT_BYTES:
            raise R2Error("R2 object exceeds its byte limit", operation="put")
        self._request("PUT", key, body, {"cache-control": cache_control, "content-type": content_type})

    def get(self, key):
        response = self._request("GET", key)
        if response.status_code == 404:
            return None
        if len(response.content) > MAX_OBJECT_BYTES:
            raise R2Error("R2 object exceeds its byte limit", operation="get")
        return response.content


def mode(env=None):
    env = os.environ if env is None else env
    value = (env.get(ENV_PREFIX + "MODE") or "").strip().lower()
    configured = all(env.get(ENV_PREFIX + name) for name in ("ACCOUNT_ID", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "BUCKET"))
    if value and value not in MODES:
        raise ValueError("PUBLIC_CHARTS_R2_MODE must be off, shadow, or required")
    if value in ("shadow", "required") and not configured:
        raise ValueError("R2 publication is enabled but its credentials are incomplete")
    return value or ("shadow" if configured else "off")


def client_from_env(env=None):
    env = os.environ if env is None else env
    return R2Client(env[ENV_PREFIX + "ACCOUNT_ID"].strip(), env[ENV_PREFIX + "ACCESS_KEY_ID"].strip(),
                    env[ENV_PREFIX + "SECRET_ACCESS_KEY"].strip(), env[ENV_PREFIX + "BUCKET"].strip())


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def fragment_key(run, rev):
    model, cycle = run.split("/")
    return f"manifest/runs/{model}/{cycle}.{rev}.json"


def split_manifest(manifest):
    """Return ``(models, {run: fragment})`` from a schema-1 public manifest."""
    runs = {}
    for row in manifest.get("charts", []):
        run = f"{row['model']}/{row['cycle']}"
        if not RUN_PATTERN.fullmatch(run) or row.get("url") != DRIVE_URL.format(row["file_id"]):
            raise ValueError("Manifest row cannot be represented in an R2 fragment")
        item = [int(row["hour"]), row["domain"], row["product"], row["file_id"]]
        extra = {name: row[name] for name in EXTRA_FIELDS if name in row}
        if extra:
            item.append(extra)
        runs.setdefault(run, []).append(item)
    fragments = {}
    for run, charts in runs.items():
        model, cycle = run.split("/")
        charts.sort(key=lambda item: (item[0], item[1], item[2]))
        if len({tuple(item[:3]) for item in charts}) != len(charts):
            raise ValueError("Duplicate chart identity in manifest")
        fragments[run] = {"schema": SCHEMA, "model": model, "cycle": cycle, "images": IMAGE_STORE,
                          "leads": list(policy.hours(model, cycle)), "charts": charts}
    return manifest.get("models", {}), fragments


def revision(fragment):
    return hashlib.sha256(encode(fragment)).hexdigest()[:16]


def build_index(models, revisions, counts, generated_at):
    return {"schema": SCHEMA, "pipeline_version": policy.PIPELINE_VERSION, "generated_at": generated_at,
            "images": IMAGE_STORE, "models": models,
            "runs": {run: {"rev": revisions[run], "count": counts[run]} for run in sorted(revisions)}}


class R2Publisher:
    """Upload changed fragments, then swap the index; never raises from ``publish`` in-run."""

    def __init__(self, client, *, clock=time.monotonic, wall=None, emit=None,
                 min_seconds=MIN_INDEX_SECONDS, refresh_seconds=INDEX_REFRESH_SECONDS):
        self.client, self.clock = client, clock
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.emit = emit or (lambda item: print(json.dumps(item), flush=True))
        self.min_seconds, self.refresh_seconds = min_seconds, refresh_seconds
        self.uploaded = set()
        self.published = None
        self.last_write = None
        self.loaded = False
        self.latest = None
        self.dirty = False

    def due(self):
        """Cheap check for the coordinator tick: a deferred change or the clock refresh is due."""
        if self.latest is None or self.last_write is None:
            return False
        elapsed = self.clock() - self.last_write
        return elapsed >= self.refresh_seconds or self.dirty and elapsed >= self.min_seconds

    def load(self):
        """Learn which fragments already exist from the live index (one Class B read)."""
        payload = self.client.get(INDEX_KEY)
        self.loaded = True
        if payload is None:
            return
        index = json.loads(payload)
        if index.get("schema") != SCHEMA or not isinstance(index.get("runs"), dict):
            return
        for run, item in index["runs"].items():
            if RUN_PATTERN.fullmatch(run) and isinstance(item, dict) and re.fullmatch(r"[a-f0-9]{16}", str(item.get("rev"))):
                self.uploaded.add(fragment_key(run, item["rev"]))
        self.published = {key: index.get(key) for key in ("models", "runs", "pipeline_version")}

    def publish(self, manifest, *, force=False):
        """Return a status string; ``force`` bypasses the write coalescing window."""
        now = self.clock()
        self.latest = manifest
        if not force and self.last_write is not None and now - self.last_write < self.min_seconds:
            self.dirty = True
            return "deferred"
        self.dirty = True  # cleared only once this attempt is known to be published
        if not self.loaded:
            self.load()
        models, fragments = split_manifest(manifest)
        revisions = {run: revision(fragment) for run, fragment in fragments.items()}
        counts = {run: len(fragment["charts"]) for run, fragment in fragments.items()}
        candidate = build_index(models, revisions, counts, None)
        content = {key: candidate[key] for key in ("models", "runs", "pipeline_version")}
        # A fresh process always writes once, then only on change or the periodic clock refresh.
        if (content == self.published and self.last_write is not None
                and now - self.last_write < self.refresh_seconds):
            self.dirty = False
            return "unchanged"
        uploads = 0
        for run, fragment in fragments.items():
            key = fragment_key(run, revisions[run])
            if key in self.uploaded:
                continue
            self.client.put(key, encode(fragment), cache_control=IMMUTABLE_CACHE)
            self.uploaded.add(key)
            uploads += 1
        candidate["generated_at"] = self.wall().isoformat()
        body = encode(candidate)
        self.client.put(INDEX_KEY, body, cache_control=INDEX_CACHE)
        self.published, self.last_write, self.dirty = content, now, False
        self.emit({"event": "r2_index_published", "runs": len(revisions), "fragments_uploaded": uploads,
                   "index_bytes": len(body), "r2_writes": self.client.writes, "r2_reads": self.client.reads})
        return "published"


def publisher_from_env(env=None, **kwargs):
    """Return ``(mode, publisher)``; the publisher is ``None`` when R2 is off."""
    selected = mode(env)
    return selected, (R2Publisher(client_from_env(env), **kwargs) if selected != "off" else None)


def safe_publish(publisher, manifest, *, force=False, emit=None):
    """In-run publication: failures are reported, never raised, so production continues."""
    emit = emit or (lambda item: print(json.dumps(item), flush=True))
    try:
        return publisher.publish(manifest, force=force)
    except (R2Error, ValueError, KeyError, TypeError, OSError) as error:
        emit({"event": "r2_publish_failed", "error": type(error).__name__, "message": str(error)[:200],
              **(error.details if isinstance(error, R2Error) else {})})
        return "failed"
