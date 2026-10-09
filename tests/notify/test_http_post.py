"""The shared HTTP transport of the webhook, Teams, Slack, Discord, ntfy and
Healthchecks channels, against a real socket on the loopback interface."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error

import pytest

from backuphelper.notify import base
from backuphelper.notify.base import http_post


@pytest.fixture
def silent_receiver():
    """A receiver that accepts the connection, reads the request and never
    answers - a hanging webhook endpoint or proxy."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            accepted.append(conn)  # keep it open, never reply

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/hook"
    finally:
        stop.set()
        thread.join(2)
        for conn in accepted:
            conn.close()
        server.close()


def test_a_receiver_that_never_answers_fails_the_post_at_the_time_limit(
        silent_receiver, monkeypatch):
    # Regression: urlopen ran without a timeout, so a receiver that accepted
    # the request and never answered blocked the run that sent the alert -
    # and, under the daemon, every later run of the job (max_instances=1).
    monkeypatch.setattr(base, "HTTP_TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    with pytest.raises((TimeoutError, urllib.error.URLError)):
        http_post(silent_receiver, b"{}", {"Content-Type": "application/json"})
    assert time.monotonic() - started < 10


def test_every_post_is_bounded_by_the_default_time_limit(monkeypatch):
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout", args[1] if len(args) > 1 else None)
        return _Response()

    monkeypatch.setattr(base.urllib.request, "urlopen", fake_urlopen)
    http_post("http://receiver.invalid/hook", b"{}", {})
    assert seen["timeout"] == base.HTTP_TIMEOUT_SECONDS == 30
