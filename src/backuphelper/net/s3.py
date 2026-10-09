"""S3 client construction shared by the s3 source and the s3 destination.

Both talk to any S3-compatible endpoint (AWS, MinIO, Ceph/RGW, R2, B2, Wasabi,
Garage) through path-style addressing + SigV4, with the same connection
settings — including how the endpoint's TLS certificate is verified.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Union

import boto3
from botocore.client import Config

from ..config.models import ConfigModel

logger = logging.getLogger(__name__)


class S3ConnectionConfig(ConfigModel):
    """The connection fields every S3 source and destination shares."""

    endpoint: Optional[str] = None
    region: str = "eu-central-1"
    access_key: str = ""
    secret_key: str = ""
    force_path_style: bool = True
    # TLS certificate verification of the endpoint. On by default; ``ca_bundle``
    # points at a PEM file of the CA(s) to trust instead of the default bundle
    # (an internal CA), ``verify_tls: false`` switches verification off.
    verify_tls: bool = True
    ca_bundle: Optional[str] = None


def tls_verify(cfg: S3ConnectionConfig) -> Union[bool, str, None]:
    """The ``verify`` argument for boto3.

    ``None`` keeps boto3's own default (verify against its CA bundle, or the
    one ``AWS_CA_BUNDLE`` names), so a deployment that sets nothing behaves
    exactly as before these options existed.
    """
    if not cfg.verify_tls:
        logger.warning(
            "TLS certificate verification is DISABLED for the S3 endpoint %s: the "
            "connection is encrypted but the server is not authenticated, so anyone "
            "on the network path can impersonate it and read or alter backups and "
            "credentials. Use ca_bundle to trust a private CA instead.",
            cfg.endpoint or "(AWS)",
        )
        return False
    if cfg.ca_bundle:
        if not Path(cfg.ca_bundle).is_file():
            raise ValueError(f"ca_bundle {cfg.ca_bundle} does not exist or is not a file")
        return cfg.ca_bundle
    return None


def build_s3_client(cfg: S3ConnectionConfig) -> Any:
    """A boto3 S3 client for ``cfg``: path-style (unless disabled) + SigV4."""
    style = "path" if cfg.force_path_style else "auto"
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint or None,
        aws_access_key_id=cfg.access_key or None,
        aws_secret_access_key=cfg.secret_key or None,
        region_name=cfg.region,
        verify=tls_verify(cfg),
        config=Config(s3={"addressing_style": style}, signature_version="s3v4"),
    )
