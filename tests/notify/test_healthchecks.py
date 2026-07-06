"""Tests for the healthchecks dead-man's-switch channel."""

from __future__ import annotations

import pytest

from backuphelper.config.models import SimpleUrlChannelConfig
from backuphelper.notify.base import AlertEvent
from backuphelper.notify.healthchecks import HealthchecksChannel

BASE = "https://hc-ping.com/abc-123"


def _recorder():
    sent: dict = {}

    def transport(url, data, headers):
        sent["url"] = url
        sent["data"] = data
        sent["headers"] = dict(headers)

    return sent, transport


def _event(status):
    return AlertEvent(status=status, title="t", message="run finished")


def test_success_pings_base_url():
    sent, transport = _recorder()
    HealthchecksChannel(SimpleUrlChannelConfig(url=BASE), transport=transport).send(
        _event("success")
    )
    assert sent["url"] == BASE
    assert isinstance(sent["data"], (bytes, bytearray))


def test_warning_pings_base_url():
    sent, transport = _recorder()
    HealthchecksChannel(SimpleUrlChannelConfig(url=BASE), transport=transport).send(
        _event("warning")
    )
    assert sent["url"] == BASE


def test_error_pings_fail_endpoint():
    sent, transport = _recorder()
    HealthchecksChannel(SimpleUrlChannelConfig(url=BASE), transport=transport).send(
        _event("error")
    )
    assert sent["url"] == f"{BASE}/fail"


def test_trailing_slash_is_normalized_before_fail_suffix():
    sent, transport = _recorder()
    HealthchecksChannel(
        SimpleUrlChannelConfig(url=BASE + "/"), transport=transport
    ).send(_event("error"))
    assert sent["url"] == f"{BASE}/fail"


def test_without_url_raises():
    with pytest.raises(ValueError):
        HealthchecksChannel(
            SimpleUrlChannelConfig(), transport=lambda *a, **k: None
        ).send(_event("success"))
