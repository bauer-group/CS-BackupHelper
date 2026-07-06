"""Microsoft Teams channel: Adaptive Card v1.4 or legacy MessageCard.

Adaptive Cards are the current Teams-native format and must be wrapped in the
``message``/``attachments`` envelope; MessageCards are the older connector
format kept for backward compatibility. Both are colored by status
(green/amber/red) — Adaptive Cards via the semantic color words, MessageCards
via a ``themeColor`` hex.
"""

from __future__ import annotations

import json
from typing import ClassVar, Optional

from backuphelper.config.models import TeamsChannelConfig
from backuphelper.notify.base import AlertEvent, Channel, Transport, http_post

# MessageCard themeColor hex by status (green / amber / red).
THEME_COLOR = {"success": "2DA44E", "warning": "FFC83D", "error": "D13438"}

# Adaptive Card semantic color words by status.
ADAPTIVE_COLOR = {"success": "Good", "warning": "Warning", "error": "Attention"}


class TeamsChannel(Channel):
    """Posts an Adaptive Card or MessageCard to a Teams incoming webhook."""

    name: ClassVar[str] = "teams"

    def __init__(
        self, cfg: TeamsChannelConfig, *, transport: Optional[Transport] = None
    ):
        self.cfg = cfg
        self._transport: Transport = transport or http_post

    def send(self, event: AlertEvent) -> None:
        if not self.cfg.url:
            raise ValueError("teams channel requires a url")

        if self.cfg.format == "messagecard":
            payload = self._message_card(event)
        else:
            payload = self._adaptive_card(event)

        body = json.dumps(payload).encode("utf-8")
        self._transport(self.cfg.url, body, {"Content-Type": "application/json"})

    def _facts(self, event: AlertEvent) -> list[tuple[str, str]]:
        facts: list[tuple[str, str]] = []
        if event.instance:
            facts.append(("Instance", event.instance))
        if event.job:
            facts.append(("Job", event.job))
        if event.snapshot_id:
            facts.append(("Snapshot", event.snapshot_id))
        return facts

    def _adaptive_card(self, event: AlertEvent) -> dict:
        color = ADAPTIVE_COLOR.get(event.status, "Default")
        card_body: list[dict] = [
            {
                "type": "TextBlock",
                "text": event.title,
                "weight": "Bolder",
                "size": "Large",
                "color": color,
                "wrap": True,
            },
            {"type": "TextBlock", "text": event.message, "wrap": True},
        ]
        facts = self._facts(event)
        if facts:
            card_body.append(
                {
                    "type": "FactSet",
                    "facts": [{"title": k, "value": v} for k, v in facts],
                }
            )
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "type": "AdaptiveCard",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "version": "1.4",
                        "body": card_body,
                    },
                }
            ],
        }

    def _message_card(self, event: AlertEvent) -> dict:
        theme = THEME_COLOR.get(event.status, "808080")
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": theme,
            "summary": event.title,
            "title": event.title,
            "text": event.message,
            "sections": [
                {
                    "facts": [
                        {"name": k, "value": v} for k, v in self._facts(event)
                    ]
                }
            ],
        }
