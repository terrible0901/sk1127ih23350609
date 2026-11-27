"""Small-board JSON storage. Every mutation is an atomic compare-and-swap.

The private blob is the source of truth; no database is stored on App Service's
temporary disk. A single document keeps registration, numbering and post limits
atomic. This is deliberately intended for a small pilot, not an unbounded board.
"""

import copy
import json
import os
import random
import threading
import time
from pathlib import Path


MAX_STATE_BYTES = 12 * 1024 * 1024


class StorageUnavailable(Exception):
    pass


class StorageConflict(StorageUnavailable):
    pass


def empty_state():
    return {"version": 1, "users": {}, "threads": {}, "last_posts": {}}


def encode(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise StorageUnavailable("Storage capacity reached")
    return raw


class MemoryStore:
    """Also useful for deterministic tests; never chosen by production defaults."""

    def __init__(self):
        self._state = empty_state()
        self._lock = threading.Lock()

    def read(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def mutate(self, operation):
        with self._lock:
            state = copy.deepcopy(self._state)
            result = operation(state)
            encode(state)
            self._state = state
            return result


class LocalStore:
    """Single-process local development only, with atomic file replacement."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return empty_state()
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageUnavailable("Cannot read local storage") from exc

    def read(self):
        with self._lock:
            return self._read()

    def mutate(self, operation):
        with self._lock:
            state = self._read()
            result = operation(state)
            raw = encode(state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(raw)
            os.replace(temporary, self.path)
            return result


class AzureBlobStore:
    def __init__(self, connection_string, account_url, container, blob_name):
        from azure.storage.blob import BlobServiceClient, ContentSettings

        if connection_string:
            service = BlobServiceClient.from_connection_string(
                connection_string, connection_timeout=5, read_timeout=10, retry_total=1
            )
        elif account_url:
            from azure.identity import DefaultAzureCredential

            service = BlobServiceClient(
                account_url, credential=DefaultAzureCredential(),
                connection_timeout=5, read_timeout=10, retry_total=1,
            )
        else:
            raise RuntimeError("Azure Blob接続設定がありません。READMEの環境変数を設定してください。")
        # A dedicated private container is created lazily if it doesn't exist.
        self.blob = service.get_blob_client(container, blob_name)
        self.container = service.get_container_client(container)
        self._container_ready = False
        self._container_lock = threading.Lock()
        self.content_settings = ContentSettings(content_type="application/json; charset=utf-8")

    def _ensure_container(self):
        from azure.core.exceptions import AzureError, ResourceExistsError

        if not hasattr(self, "container"):
            return
        with self._container_lock:
            if self._container_ready:
                return
            try:
                try:
                    self.container.create_container()
                except ResourceExistsError:
                    pass
                if self.container.get_container_properties().get("public_access"):
                    raise StorageUnavailable("Board container must be private")
                self._container_ready = True
            except AzureError as exc:
                raise StorageUnavailable("Cannot prepare private container") from exc

    def _load(self):
        from azure.core.exceptions import AzureError, ResourceNotFoundError

        self._ensure_container()
        try:
            download = self.blob.download_blob()
            if download.size > MAX_STATE_BYTES:
                raise StorageUnavailable("Storage capacity reached")
            # ETag belongs to this exact download, not a separate properties read.
            return json.loads(download.readall()), download.properties.etag
        except ResourceNotFoundError as exc:
            # A missing container is configuration failure, not an empty board.
            if exc.error_code == "BlobNotFound":
                return empty_state(), None
            raise StorageUnavailable("Storage container not found") from exc
        except (AzureError, ValueError) as exc:
            raise StorageUnavailable("Cannot read Azure storage") from exc

    def read(self):
        return self._load()[0]

    def mutate(self, operation):
        from azure.core import MatchConditions
        from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, HttpResponseError

        for attempt in range(8):
            state, etag = self._load()
            # Operation must have no side effects: it can be called again on conflict.
            result = operation(state)
            raw = encode(state)
            try:
                options = {"content_settings": self.content_settings}
                if etag is None:
                    self.blob.upload_blob(raw, overwrite=False, **options)
                else:
                    self.blob.upload_blob(
                        raw, overwrite=True, etag=etag,
                        match_condition=MatchConditions.IfNotModified, **options,
                    )
                return result
            except (ResourceExistsError, ResourceModifiedError):
                pass
            except HttpResponseError as exc:
                if exc.status_code not in (409, 412):
                    raise StorageUnavailable("Cannot write Azure storage") from exc
            except AzureError as exc:
                raise StorageUnavailable("Cannot write Azure storage") from exc
            time.sleep(min(0.025 * 2**attempt, 0.4) + random.uniform(0, 0.025))
        raise StorageConflict("Concurrent updates; please retry")


def make_store(config):
    backend = config["STORAGE_BACKEND"]
    if backend == "memory" and config.get("TESTING"):
        return MemoryStore()
    if backend == "local":
        if os.environ.get("WEBSITE_SITE_NAME"):
            raise RuntimeError("AzureではSTORAGE_BACKEND=azureを指定してください。")
        return LocalStore(config["LOCAL_STORAGE_PATH"])
    if backend == "azure":
        return AzureBlobStore(
            config["AZURE_STORAGE_CONNECTION_STRING"], config["AZURE_STORAGE_ACCOUNT_URL"],
            config["AZURE_STORAGE_CONTAINER"], config["AZURE_STORAGE_BLOB"],
        )
    raise RuntimeError("STORAGE_BACKEND must be azure or local")

