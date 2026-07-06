"""Config model hierarchy.

The root config carries N jobs; each job bundles sources → destinations with
its own schedule / retention / encryption / notifications. Source specs are
*open* (``extra="allow"``) so plugin source types validate their own fields;
destinations are *closed* to ``local`` / ``s3`` (the only two backends).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SourceSpec(BaseModel):
    """A source entry. ``type`` selects the Source implementation (built-in or
    plugin); all other keys are that source's own config and are preserved."""

    model_config = ConfigDict(extra="allow")
    type: str


class DestinationSpec(BaseModel):
    """A destination. Only ``local`` and ``s3`` exist; ``local`` is always the
    working/staging store, ``s3`` is the off-site target when configured."""

    model_config = ConfigDict(extra="allow")
    type: Literal["local", "s3"]


class ScheduleConfig(BaseModel):
    mode: Literal["cron", "interval"] = "cron"
    cron: str = "15 3 * * *"
    interval_hours: int = Field(default=24, ge=1, le=8760)
    on_startup: bool = False
    # Field-based alternative to a raw cron string (normalized by the scheduler).
    hour: Optional[str] = None
    minute: Optional[str] = None
    day_of_week: Optional[str] = None


class GFSConfig(BaseModel):
    """Grandfather-father-son tier keep-counts. 0 disables a tier."""

    daily: int = Field(default=0, ge=0)
    weekly: int = Field(default=0, ge=0)
    monthly: int = Field(default=0, ge=0)


class RetentionConfig(BaseModel):
    count: int = 14  # <= 0 means keep EVERYTHING (safety)
    age_days: int = Field(default=0, ge=0)  # 0 disables age-based pruning
    gfs: GFSConfig = Field(default_factory=GFSConfig)
    smart_last: bool = True  # never prune the sole/last backup of a source


class EncryptionConfig(BaseModel):
    mode: Literal["none", "age", "gpg"] = "none"
    recipient: Optional[str] = None


class EmailChannelConfig(BaseModel):
    host: Optional[str] = None
    port: int = 587
    tls: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    sender: Optional[str] = None
    recipients: list[str] = Field(default_factory=list)


class WebhookChannelConfig(BaseModel):
    url: Optional[str] = None
    secret: Optional[str] = None  # HMAC-SHA256 signing key


class TeamsChannelConfig(BaseModel):
    url: Optional[str] = None
    format: Literal["adaptive", "messagecard"] = "adaptive"


class SimpleUrlChannelConfig(BaseModel):
    url: Optional[str] = None


class NtfyChannelConfig(BaseModel):
    url: Optional[str] = None
    topic: Optional[str] = None
    token: Optional[str] = None


class NotifyConfig(BaseModel):
    channels: list[str] = Field(default_factory=list)
    level: Literal["errors", "warnings", "all"] = "warnings"
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)
    webhook: WebhookChannelConfig = Field(default_factory=WebhookChannelConfig)
    teams: TeamsChannelConfig = Field(default_factory=TeamsChannelConfig)
    slack: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)
    discord: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)
    ntfy: NtfyChannelConfig = Field(default_factory=NtfyChannelConfig)
    healthchecks: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)


def _default_destinations() -> list[DestinationSpec]:
    return [DestinationSpec(type="local")]


class Job(BaseModel):
    name: str = "main"
    sources: list[SourceSpec] = Field(default_factory=list)
    destinations: list[DestinationSpec] = Field(default_factory=_default_destinations)
    # When false, delete the local copy after a successful off-site S3 upload
    # (local stays the working store; the archive lives only off-site).
    keep_local: bool = True
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)
    notifications: NotifyConfig = Field(default_factory=NotifyConfig)


class RootConfig(BaseModel):
    version: int = 1
    instance_name: str = "backup"
    jobs: list[Job] = Field(default_factory=list)
