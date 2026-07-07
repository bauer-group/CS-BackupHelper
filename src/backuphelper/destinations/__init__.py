"""Destinations — keyed object stores for backup artifacts.

Built-ins: ``local`` (a directory tree) and ``s3`` (any S3-compatible endpoint
via path-style + SigV4, with hand-rolled equal-chunk multipart for MinIO/Ceph
compatibility). All implement the :class:`~backuphelper.destinations.base.Destination`
contract: put / get / list_keys / delete / exists.
"""

from __future__ import annotations

from .base import Destination
from .local import LocalDestination
from .s3 import S3Destination, S3DestinationConfig

__all__ = [
    "Destination",
    "LocalDestination",
    "S3Destination",
    "S3DestinationConfig",
]
