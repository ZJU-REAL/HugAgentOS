"""Object storage side of a synchronized space: one grant prefix in an S3 bucket.

Writes carry the ETag the sandbox copy was based on (``If-Match`` /
``If-None-Match``), so another writer's change is detected by the store
instead of being overwritten. Older S3-compatible stores (MinIO before 2024)
silently ignore those headers; a one-time probe detects that and the write
then re-checks the ETag immediately before storing, which narrows but does
not close the race. Deletes always use that re-check.
"""
from __future__ import annotations

import os
import threading
import uuid

from fastapi import HTTPException

from core.files.spaces.access import identifier
from core.files.spaces.transport.sync.manifest import Remote
from core.files.storage.entries import validate_key
from core.files.storage.s3_directory import is_not_found, is_precondition_failed

_CONDITIONAL: dict[tuple, bool] = {}
_CONDITIONAL_LOCK = threading.Lock()


class PreconditionFailed(Exception):
    """The object changed since the sandbox copy was taken."""


class RemoteSpace:
    def __init__(self, storage, prefix: str):
        if not hasattr(storage, "s3_client"):
            raise RuntimeError("HUGAGENT_SPACE_MOUNT_MODE=sync requires S3-compatible storage")
        self.storage = storage
        self.client = storage.s3_client
        self.bucket = storage.bucket
        self.prefix = validate_key(prefix, directory=True)

    def key(self, rel: str, *, directory=False) -> str:
        return self.prefix + validate_key(rel, directory=directory)

    def listing(self, *, limit: int) -> tuple[dict[str, Remote], set[str]]:
        """Every object under the prefix in one flat pass (1000 keys per request).

        Directories are the marker objects plus the parents of every key, which
        is what a delimited, per-directory walk would report."""
        files, dirs, count = {}, set(), 0
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix)
        for page in pages:
            for item in page.get("Contents", []):
                rel = item["Key"][len(self.prefix):]
                if not rel:
                    continue
                count += 1
                if count > limit:
                    raise HTTPException(413, "Directory exceeds operation limit")
                parts = rel.rstrip("/").split("/")
                dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
                if rel.endswith("/"):
                    dirs.add(rel.rstrip("/"))
                else:
                    validate_key(rel)
                    modified = item["LastModified"].timestamp() if item.get("LastModified") else 0.0
                    files[rel] = Remote(item.get("ETag") or "", int(item.get("Size") or 0), modified)
        return files, dirs

    def download(self, rel: str, stream) -> str | None:
        """Copy the object into ``stream``; return the ETag of exactly that content,
        or None when it was deleted meanwhile (the next sync removes it)."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(rel))
        except self.storage.ClientError as exc:
            if is_not_found(exc):
                return None
            raise
        body = response["Body"]
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                stream.write(chunk)
        finally:
            body.close()
        return response["ETag"]

    def conditional_writes(self) -> bool:
        identity = (getattr(self.client.meta, "endpoint_url", ""), self.bucket)
        with _CONDITIONAL_LOCK:
            if identity not in _CONDITIONAL:
                _CONDITIONAL[identity] = self._probe()
            return _CONDITIONAL[identity]

    def _probe(self) -> bool:
        environment = identifier(os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")))
        key = f"{environment}/.hugagent-sync-probe/{uuid.uuid4().hex}"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=b"probe")
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=b"probe", IfNoneMatch="*")
            return False
        except self.storage.ClientError as exc:
            if is_precondition_failed(exc):
                return True
            raise
        finally:
            self.client.delete_object(Bucket=self.bucket, Key=key)

    def upload(self, rel: str, stream, expected: str | None) -> str:
        condition = {"IfNoneMatch": "*"} if expected is None else {"IfMatch": expected}
        if not self.conditional_writes():
            current = self.storage.stat_object(self.key(rel))
            if (current.etag if current else None) != expected:
                raise PreconditionFailed(rel)
            condition = {}
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=self.key(rel), Body=stream,
                                              **condition)
        except self.storage.ClientError as exc:
            if is_precondition_failed(exc):
                raise PreconditionFailed(rel) from exc
            raise
        return response["ETag"]

    def delete(self, rel: str, expected: str) -> None:
        current = self.storage.stat_object(self.key(rel))
        if current is None:
            return
        if current.etag != expected:
            raise PreconditionFailed(rel)
        self.client.delete_object(Bucket=self.bucket, Key=self.key(rel))

    def make_directory(self, rel: str) -> None:
        self.storage.make_directory(self.key(rel, directory=True))

    def remove_directory(self, rel: str) -> bool:
        try:
            self.storage.remove_directory(self.key(rel, directory=True))
        except OSError:
            return False
        return True
