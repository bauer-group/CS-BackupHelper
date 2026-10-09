"""Which channels carry run data, and in what form (docs/notifications.md,
"Run data and escaping").

Run data is text a backup run produces: the error texts (file names, database
and exception messages) and plugin metrics. The email channel escapes it in
its HTML part (test_email.py); the webhook ships it JSON-encoded but otherwise
verbatim, so receivers must escape; the chat channels never carry it at all.
These tests pin that contract so a future payload change cannot quietly start
pushing run data into Slack/Discord/Teams markup.
"""

from __future__ import annotations

import json

import pytest

from backuphelper.config.models import (
    NtfyChannelConfig,
    SimpleUrlChannelConfig,
    TeamsChannelConfig,
    WebhookChannelConfig,
)
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.discord import DiscordChannel
from backuphelper.notify.healthchecks import HealthchecksChannel
from backuphelper.notify.ntfy import NtfyChannel
from backuphelper.notify.slack import SlackChannel
from backuphelper.notify.teams import TeamsChannel
from backuphelper.notify.webhook import WebhookChannel

URL = "https://chat.example.com/hook"

# A marker no channel can produce on its own, wrapped in the markup each
# platform would interpret: HTML, Slack link/mention syntax, Markdown, a
# Discord mass mention.
MARKER = "run-data-7f3a9c"
RUN_DATA = f"<b>{MARKER}</b> <!channel> <https://evil.example|{MARKER}> **{MARKER}** @everyone"


def _event(status: str) -> AlertEvent:
    return AlertEvent(
        status=status,
        title=f"backup {status}",
        message="snapshot is incomplete - a component failed",
        instance="prod",
        snapshot_id="2026-10-09_03-15-00",
        job="main",
        duration_seconds=12.5,
        total_bytes=4096,
        errors=[f"files: skipped /data/{RUN_DATA}.jpg", f"db: {RUN_DATA}"],
        metrics={"plugin_note": RUN_DATA},
    )


def _record(channel_factory, event):
    sent: dict = {}

    def transport(url, data, headers):
        sent.update(url=url, data=data.decode("utf-8"), headers=dict(headers))

    channel_factory(transport).send(event)
    return sent


CHAT_CHANNELS = {
    "slack": lambda t: SlackChannel(SimpleUrlChannelConfig(url=URL), transport=t),
    "discord": lambda t: DiscordChannel(SimpleUrlChannelConfig(url=URL), transport=t),
    "teams-adaptive": lambda t: TeamsChannel(
        TeamsChannelConfig(url=URL, format="adaptive"), transport=t
    ),
    "teams-messagecard": lambda t: TeamsChannel(
        TeamsChannelConfig(url=URL, format="messagecard"), transport=t
    ),
    "ntfy": lambda t: NtfyChannel(NtfyChannelConfig(url=URL, topic="backups"), transport=t),
    "healthchecks": lambda t: HealthchecksChannel(SimpleUrlChannelConfig(url=URL), transport=t),
}


@pytest.mark.parametrize("status", ["success", "warning", "error"])
@pytest.mark.parametrize("name", sorted(CHAT_CHANNELS))
def test_chat_channels_never_carry_run_data(name, status):
    event = _event(status)

    sent = _record(CHAT_CHANNELS[name], event)

    wire = " ".join([sent["url"], sent["data"], *sent["headers"].values()])
    assert MARKER not in wire
    assert event.message in sent["data"]  # ... while the alert itself arrives


def test_webhook_ships_run_data_json_encoded_but_not_html_escaped():
    event = _event("error")

    sent = _record(
        lambda t: WebhookChannel(WebhookChannelConfig(url=URL), transport=t), event
    )

    payload = json.loads(sent["data"])
    # Verbatim after JSON decoding: rendering it as HTML (or Slack mrkdwn,
    # Markdown) is the receiver's job, and so is escaping it.
    assert payload["errors"] == event.errors
    assert payload["metrics"] == event.metrics
    assert "&lt;" not in sent["data"]
