"""Config model hierarchy.

The root config carries N jobs; each job bundles sources → destinations with
its own schedule / retention / encryption / notifications. Source specs are
*open* (``extra="allow"``) so plugin source types validate their own fields;
destinations are *closed* to ``local`` / ``s3`` (the only two backends).
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigModel(BaseModel):
    """Base of every config model (sources, destinations and plugins may use it).

    A validation error must not echo the offending value: it can be a secret
    (e.g. a numeric password from a discrete env override, which the loader
    JSON-parses into an int) and the error text ends up in logs, alerts and
    the snapshot manifest."""

    model_config = ConfigDict(hide_input_in_errors=True)


class SourceSpec(ConfigModel):
    """A source entry. ``type`` selects the Source implementation (built-in or
    plugin); all other keys are that source's own config and are preserved."""

    model_config = ConfigDict(extra="allow")
    type: str


class DestinationSpec(ConfigModel):
    """A destination. Only ``local`` and ``s3`` exist; ``local`` is always the
    working/staging store, ``s3`` is the off-site target when configured."""

    model_config = ConfigDict(extra="allow")
    type: Literal["local", "s3"]


class ScheduleConfig(ConfigModel):
    mode: Literal["cron", "interval"] = "cron"
    cron: str = "15 3 * * *"
    interval_hours: int = Field(default=24, ge=1, le=8760)
    on_startup: bool = False
    # Field-based alternative to a raw cron string (normalized by the scheduler).
    hour: Optional[str] = None
    minute: Optional[str] = None
    day_of_week: Optional[str] = None


class GFSConfig(ConfigModel):
    """Grandfather-father-son tier keep-counts. 0 disables a tier."""

    daily: int = Field(default=0, ge=0)
    weekly: int = Field(default=0, ge=0)
    monthly: int = Field(default=0, ge=0)


class RetentionConfig(ConfigModel):
    count: int = 14  # <= 0 means keep EVERYTHING (safety)
    age_days: int = Field(default=0, ge=0)  # 0 disables age-based pruning
    gfs: GFSConfig = Field(default_factory=GFSConfig)
    smart_last: bool = True  # never prune the sole/last backup of a source


class EncryptionConfig(ConfigModel):
    mode: Literal["none", "age", "gpg"] = "none"
    recipient: Optional[str] = None


class EmailChannelConfig(ConfigModel):
    host: Optional[str] = None
    port: int = 587
    tls: bool = True  # STARTTLS after connecting; not used with implicit_tls
    # TLS from the first byte (SMTPS, usually port 465) instead of a plain
    # connection that STARTTLS upgrades. Off by default, so ``tls`` keeps its
    # meaning for every existing config.
    implicit_tls: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    sender: Optional[str] = None
    recipients: list[str] = Field(default_factory=list)

    @field_validator("recipients", mode="before")
    @classmethod
    def _split_recipients(cls, v: object) -> object:
        # Compose stacks render recipients as ["${ALERT_EMAIL}"]: an unset variable
        # yields [""] (an SMTP RCPT with an empty address) and a list-valued one
        # yields ["a@x, b@y"]. Split every entry on "," / ";", strip whitespace
        # and drop empties; a plain string is accepted like a one-element list.
        items = [v] if isinstance(v, str) else v
        if not isinstance(items, list):
            return v
        out: list[object] = []
        for item in items:
            if isinstance(item, str):
                out.extend(p.strip() for p in re.split(r"[,;]", item) if p.strip())
            else:
                out.append(item)  # leave non-strings to pydantic's own validation error
        return out


class WebhookChannelConfig(ConfigModel):
    url: Optional[str] = None
    secret: Optional[str] = None  # HMAC-SHA256 signing key


class TeamsChannelConfig(ConfigModel):
    url: Optional[str] = None
    format: Literal["adaptive", "messagecard"] = "adaptive"


class SimpleUrlChannelConfig(ConfigModel):
    url: Optional[str] = None


class NtfyChannelConfig(ConfigModel):
    url: Optional[str] = None
    topic: Optional[str] = None
    token: Optional[str] = None


class NotifyConfig(ConfigModel):
    channels: list[str] = Field(default_factory=list)
    level: Literal["errors", "warnings", "all"] = "warnings"

    @field_validator("channels", mode="before")
    @classmethod
    def _split_channels(cls, v: object) -> object:
        # Accept a comma-separated string ("email,webhook") as well as a list,
        # so the fleet's existing ALERT_CHANNELS env values migrate unchanged.
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)
    webhook: WebhookChannelConfig = Field(default_factory=WebhookChannelConfig)
    teams: TeamsChannelConfig = Field(default_factory=TeamsChannelConfig)
    slack: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)
    discord: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)
    ntfy: NtfyChannelConfig = Field(default_factory=NtfyChannelConfig)
    healthchecks: SimpleUrlChannelConfig = Field(default_factory=SimpleUrlChannelConfig)


def _default_destinations() -> list[DestinationSpec]:
    return [DestinationSpec(type="local")]


class Job(ConfigModel):
    name: str = "main"
    sources: list[SourceSpec] = Field(default_factory=list)
    destinations: list[DestinationSpec] = Field(default_factory=_default_destinations)
    # When false, delete the local copy once an off-site destination has stored
    # this snapshot (local stays the working store; the archive lives only
    # off-site). Without a successful off-site copy the local one is kept.
    keep_local: bool = True
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)
    notifications: NotifyConfig = Field(default_factory=NotifyConfig)


class RootConfig(ConfigModel):
    version: int = 1
    instance_name: str = "backup"
    jobs: list[Job] = Field(default_factory=list)
