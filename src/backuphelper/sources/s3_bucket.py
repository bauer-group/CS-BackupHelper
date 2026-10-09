"""S3-bucket source — full-bucket mirror that PRESERVES per-object metadata.

Unlike every existing fleet tool (which mirrors object keys only), this captures
content-type, the other content headers (Content-Disposition, Cache-Control,
Content-Encoding, Content-Language), user metadata, tags and storage class into
``metadata.json`` and faithfully re-applies them on restore. Works against any
S3-compatible endpoint (AWS, MinIO, R2, B2, Wasabi, Garage) via path-style +
SigV4.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from pydantic import Field

from ..archive.bundle import create_bundle
from ..config.models import ConfigModel
from .base import Source, StagedComponent

# Object headers a client acts on — Outline, for one, stores attachments of unsafe
# types with "Content-Disposition: attachment" so a browser downloads them instead
# of rendering them. Captured on backup and re-applied on restore:
# metadata.json key -> get_object response field / put_object parameter.
_CONTENT_HEADERS = {
    "content_type": "ContentType",
    "content_disposition": "ContentDisposition",
    "cache_control": "CacheControl",
    "content_encoding": "ContentEncoding",
    "content_language": "ContentLanguage",
}

# get_object_tagging answers that the tags cannot be read (no s3:GetObjectTagging
# permission) or that the store has no object tagging at all. The object itself
# was readable, so it is kept; only its tags are missing.
_TAGGING_UNAVAILABLE_CODES = frozenset({
    "AccessDenied", "NotImplemented", "MethodNotAllowed", "NotSupported",
    "UnsupportedOperation", "XNotImplemented",
})
_TAGGING_UNAVAILABLE_STATUS = frozenset({403, 405, 501})


class S3SourceConfig(ConfigModel):
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

    @property
    def component_name(self) -> str:
        return self.cfg.name

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / f"{self.cfg.name}.tar.gz"
        try:
            with tempfile.TemporaryDirectory(dir=staging_dir) as td:
                stage = Path(td)
                untagged: Counter[str] = Counter()
                objects = self._download_all(stage / "objects", untagged)
                manifest = {"bucket": self.cfg.bucket, "prefix": self.cfg.prefix,
                            "object_count": len(objects), "objects": objects}
                (stage / "metadata.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
                create_bundle(stage, out)
        except Exception as exc:  # noqa: BLE001 - surfaced as an errored component
            out.unlink(missing_ok=True)
            return [StagedComponent(name=self.cfg.name, kind=self.type, path=None,
                                    error=f"s3 mirror failed: {exc}")]
        metadata: dict[str, Any] = {"bucket": self.cfg.bucket, "object_count": len(objects)}
        if untagged:
            metadata["warnings"] = [_untagged_warning(untagged, len(objects))]
        return [StagedComponent(name=self.cfg.name, kind=self.type, path=out, metadata=metadata)]

    def _download_all(self, objects_dir: Path, untagged: Counter[str]) -> list[dict]:
        objects_dir.mkdir(parents=True, exist_ok=True)
        captured: list[dict] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.cfg.bucket, Prefix=self.cfg.prefix):
            for obj in page.get("Contents", []):
                captured.append(self._download_one(obj["Key"], objects_dir, untagged))
        captured.sort(key=lambda o: o["key"])
        return captured

    def _download_one(self, key: str, objects_dir: Path, untagged: Counter[str]) -> dict:
        resp = self._client.get_object(Bucket=self.cfg.bucket, Key=key)
        dest = objects_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp["Body"].read())
        entry = {
            "key": key,
            "size": resp.get("ContentLength", dest.stat().st_size),
            "metadata": dict(resp.get("Metadata", {})),
            "storage_class": resp.get("StorageClass"),
            "etag": resp.get("ETag"),
            "tags": self._tags(key, untagged),
        }
        for field, header in _CONTENT_HEADERS.items():
            entry[field] = resp.get(header)
        return entry

    def _tags(self, key: str, untagged: Counter[str]) -> Optional[dict[str, str]]:
        """The object's tags, or None (recorded as null) when the store refuses
        to hand them out; ``untagged`` counts those refusals by error code."""
        try:
            resp = self._client.get_object_tagging(Bucket=self.cfg.bucket, Key=key)
        except ClientError as exc:
            code = _tagging_unavailable(exc)
            if code is None:
                raise
            untagged[code] += 1
            return None
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}

    def restore(self, staged_dir: Path) -> None:
        manifest = json.loads((Path(staged_dir) / "metadata.json").read_text())
        objects_dir = Path(staged_dir) / "objects"
        for obj in manifest.get("objects", []):
            key = obj["key"]
            body = (objects_dir / key).read_bytes()
            extra: dict[str, Any] = {}
            # A snapshot taken before a header was captured simply lacks its key.
            for field, header in _CONTENT_HEADERS.items():
                if obj.get(field):
                    extra[header] = obj[field]
            if obj.get("metadata"):
                extra["Metadata"] = obj["metadata"]
            if obj.get("tags"):
                extra["Tagging"] = "&".join(f"{k}={v}" for k, v in obj["tags"].items())
            self._client.put_object(Bucket=self.cfg.bucket, Key=key, Body=body, **extra)


def _tagging_unavailable(exc: ClientError) -> Optional[str]:
    """The error code when ``exc`` means "tags not readable here", else None."""
    error = exc.response.get("Error", {})
    code = str(error.get("Code") or "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code in _TAGGING_UNAVAILABLE_CODES or status in _TAGGING_UNAVAILABLE_STATUS:
        return code or f"HTTP {status}"
    return None


def _untagged_warning(untagged: Counter[str], total: int) -> str:
    codes = ", ".join(sorted(untagged))
    return (f"tags of {sum(untagged.values())} of {total} objects "
            f"(get_object_tagging: {codes}); these objects restore without tags")
