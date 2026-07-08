"""S3 destination — any S3-compatible endpoint via path-style + SigV4.

The upload path is deliberately hand-rolled rather than delegated to boto3's
``upload_file``/TransferManager: backups routinely target MinIO and Ceph/RGW,
which are strict about multipart semantics. We split large files into EQUAL
``multipart_chunk_size`` parts (only the final part is shorter) so every part is
uniform, then verify the completed object's ``ContentLength`` against the local
file size. Small files (< ``multipart_threshold``) take a single ``put_object``.

Network calls are wrapped in :func:`backuphelper.net.retry.call_with_retry` so
transient errors retry with backoff; keys are transparently prefixed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel

from ..net.retry import call_with_retry
from .base import Destination

logger = logging.getLogger(__name__)


class S3DestinationConfig(BaseModel):
    """Validated configuration for an :class:`S3Destination`."""

    bucket: str
    endpoint: str | None = None
    region: str = "eu-central-1"
    access_key: str = ""
    secret_key: str = ""
    prefix: str = ""
    force_path_style: bool = True
    multipart_threshold: int = 100 * 1024 * 1024
    multipart_chunk_size: int = 50 * 1024 * 1024
    ensure_bucket: bool = True


class S3Destination(Destination):
    """A :class:`Destination` backed by an S3 (or S3-compatible) bucket."""

    def __init__(self, cfg: Mapping[str, Any], client: Any = None) -> None:
        self.cfg = S3DestinationConfig.model_validate(
            {k: v for k, v in cfg.items() if k != "type"}
        )
        self._client = client or self._build_client()
        if self.cfg.ensure_bucket:
            self._ensure_bucket()

    def _build_client(self) -> Any:
        style = "path" if self.cfg.force_path_style else "auto"
        return boto3.client(
            "s3",
            endpoint_url=self.cfg.endpoint or None,
            aws_access_key_id=self.cfg.access_key or None,
            aws_secret_access_key=self.cfg.secret_key or None,
            region_name=self.cfg.region,
            config=Config(s3={"addressing_style": style}, signature_version="s3v4"),
        )

    def _full_key(self, key: str) -> str:
        return f"{self.cfg.prefix}{key}"

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.cfg.bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                raise
            self._create_bucket()

    def _create_bucket(self) -> None:
        params: dict[str, Any] = {"Bucket": self.cfg.bucket}
        if self.cfg.region and self.cfg.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.cfg.region}
        self._client.create_bucket(**params)
        logger.info("created bucket %s", self.cfg.bucket)

    def put(self, local_path: Path, key: str) -> None:
        local_path = Path(local_path)
        size = local_path.stat().st_size
        full_key = self._full_key(key)
        if size < self.cfg.multipart_threshold:
            self._put_single(local_path, full_key)
        else:
            self._put_multipart(local_path, full_key, size)
        logger.debug("uploaded key %s (%d bytes)", key, size)

    def _put_single(self, local_path: Path, full_key: str) -> None:
        body = local_path.read_bytes()
        call_with_retry(
            lambda: self._client.put_object(
                Bucket=self.cfg.bucket, Key=full_key, Body=body
            )
        )

    def _put_multipart(self, local_path: Path, full_key: str, size: int) -> None:
        upload = call_with_retry(
            lambda: self._client.create_multipart_upload(
                Bucket=self.cfg.bucket, Key=full_key
            )
        )
        upload_id = upload["UploadId"]
        parts: list[dict[str, Any]] = []
        try:
            with local_path.open("rb") as fh:
                part_number = 1
                while True:
                    chunk = fh.read(self.cfg.multipart_chunk_size)
                    if not chunk:
                        break
                    resp = call_with_retry(
                        lambda c=chunk, n=part_number: self._client.upload_part(
                            Bucket=self.cfg.bucket,
                            Key=full_key,
                            PartNumber=n,
                            UploadId=upload_id,
                            Body=c,
                        )
                    )
                    parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
                    part_number += 1
            call_with_retry(
                lambda: self._client.complete_multipart_upload(
                    Bucket=self.cfg.bucket,
                    Key=full_key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            )
        except Exception:
            try:
                self._client.abort_multipart_upload(
                    Bucket=self.cfg.bucket, Key=full_key, UploadId=upload_id
                )
            except Exception:  # noqa: BLE001 - abort is best-effort cleanup
                logger.exception("failed to abort multipart upload for %s", full_key)
            raise

        head = call_with_retry(
            lambda: self._client.head_object(Bucket=self.cfg.bucket, Key=full_key)
        )
        remote_size = head.get("ContentLength")
        if remote_size != size:
            raise RuntimeError(
                f"multipart size mismatch for {full_key}: "
                f"remote {remote_size} != local {size}"
            )
        logger.debug("multipart upload of %s verified (%d parts)", full_key, len(parts))

    def get(self, key: str, dest: Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        full_key = self._full_key(key)
        # Stream to disk (chunked TransferManager) instead of buffering the whole
        # object in memory — a multi-GB snapshot would otherwise OOM the container.
        call_with_retry(
            lambda: self._client.download_file(self.cfg.bucket, full_key, str(dest))
        )

    def list_keys(self, prefix: str = "") -> list[str]:
        search = self._full_key(prefix)

        def _collect() -> list[str]:
            keys: list[str] = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.cfg.bucket, Prefix=search):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"][len(self.cfg.prefix):])
            return keys

        return sorted(call_with_retry(_collect))

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.cfg.bucket, Key=self._full_key(key))
        logger.debug("deleted key %s", key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.cfg.bucket, Key=self._full_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise
        return True
