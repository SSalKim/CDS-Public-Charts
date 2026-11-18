"""Isolated My Drive storage for public charts; orchestration owns scheduling.

Only explicitly selected chart Drive settings are read. Images are immutable versions;
state and manifest updates use an ETag precondition. Callers must serialize the
first creation of those two JSON entries (Drive names are not unique).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import copy
from collections import deque
import time
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit
from requests import exceptions as request_errors

from vtg_drive_archive import DRIVE_API, DRIVE_UPLOAD_API, DriveClient, drive_url


NAMESPACE = "cds-public-charts-v1"
FOLDER_MIME = "application/vnd.google-apps.folder"
JSON_NAMES = frozenset({"state.json", "manifest.json"})
ENV_PREFIX = "PUBLIC_CHARTS_DRIVE_"
DRIVE_V2_API = "https://www.googleapis.com/drive/v2"
DRIVE_V2_UPLOAD_API = "https://www.googleapis.com/upload/drive/v2"
FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,parents,appProperties,trashed,"
    "ownedByMe,owners(emailAddress),driveId"
)
IMAGE_TYPES = {".webp": "image/webp", ".png": "image/png",
               ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MULTIPART_LIMIT = 5 * 1024 * 1024
KEY_PATTERN = re.compile(
    r"(?:aifs|gfs|aigfs|cmc|ecmwf|icon|ukmo)/([0-9]{10})/([0-9]{3})/"
    r"[a-z][a-z0-9_]{0,39}/(?:asia|hkor)\Z"
)


class ChartDriveError(RuntimeError):
    """A sanitized configuration, safety, integrity, or Drive API failure."""

    def __init__(self, message, *, http_status=None, transient=False, reason=None, error_type=None):
        super().__init__(message)
        self.http_status = http_status
        self.transient = transient
        self.reason = reason
        self.error_type = error_type


def _file_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ChartDriveError("Invalid Drive file or folder ID.")
    if value.casefold() == "root":
        raise ChartDriveError("My Drive root is not an allowed storage target.")
    return value


def _query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class ChartDriveStore:
    """One explicitly configured, account-owned personal Drive folder.

    Construction with an injected client performs no IO. Public operations
    verify the account/folder lazily; from_environment verifies immediately.
    JSON files remain private unless sharing was configured outside this store.
    """

    def __init__(self, client: DriveClient | None, folder_id: str, account_email: str):
        self.folder_id = _file_id(folder_id)
        if not isinstance(account_email, str) or not re.fullmatch(
            r"[^\s@]+@[^\s@]+", account_email.strip()
        ):
            raise ChartDriveError("A valid PUBLIC_CHARTS_DRIVE_ACCOUNT_EMAIL is required.")
        self.account_email = account_email.strip().casefold()
        self._client = client
        self._verified = False
        self._image_index = None
        self._upload_ids = deque()
        self.env_prefix = ENV_PREFIX

    def prime_inventory(self, inventory):
        self._image_index = {}
        for item in inventory:
            if self._managed(item) and item["appProperties"].get("kind") == "image":
                self._image_index.setdefault(item["appProperties"].get("key"), []).append(copy.deepcopy(item))

    def new_upload_worker(self):
        """Each IO thread owns its OAuth client and requests session."""
        worker = type(self).from_environment(self.env_prefix)
        if (worker.folder_id, worker.account_email) != (self.folder_id, self.account_email):
            raise ChartDriveError("Upload worker target differs from the verified coordinator.")
        worker._image_index = copy.deepcopy(self._image_index)
        return worker

    @classmethod
    def from_environment(cls, prefix=ENV_PREFIX) -> ChartDriveStore:
        """Load only the five dedicated variables, authenticate, then verify."""
        if prefix not in (ENV_PREFIX, "PUBLIC_CHARTS_DRIVE2_"):
            raise ChartDriveError("Unsupported chart Drive credentials namespace.")
        names = ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "FOLDER_ID", "ACCOUNT_EMAIL")
        settings = {name: os.environ.get(prefix + name, "").strip() for name in names}
        missing = [prefix + name for name in names if not settings[name]]
        if missing:
            raise ChartDriveError("Missing dedicated Drive settings: " + ", ".join(missing))
        # Validate targets before the client makes its initial OAuth request.
        store = cls(None, settings["FOLDER_ID"], settings["ACCOUNT_EMAIL"])
        store.env_prefix = prefix
        try:
            store._client = DriveClient({
                "mode": "oauth_refresh_token",
                "client_id": settings["CLIENT_ID"],
                "client_secret": settings["CLIENT_SECRET"],
                "refresh_token": settings["REFRESH_TOKEN"],
            })
        except (Exception, SystemExit):
            raise ChartDriveError("Dedicated Google Drive authentication failed.") from None
        store.verify_account_and_folder()
        return store

    def _request(self, method: str, url: str, **kwargs):
        # The shared client includes response bodies in errors; never expose them.
        kwargs.setdefault("timeout", (15, 60))
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            response = None
            try:
                response = self._client.request(method, url, allow_redirects=False, **kwargs)
                if not 200 <= response.status_code < 300:
                    raise ValueError("Unexpected HTTP status")
                return response
            except (Exception, SystemExit) as error:
                details = str(error)
                status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", details)
                if response is None:
                    response = getattr(error, "response", None)
                code = getattr(response, "status_code", None)
                if not isinstance(code, int):
                    code = int(status.group(1)) if status else None
                error_type = type(error).__name__
                reason = next((name for name in (
                    "storageQuotaExceeded", "userRateLimitExceeded", "rateLimitExceeded",
                    "dailyLimitExceeded", "insufficientPermissions", "backendError", "internalError",
                ) if name in details), "request_error")
                if code is None:
                    if isinstance(error, (request_errors.Timeout, TimeoutError)):
                        reason = "timeout"
                    elif isinstance(error, request_errors.SSLError):
                        reason = "tls"
                    elif isinstance(error, (request_errors.ConnectionError, ConnectionError)):
                        reason = "connection"
                    elif isinstance(error, (request_errors.ChunkedEncodingError,
                                           request_errors.ContentDecodingError, request_errors.RetryError)):
                        reason = "transport"
                transient = (code in (408, 429, 500, 502, 503, 504) or reason in
                             {"timeout", "tls", "connection", "transport", "userRateLimitExceeded",
                              "rateLimitExceeded", "backendError", "internalError"})
                if transient and attempt < attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                operation = method.upper() if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "API"
                raise ChartDriveError(f"Google Drive {operation} request failed (HTTP {code or 'unknown'}, {reason}, {error_type}).",
                                      http_status=code, transient=transient, reason=reason,
                                      error_type=error_type) from None

    @staticmethod
    def _json(response) -> dict:
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (Exception, SystemExit):
            raise ChartDriveError("Google Drive returned invalid metadata.") from None

    def _metadata(self, file_id: str):
        response = self._request(
            "GET", f"{DRIVE_API}/files/{quote(_file_id(file_id), safe='')}",
            params={"fields": FILE_FIELDS},
        )
        metadata = self._json(response)
        if metadata.get("id") != file_id:
            raise ChartDriveError("Google Drive returned an unexpected file ID.")
        return metadata, response.headers.get("ETag")

    def _mutation_metadata(self, file_id: str):
        metadata, etag = self._metadata(file_id)
        if etag:
            return metadata, etag, 3
        # Drive v3 may omit metadata ETags. The v2 snapshot exposes an ETag
        # together with fresh ownership, parent, and private-property guards.
        payload = self._json(self._request(
            "GET", f"{DRIVE_V2_API}/files/{quote(_file_id(file_id), safe='')}",
            params={"fields": "id,title,mimeType,fileSize,md5Checksum,parents(id),"
                    "properties(key,value,visibility),labels(trashed),ownedByMe,"
                    "owners(emailAddress),driveId,etag"},
        ))
        if payload.get("id") != file_id:
            raise ChartDriveError("Google Drive returned an unexpected file ID.")
        properties = payload.get("properties", [])
        parents = payload.get("parents", [])
        labels = payload.get("labels", {})
        if (not isinstance(properties, list) or not isinstance(parents, list)
                or not isinstance(labels, dict)
                or not all(isinstance(item, dict) for item in properties + parents)):
            raise ChartDriveError("Google Drive returned invalid conditional metadata.")
        private = [item for item in properties if item.get("visibility") == "PRIVATE"]
        keys = [item.get("key") for item in private]
        if not all(isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ChartDriveError("Google Drive returned invalid private properties.")
        fresh = {"id": file_id, "name": payload.get("title"), "mimeType": payload.get("mimeType"),
                 "size": payload.get("fileSize"), "md5Checksum": payload.get("md5Checksum"),
                 "parents": [item.get("id") for item in parents],
                 "appProperties": {item["key"]: item.get("value") for item in private},
                 "trashed": labels.get("trashed"), "ownedByMe": payload.get("ownedByMe"),
                 "owners": payload.get("owners"), "driveId": payload.get("driveId")}
        etag = payload.get("etag")
        if not isinstance(etag, str) or etag.startswith("W/"):
            etag = None
        return fresh, etag, 2

    def _owned(self, metadata: dict) -> bool:
        owners = metadata.get("owners")
        return (
            metadata.get("ownedByMe") is True
            and not metadata.get("driveId")
            and isinstance(owners, list)
            and len(owners) == 1
            and isinstance(owners[0], dict)
            and str(owners[0].get("emailAddress", "")).casefold() == self.account_email
        )

    def verify_account_and_folder(self) -> dict:
        """Fail closed on a wrong account, root, shared, trashed, or foreign folder."""
        self._verified = False
        about = self._json(self._request(
            "GET", f"{DRIVE_API}/about", params={"fields": "user(emailAddress)"},
        ))
        user = about.get("user")
        if not isinstance(user, dict) or str(user.get("emailAddress", "")).casefold() != self.account_email:
            raise ChartDriveError("Authenticated Drive account does not match the expected account.")
        root = self._json(self._request(
            "GET", f"{DRIVE_API}/files/root", params={"fields": "id"},
        ))
        if not root.get("id") or root["id"] == self.folder_id:
            raise ChartDriveError("My Drive root is not an allowed storage target.")
        folder, _ = self._metadata(self.folder_id)
        if (folder.get("mimeType") != FOLDER_MIME
                or folder.get("trashed") is not False or not self._owned(folder)):
            raise ChartDriveError("Chart folder must be an untrashed, account-owned My Drive folder.")
        self._verified = True
        return {"folder_id": self.folder_id, "account_email": self.account_email}

    def _ensure_verified(self) -> None:
        if not self._verified:
            self.verify_account_and_folder()

    def storage_quota(self):
        self._ensure_verified()
        value = self._json(self._request("GET", f"{DRIVE_API}/about",
                          params={"fields": "storageQuota(limit,usage,usageInDrive,usageInDriveTrash)"}))
        try:
            quota = value["storageQuota"]
            result = {name: int(quota[name]) for name in ("usage", "usageInDrive", "usageInDriveTrash")}
            result["limit"] = int(quota["limit"]) if "limit" in quota else None
            if any(number is not None and number < 0 for number in result.values()):
                raise ValueError("Negative quota")
            return result
        except (KeyError, TypeError, ValueError):
            raise ChartDriveError("Google Drive returned invalid storage quota metadata.") from None

    def _managed(self, metadata: dict) -> bool:
        properties = metadata.get("appProperties")
        mime_type = metadata.get("mimeType")
        return (
            isinstance(metadata.get("id"), str)
            and metadata["id"] != self.folder_id
            and metadata.get("parents") == [self.folder_id]
            and metadata.get("trashed") is False
            and self._owned(metadata)
            and isinstance(mime_type, str) and bool(mime_type)
            and not mime_type.startswith("application/vnd.google-apps.")
            and isinstance(properties, dict)
            and properties.get("namespace") == NAMESPACE
        )

    def _require_managed(self, metadata: dict) -> None:
        if not self._managed(metadata):
            raise ChartDriveError("File is not managed directly inside the configured chart folder.")

    def _list(self, query: str) -> list[dict]:
        files = []
        token = None
        seen = set()
        while True:
            params = {"q": query, "spaces": "drive", "pageSize": 1000,
                      "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"}
            if token:
                params["pageToken"] = token
            payload = self._json(self._request("GET", f"{DRIVE_API}/files", params=params))
            page = payload.get("files")
            if payload.get("incompleteSearch") or not isinstance(page, list):
                raise ChartDriveError("Google Drive inventory was incomplete.")
            if not all(isinstance(item, dict) for item in page):
                raise ChartDriveError("Google Drive returned invalid inventory entries.")
            files.extend(page)
            token = payload.get("nextPageToken")
            if not token:
                return files
            if not isinstance(token, str) or token in seen:
                raise ChartDriveError("Google Drive inventory pagination failed.")
            seen.add(token)

    def _base_query(self) -> str:
        return (
            f"'{_query_literal(self.folder_id)}' in parents and trashed = false and "
            f"appProperties has {{ key='namespace' and value='{NAMESPACE}' }}"
        )

    def inventory(self) -> list[dict]:
        """Return raw metadata for owned, tagged files directly under this folder."""
        self._ensure_verified()
        return [item for item in self._list(self._base_query()) if self._managed(item)]

    @staticmethod
    def _validate_key(key: str) -> None:
        match = KEY_PATTERN.fullmatch(key) if isinstance(key, str) else None
        if not match or int(match[2]) > 240:
            raise ChartDriveError("Invalid public chart key.")
        try:
            cycle = datetime.strptime(match[1], "%Y%m%d%H")
        except ValueError:
            raise ChartDriveError("Invalid public chart cycle.") from None
        if key.startswith("ecmwf/") and (cycle.hour not in (6,18) or int(match[2]) > 144):
            raise ChartDriveError("ECMWF generated charts require 06/18Z and leads through +144h.")
        if key.startswith("icon/") and (cycle.hour not in (0,6,12,18) or
                int(match[2]) > (120 if cycle.hour in (6,18) else 180)):
            raise ChartDriveError("ICON charts require a native cycle and forecast horizon.")

    @staticmethod
    def _hash_stream(handle: BinaryIO) -> tuple[int, str, str]:
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
        handle.seek(0)
        return size, md5.hexdigest(), sha256.hexdigest()

    @staticmethod
    def _matches(metadata: dict, size: int, md5: str) -> bool:
        return (str(metadata.get("size", "")) == str(size)
                and str(metadata.get("md5Checksum", "")).lower() == md5)

    def _verify_content(self, metadata: dict, size: int, md5: str, key: str) -> None:
        self._require_managed(metadata)
        if metadata["appProperties"].get("key") != key or not self._matches(metadata, size, md5):
            raise ChartDriveError("Google Drive content integrity verification failed.")

    def _permissions(self, file_id: str) -> list[dict]:
        result = []
        token = None
        seen = set()
        while True:
            params = {"fields": "nextPageToken,permissions(id,type,role,deleted)", "pageSize": 100}
            if token:
                params["pageToken"] = token
            payload = self._json(self._request(
                "GET", f"{DRIVE_API}/files/{quote(_file_id(file_id), safe='')}/permissions",
                params=params,
            ))
            page = payload.get("permissions")
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ChartDriveError("Google Drive returned invalid permissions.")
            result.extend(page)
            token = payload.get("nextPageToken")
            if not token:
                return result
            if not isinstance(token, str) or token in seen:
                raise ChartDriveError("Google Drive permission pagination failed.")
            seen.add(token)

    @staticmethod
    def _has_public_reader(permissions: list[dict]) -> bool:
        public = [item for item in permissions if item.get("type") == "anyone" and not item.get("deleted")]
        if any(item.get("role") != "reader" for item in public):
            raise ChartDriveError("Chart file has an unexpected public permission role.")
        return bool(public)

    def _ensure_public_reader(self, file_id: str) -> None:
        if self._has_public_reader(self._permissions(file_id)):
            return
        for attempt in range(3):
            try:
                self._request(
                    "POST", f"{DRIVE_API}/files/{quote(file_id, safe='')}/permissions",
                    params={"fields": "id"},
                    json={"type": "anyone", "role": "reader", "allowFileDiscovery": False},
                )
                break
            except ChartDriveError as error:
                if not error.transient:
                    raise
                if self._has_public_reader(self._permissions(file_id)):
                    return
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if not self._has_public_reader(self._permissions(file_id)):
            raise ChartDriveError("Google Drive public reader verification failed.")

    def _allocate_upload_id(self):
        if not self._upload_ids:
            payload = self._json(self._request("GET", f"{DRIVE_API}/files/generateIds",
                                params={"count": 32, "space": "drive", "type": "files"}))
            ids = payload.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 32:
                raise ChartDriveError("Google Drive returned invalid upload IDs.")
            valid = [_file_id(value) for value in ids]
            if len(set(valid)) != len(valid):
                raise ChartDriveError("Google Drive returned duplicate upload IDs.")
            self._upload_ids.extend(valid)
        return self._upload_ids.popleft()

    def _upload_image(self, handle: BinaryIO, key: str, suffix: str,
                      size: int, sha256: str) -> str:
        metadata = {"name": key.replace("/", "_") + "_" + sha256[:16] + suffix,
                    "parents": [self.folder_id], "mimeType": IMAGE_TYPES[suffix],
                    "appProperties": {"namespace": NAMESPACE, "kind": "image", "key": key, "sha256": sha256}}
        if size <= MULTIPART_LIMIT:
            file_id = self._allocate_upload_id()
            metadata["id"] = file_id
            boundary = "chart_" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                    + json.dumps(metadata).encode("utf-8")
                    + f"\r\n--{boundary}\r\nContent-Type: {IMAGE_TYPES[suffix]}\r\n\r\n".encode()
                    + handle.read() + f"\r\n--{boundary}--\r\n".encode())
            for attempt in range(3):
                try:
                    uploaded = self._json(self._request("POST", f"{DRIVE_UPLOAD_API}/files",
                                          params={"uploadType": "multipart", "fields": "id"}, data=body,
                                          timeout=(15, 45),
                                          headers={"Content-Type": f"multipart/related; boundary={boundary}"}))
                    if _file_id(uploaded.get("id")) != file_id:
                        raise ChartDriveError("Google Drive returned an unexpected upload ID.")
                    return file_id
                except ChartDriveError as error:
                    error.chart_key = key
                    # A committed upload with a lost response returns 409 on this same ID.
                    if error.http_status == 409:
                        return file_id
                    if not error.transient or attempt == 2:
                        raise
                    print(json.dumps({"event": "drive_upload_retry", "chart_key": key,
                          "storage_account": "secondary" if self.env_prefix == "PUBLIC_CHARTS_DRIVE2_" else "primary",
                          "attempt": attempt + 1, "max_attempts": 3, "retry_after_seconds": 2 ** attempt,
                          "http_status": error.http_status, "reason": error.reason,
                          "error_type": error.error_type}), flush=True)
                    time.sleep(2 ** attempt)
        response = self._request(
            "POST", f"{DRIVE_UPLOAD_API}/files",
            params={"uploadType": "resumable", "fields": "id"},
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": IMAGE_TYPES[suffix],
                     "X-Upload-Content-Length": str(size)},
            json=metadata,
        )
        location = response.headers.get("Location", "")
        try:
            target = urlsplit(location)
            valid = (target.scheme == "https" and target.hostname == "www.googleapis.com"
                     and target.port in (None, 443) and not target.username
                     and not target.password and not target.fragment
                     and target.path == "/upload/drive/v3/files")
        except ValueError:
            valid = False
        if not valid:
            raise ChartDriveError("Google Drive returned an invalid upload destination.")
        uploaded = self._json(self._request(
            "PUT", location, data=handle,
            headers={"Content-Type": IMAGE_TYPES[suffix], "Content-Length": str(size)},
        ))
        return _file_id(uploaded.get("id"))

    def upload_image(self, path: str | Path, key: str) -> dict:
        """Reuse matching MD5+size; changed bytes create a new immutable file ID."""
        self._validate_key(key)
        try:
            local_path = Path(path)
            suffix = local_path.suffix.lower()
            if suffix not in IMAGE_TYPES:
                raise ChartDriveError("Unsupported chart image type.")
            handle = local_path.open("rb")
        except (OSError, TypeError, ValueError):
            raise ChartDriveError("Chart image could not be opened.") from None
        with handle:
            try:
                size, md5, sha256 = self._hash_stream(handle)
            except OSError:
                raise ChartDriveError("Chart image could not be read.") from None
            if not size:
                raise ChartDriveError("Chart image must not be empty.")
            self._ensure_verified()
            query = self._base_query() + (
                f" and appProperties has {{ key='key' and value='{_query_literal(key)}' }}"
            )
            candidates = self._list(query) if self._image_index is None else self._image_index.get(key, [])
            file_id = None
            for candidate in sorted(candidates, key=lambda item: str(item.get("id", ""))):
                if (not self._managed(candidate) or candidate["appProperties"].get("key") != key
                        or candidate.get("mimeType") != IMAGE_TYPES[suffix]
                        or not self._matches(candidate, size, md5)):
                    continue
                current, _ = self._metadata(_file_id(candidate["id"]))
                self._require_managed(current)
                if (current.get("mimeType") == IMAGE_TYPES[suffix]
                        and current["appProperties"].get("key") == key
                        and self._matches(current, size, md5)):
                    file_id = current["id"]
                    break
            if file_id is None:
                file_id = self._upload_image(handle, key, suffix, size, sha256)
                current, _ = self._metadata(file_id)
                self._verify_content(current, size, md5, key)
                if current.get("mimeType") != IMAGE_TYPES[suffix]:
                    raise ChartDriveError("Google Drive returned an unexpected image type.")
            self._ensure_public_reader(file_id)
            if self._image_index is not None:
                self._image_index.setdefault(key, []).append(copy.deepcopy(current))
            return {"file_id": file_id, "url": drive_url(file_id), "size": size,
                    "md5": md5, "sha256": sha256, "key": key}

    @staticmethod
    def _json_name(name: str) -> None:
        if not isinstance(name, str) or name not in JSON_NAMES:
            raise ChartDriveError("Only state.json and manifest.json are managed JSON names.")

    def _json_entry(self, name: str) -> dict | None:
        self._ensure_verified()
        query = self._base_query() + f" and name = '{_query_literal(name)}'"
        entries = [item for item in self._list(query)
                   if self._managed(item) and item.get("name") == name]
        if len(entries) > 1:
            raise ChartDriveError("Duplicate managed JSON entries require reconciliation.")
        if not entries:
            return None
        self._require_json_entry(entries[0], name)
        return entries[0]

    def _require_json_entry(self, entry: dict, name: str) -> None:
        self._require_managed(entry)
        if (entry.get("name") != name or entry.get("mimeType") != "application/json"
                or entry["appProperties"].get("key") != name
                or entry["appProperties"].get("kind") != "json"):
            raise ChartDriveError("Managed JSON entry has unexpected metadata.")

    def read_json(self, name: str, default: Any = None) -> Any:
        """Return default only when absent, never on an API or integrity error."""
        self._json_name(name)
        entry = self._json_entry(name)
        if entry is None:
            return default
        current, etag = self._metadata(_file_id(entry["id"]))
        self._require_json_entry(current, name)
        response = self._request(
            "GET", f"{DRIVE_API}/files/{quote(current['id'], safe='')}",
            params={"alt": "media"}, headers={"If-Match": etag} if etag else {},
        )
        try:
            data = response.content
            md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
            self._verify_content(current, len(data), md5, name)
            return json.loads(data.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError):
            raise ChartDriveError("Managed JSON content is invalid.") from None

    def write_json(self, name: str, payload: Any) -> dict:
        """Create/update a private managed JSON blob; reject stale ETag updates."""
        self._json_name(name)
        try:
            data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
                               separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ChartDriveError("Payload is not valid JSON.") from None
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        entry = self._json_entry(name)
        metadata = {"name": name, "mimeType": "application/json", "parents": [self.folder_id],
                    "appProperties": {"namespace": NAMESPACE, "key": name, "kind": "json"}}
        method, url, headers = "POST", f"{DRIVE_UPLOAD_API}/files", {}
        params = {"uploadType": "multipart", "fields": "id"}
        if entry is not None:
            current, etag, api_version = self._mutation_metadata(_file_id(entry["id"]))
            self._require_json_entry(current, name)
            if self._matches(current, len(data), md5):
                return self._json_record(current["id"], name, data, md5)
            if not etag:
                raise ChartDriveError("Managed JSON update requires an ETag precondition.")
            method, url = "PATCH", f"{DRIVE_UPLOAD_API}/files/{quote(current['id'], safe='')}"
            if api_version == 2:
                method, url = "PUT", f"{DRIVE_V2_UPLOAD_API}/files/{quote(current['id'], safe='')}"
                params["newRevision"] = "false"
            headers["If-Match"] = etag
            metadata = {"mimeType": "application/json"}
        boundary = "chart_" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata).encode("utf-8")
            + f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode()
            + data + f"\r\n--{boundary}--\r\n".encode()
        )
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        result = self._json(self._request(
            method, url, params=params,
            headers=headers, data=body,
        ))
        file_id = _file_id(result.get("id"))
        if entry is not None and file_id != entry["id"]:
            raise ChartDriveError("Google Drive returned an unexpected JSON file ID.")
        verified, _ = self._metadata(file_id)
        self._require_json_entry(verified, name)
        self._verify_content(verified, len(data), md5, name)
        return self._json_record(file_id, name, data, md5)

    @staticmethod
    def _json_record(file_id: str, name: str, data: bytes, md5: str) -> dict:
        return {"file_id": file_id, "name": name, "size": len(data), "md5": md5,
                "sha256": hashlib.sha256(data).hexdigest()}

    def delete_managed(self, file_id: str) -> None:
        """Permanently delete exactly one freshly verified managed blob ID."""
        clean_id = _file_id(file_id)
        if clean_id == self.folder_id:
            raise ChartDriveError("The chart folder cannot be deleted.")
        self._ensure_verified()
        current, etag, api_version = self._mutation_metadata(clean_id)
        self._require_managed(current)
        if not etag:
            raise ChartDriveError("Managed deletion requires an ETag precondition.")
        self._request(
            "DELETE", f"{DRIVE_V2_API if api_version == 2 else DRIVE_API}/files/{quote(clean_id, safe='')}",
            headers={"If-Match": etag},
        )
