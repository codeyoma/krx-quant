"""Object Store: permanent storage for recorded days, models and training reports.

Two implementations share one interface: a local directory, for development and
tests, and S3. Keys are ``/``-separated paths such as
``raw/date=2026-10-08/feed=H0UNCNT0/symbol=005930/data.parquet``. Every object carries
the SHA-256 of its content, so a caller can tell whether a file is already stored.

Only models are ever deleted (#361, ``delete``): on S3 the engine's IAM user may delete
``engine/models/*`` and nothing else (``deploy/iam-policy.json``), and with the bucket's
versioning a delete leaves the object a noncurrent version for 30 days.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import boto3

from krx_quant.config import Settings


@dataclass(frozen=True)
class Stored:
    """An object as listed: its key, its size in bytes and when it was stored."""

    key: str
    size: int
    modified: datetime  # aware


class ObjectStore(Protocol):
    @property
    def name(self) -> str:
        """Where the objects go, e.g. ``s3://bucket/prefix``; uploads are remembered per name."""
        ...

    def digest(self, key: str) -> str | None:
        """SHA-256 (hex) of the stored object, or None if there is none."""
        ...

    def put(self, key: str, source: Path, digest: str) -> None:
        """Store ``source`` under ``key``, replacing any object there."""
        ...

    def get(self, key: str, target: Path) -> None: ...

    def keys(self, prefix: str = "") -> list[str]:
        """Every key starting with ``prefix``, sorted."""
        ...

    def objects(self, prefix: str = "") -> list[Stored]:
        """Every object whose key starts with ``prefix``, sorted by key, with its size and
        when it was stored."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object under ``key``; none there: nothing happens."""
        ...


def put_json(store: ObjectStore, key: str, data: Any) -> None:
    """Store ``data`` as a JSON object."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "object.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        store.put(key, path, file_digest(path))


def get_json(store: ObjectStore, key: str) -> Any:
    """The JSON object stored under ``key``, or None if there is none."""
    if store.digest(key) is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "object.json"
        store.get(key, path)
        return json.loads(path.read_text())


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def name(self) -> str:
        return str(self._root.resolve())

    def digest(self, key: str) -> str | None:
        path = self._root / key
        return file_digest(path) if path.is_file() else None

    def put(self, key: str, source: Path, digest: str) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(path)

    def get(self, key: str, target: Path) -> None:
        shutil.copyfile(self._root / key, target)

    def keys(self, prefix: str = "") -> list[str]:
        return [o.key for o in self.objects(prefix)]

    def objects(self, prefix: str = "") -> list[Stored]:
        if not self._root.exists():
            return []
        found = []
        for p in self._root.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                key = p.relative_to(self._root).as_posix()
                if key.startswith(prefix):
                    stat = p.stat()
                    found.append(Stored(key, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC)))
        return sorted(found, key=lambda o: o.key)

    def delete(self, key: str) -> None:
        (self._root / key).unlink(missing_ok=True)


class S3ObjectStore:
    """Objects under ``prefix`` in ``bucket``; the digest is kept as object metadata."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    @property
    def name(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    def digest(self, key: str) -> str | None:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._prefix + key)
        except self._client.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        value = head.get("Metadata", {}).get("sha256")
        return str(value) if value else None

    def put(self, key: str, source: Path, digest: str) -> None:
        self._client.upload_file(
            str(source), self._bucket, self._prefix + key, ExtraArgs={"Metadata": {"sha256": digest}}
        )

    def get(self, key: str, target: Path) -> None:
        self._client.download_file(self._bucket, self._prefix + key, str(target))

    def keys(self, prefix: str = "") -> list[str]:
        return [o.key for o in self.objects(prefix)]

    def objects(self, prefix: str = "") -> list[Stored]:
        found: list[Stored] = []
        for page in self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=self._prefix + prefix
        ):
            found += [
                Stored(obj["Key"][len(self._prefix) :], int(obj["Size"]), obj["LastModified"])
                for obj in page.get("Contents", [])
            ]
        return sorted(found, key=lambda o: o.key)

    def delete(self, key: str) -> None:
        """A delete marker where the bucket keeps versions (the server's does, 30 days)."""
        self._client.delete_object(Bucket=self._bucket, Key=self._prefix + key)


def open_store(settings: Settings) -> ObjectStore | None:
    """The store ``KRX_OBJECT_STORE`` names: ``s3://bucket/prefix`` or a local directory."""
    target = settings.object_store
    if not target:
        return None
    if target.startswith("s3://"):
        bucket, _, prefix = target.removeprefix("s3://").partition("/")
        client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        return S3ObjectStore(client, bucket, prefix)
    return LocalObjectStore(Path(target))
