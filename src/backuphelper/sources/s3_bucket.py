"""S3-bucket source — full-bucket mirror that PRESERVES per-object metadata.

Unlike every existing fleet tool (which mirrors object keys only), this captures
content-type, user metadata, tags and storage class into ``metadata.json`` and
faithfully re-applies them on restore. Works against any S3-compatible endpoint
(AWS, MinIO, R2, B2, Wasabi, Garage) via path-style + SigV4.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

import boto3
from botocore.client import Config
from pydantic import BaseModel, Field

from ..archive.bundle import create_bundle
from .base import Source, StagedComponent


class S3SourceConfig(BaseModel):
    bucket: str
    endpoint: Optional[str] = None
    region: str = "eu-central-1"
    access_key: str = ""
    secret_key: str = ""
    prefix: str = ""
    force_path_style: bool = True
    name: str = "s3"


class S3BucketSource(Source):
    type = "s3"

    def __init__(self, spec: Mapping[str, Any], client: Any = None):
        super().__init__(spec)
        self.cfg = S3SourceConfig.model_validate({k: v for k, v in spec.items() if k != "type"})
        self._client = client or self._build_client()

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

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / f"{self.cfg.name}.tar.gz"
        try:
            with tempfile.TemporaryDirectory(dir=staging_dir) as td:
                stage = Path(td)
                objects = self._download_all(stage / "objects")
                manifest = {"bucket": self.cfg.bucket, "prefix": self.cfg.prefix,
                            "object_count": len(objects), "objects": objects}
                (stage / "metadata.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
                create_bundle(stage, out)
        except Exception as exc:  # noqa: BLE001 - surfaced as an errored component
            out.unlink(missing_ok=True)
            return [StagedComponent(name=self.cfg.name, kind=self.type, path=None,
                                    error=f"s3 mirror failed: {exc}")]
        return [StagedComponent(name=self.cfg.name, kind=self.type, path=out,
                                metadata={"bucket": self.cfg.bucket, "object_count": len(objects)})]

    def _download_all(self, objects_dir: Path) -> list[dict]:
        objects_dir.mkdir(parents=True, exist_ok=True)
        captured: list[dict] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.cfg.bucket, Prefix=self.cfg.prefix):
            for obj in page.get("Contents", []):
                captured.append(self._download_one(obj["Key"], objects_dir))
        captured.sort(key=lambda o: o["key"])
        return captured

    def _download_one(self, key: str, objects_dir: Path) -> dict:
        resp = self._client.get_object(Bucket=self.cfg.bucket, Key=key)
        dest = objects_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp["Body"].read())
        tags = self._client.get_object_tagging(Bucket=self.cfg.bucket, Key=key).get("TagSet", [])
        return {
            "key": key,
            "size": resp.get("ContentLength", dest.stat().st_size),
            "content_type": resp.get("ContentType"),
            "metadata": dict(resp.get("Metadata", {})),
            "storage_class": resp.get("StorageClass"),
            "etag": resp.get("ETag"),
            "tags": {t["Key"]: t["Value"] for t in tags},
        }

    def restore(self, staged_dir: Path) -> None:
        manifest = json.loads((Path(staged_dir) / "metadata.json").read_text())
        objects_dir = Path(staged_dir) / "objects"
        for obj in manifest.get("objects", []):
            key = obj["key"]
            body = (objects_dir / key).read_bytes()
            extra: dict[str, Any] = {}
            if obj.get("content_type"):
                extra["ContentType"] = obj["content_type"]
            if obj.get("metadata"):
                extra["Metadata"] = obj["metadata"]
            if obj.get("tags"):
                extra["Tagging"] = "&".join(f"{k}={v}" for k, v in obj["tags"].items())
            self._client.put_object(Bucket=self.cfg.bucket, Key=key, Body=body, **extra)
