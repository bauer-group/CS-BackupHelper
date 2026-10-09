"""scripts/e2e.sh helper for the alerts suite: HTTP sink, configs, seed, checks.

Runs with the python of the engine image (standard library only):

    serve            the HTTP sink (port 8080, compose service ``sink``): records
                     every request - method, path, headers, raw body - and serves
                     the records on ``GET /_requests``. A POST below ``/stall/``
                     is recorded and never answered, like a receiver that hangs.
    ping             exit 0 once the sink answers (its compose healthcheck)
    seed             create the filesystem trees the scenarios back up (as root)
    config <name>    print the BACKUP_CONFIG_JSON of one scenario
    check            compare what the sink and both SMTP servers received with
                     what the scenarios must have sent; one ``PASS <label>`` or
                     ``FAIL <label>: <detail>`` line per check, exit 1 on a FAIL

Scenarios (``config``): ``success``, ``warning`` and ``error`` run three jobs
with the notification levels all / warnings / errors and every channel;
``untrusted`` sends SMTPS mail without trusting the runtime CA; ``stall`` posts
to a receiver that never answers, then to Slack.

Secrets never appear in a config: the configs reference ``${E2E_ALERT_SECRET}``,
``${E2E_NTFY_TOKEN}``, ``${E2E_SMTP_USER}`` and ``${E2E_SMTP_PASSWORD}``, which
scripts/e2e/alerts.sh builds at runtime and hands to the engine's environment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

SINK = "http://sink:8080"
MAILPIT = {"mail-starttls": "http://mail-starttls:8025", "mail-smtps": "http://mail-smtps:8025"}
INSTANCE = "e2e-alerts"
SENDER = "backup@backuphelper.test"

# Run data: what a backup run produces and the engine cannot vouch for. It is
# a file name below a filesystem source (warning) and a missing source path
# (error), so it reaches the alert as part of an error text. Markup, a Slack
# mention, Markdown and quotes - none of it may be interpreted by a receiver.
RUNDATA = '<img src=x onerror=alert(1)> & <!channel> **bold** "q"'
RUNDATA_FRAGMENTS = ("<img", "onerror", "<!channel>", "**bold**")  # none may reach a chat
RUNDATA_MARKUP = ("<img", "<!channel>")  # as markup: never in an HTML part

FILES = PurePosixPath("/files/alerts")  # in the backup container
SOURCES = {
    "success": {"type": "filesystem", "name": "alerts", "path": str(FILES / "ok")},
    "warning": {"type": "filesystem", "name": "alerts", "path": str(FILES / "warn")},
    "error": {"type": "filesystem", "name": "alerts", "path": str(FILES / f"missing {RUNDATA}")},
}
# level -> the statuses its job must deliver, in scenario order
LEVELS = {"all": ["success", "warning", "error"], "warnings": ["warning", "error"],
          "errors": ["error"]}
# level -> (SMTP server, port, implicit TLS)
MAIL_SERVER = {"all": ("mail-starttls", 587, False), "warnings": ("mail-smtps", 465, True),
               "errors": ("mail-starttls", 587, False), "untrusted": ("mail-smtps", 465, True)}
TEAMS_FORMAT = {"all": "adaptive", "warnings": "adaptive", "errors": "messagecard"}
MESSAGE = {"success": "snapshot completed", "warning": "snapshot completed with warnings",
           "error": "snapshot is incomplete - a component failed"}
ADAPTIVE_COLOR = {"success": "Good", "warning": "Warning", "error": "Attention"}
THEME_COLOR = {"success": "2DA44E", "warning": "FFC83D", "error": "D13438"}
ALL_CHANNELS = ["webhook", "teams", "slack", "discord", "ntfy", "healthchecks", "email"]


# ── sink ─────────────────────────────────────────────────────────────────────
class _Sink(BaseHTTPRequestHandler):
    records: list[dict] = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with self.lock:
            self.records.append({
                "method": "POST", "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": base64.b64encode(body).decode("ascii"), "at": time.time(),
            })
        if self.path.startswith("/stall/"):
            time.sleep(3600)  # never answer: the client's own timeout must end it
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):  # noqa: N802
        if self.path != "/_requests":
            self.send_error(404)
            return
        with self.lock:
            data = json.dumps(self.records).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"sink: {self.command} {self.path}\n")


def serve() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), _Sink)
    server.daemon_threads = True
    server.serve_forever()
    return 0


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:  # nosec B310 - test network
        return json.loads(resp.read().decode("utf-8"))


def ping() -> int:
    _get_json("http://127.0.0.1:8080/_requests")
    return 0


# ── seed ─────────────────────────────────────────────────────────────────────
def seed() -> int:
    """The scenario trees: ok/ (readable), warn/ (one more file the backup user
    may not read, named with the run data). Runs as root in the backup image."""
    root = Path(FILES)
    for name in ("ok", "warn"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "note.txt").write_text("alerts e2e\n", encoding="utf-8")
    hidden = root / "warn" / f"{RUNDATA}.txt"
    hidden.write_text("not for the backup user\n", encoding="utf-8")
    os.chown(hidden, 0, 0)
    os.chmod(hidden, 0)
    for path in (root, root / "ok", root / "warn"):
        os.chmod(path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    print(f"seeded {FILES}: ok/, warn/ (+ an unreadable file named with the run data)")
    return 0


# ── configs ──────────────────────────────────────────────────────────────────
def _email(level: str) -> dict:
    host, port, implicit = MAIL_SERVER[level]
    return {"host": host, "port": port, "tls": True, "implicit_tls": implicit,
            "username": "${E2E_SMTP_USER}", "password": "${E2E_SMTP_PASSWORD}",
            "sender": SENDER, "recipients": [f"{level}@backuphelper.test"]}


def _level_job(level: str, source: dict) -> dict:
    return {
        "name": f"lvl-{level}",
        "sources": [source],
        "destinations": [{"type": "local"}],
        "notifications": {
            "channels": ALL_CHANNELS,
            "level": level,
            "webhook": {"url": f"{SINK}/webhook/{level}", "secret": "${E2E_ALERT_SECRET}"},
            "teams": {"url": f"{SINK}/teams/{level}", "format": TEAMS_FORMAT[level]},
            "slack": {"url": f"{SINK}/slack/{level}"},
            "discord": {"url": f"{SINK}/discord/{level}"},
            "ntfy": {"url": f"{SINK}/ntfy/{level}", "topic": "backups",
                     "token": "${E2E_NTFY_TOKEN}"},
            "healthchecks": {"url": f"{SINK}/hc/{level}"},
            "email": _email(level),
        },
    }


def config(name: str) -> dict:
    if name in SOURCES:
        jobs = [_level_job(level, SOURCES[name]) for level in LEVELS]
    elif name == "untrusted":
        jobs = [{"name": "lvl-untrusted", "sources": [SOURCES["success"]],
                 "notifications": {"channels": ["email", "webhook"], "level": "all",
                                   "email": _email("untrusted"),
                                   "webhook": {"url": f"{SINK}/webhook/untrusted"}}}]
    elif name == "stall":
        jobs = [{"name": "lvl-stall", "sources": [SOURCES["success"]],
                 "notifications": {"channels": ["webhook", "slack"], "level": "all",
                                   "webhook": {"url": f"{SINK}/stall/webhook"},
                                   "slack": {"url": f"{SINK}/slack/stall"}}}]
    else:
        raise SystemExit(f"unknown scenario {name!r}")
    return {"instance_name": INSTANCE, "jobs": jobs}


# ── checks ───────────────────────────────────────────────────────────────────
class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def expect(self, condition: bool, label: str, detail: object = "") -> bool:
        if condition:
            print(f"PASS {label}")
        else:
            self.failed += 1
            print(f"FAIL {label}: {detail}")
        return condition


def _body(record: dict) -> bytes:
    return base64.b64decode(record["body"])


def _at(records: list[dict], path: str) -> list[dict]:
    return [r for r in records if r["path"] == path]


def _no_run_data(text: str) -> bool:
    return not any(fragment in text for fragment in RUNDATA_FRAGMENTS)


def _webhook_problems(record: dict, level: str, status: str, secret: str) -> list[str]:
    raw = _body(record)
    problems = []
    expected_sig = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(record["headers"].get("x-signature-256", ""), expected_sig):
        problems.append("X-Signature-256 is not the HMAC-SHA256 of the raw body")
    if record["headers"].get("content-type") != "application/json":
        problems.append(f"content-type {record['headers'].get('content-type')!r}")
    payload = json.loads(raw)
    if raw != json.dumps(payload, sort_keys=True).encode("utf-8"):
        problems.append("body is not the sorted-keys serialization the signature contract names")
    keys = {"errors", "instance", "job", "message", "metrics", "snapshot_id", "status"}
    if set(payload) != keys:
        problems.append(f"keys {sorted(payload)}")
    expected = {"instance": INSTANCE, "job": f"lvl-{level}", "status": status,
                "message": MESSAGE[status], "metrics": {}}
    problems += [f"{k} is {payload.get(k)!r}, expected {v!r}"
                 for k, v in expected.items() if payload.get(k) != v]
    if not re.fullmatch(rf"\d{{4}}-\d\d-\d\d_\d\d-\d\d-\d\d_lvl-{level}", payload.get("snapshot_id", "")):
        problems.append(f"snapshot_id {payload.get('snapshot_id')!r} is not job-scoped")
    errors = payload.get("errors") or []
    if status == "success" and errors:
        problems.append(f"errors {errors!r} on success")
    if status != "success" and not any(RUNDATA in e for e in errors):
        problems.append(f"run data not verbatim in errors {errors!r}")
    return problems


def _teams_problems(record: dict, level: str, status: str, sid: str) -> list[str]:
    card = json.loads(_body(record))
    facts_expected = {"Instance": INSTANCE, "Job": f"lvl-{level}", "Snapshot": sid}
    if TEAMS_FORMAT[level] == "messagecard":
        got = {"@type": card.get("@type"), "themeColor": card.get("themeColor"),
               "title": card.get("title"), "text": card.get("text"),
               "facts": {f["name"]: f["value"] for f in card["sections"][0]["facts"]}}
        want = {"@type": "MessageCard", "themeColor": THEME_COLOR[status],
                "title": f"backup {status}", "text": MESSAGE[status], "facts": facts_expected}
    else:
        attachment = card["attachments"][0]
        content = attachment["content"]
        blocks = content["body"]
        got = {"type": card.get("type"), "contentType": attachment.get("contentType"),
               "card": (content.get("type"), content.get("version")),
               "title": (blocks[0]["text"], blocks[0]["color"]), "text": blocks[1]["text"],
               "facts": {f["title"]: f["value"] for f in blocks[2]["facts"]}}
        want = {"type": "message", "contentType": "application/vnd.microsoft.card.adaptive",
                "card": ("AdaptiveCard", "1.4"),
                "title": (f"backup {status}", ADAPTIVE_COLOR[status]), "text": MESSAGE[status],
                "facts": facts_expected}
    return [f"{k} is {got[k]!r}, expected {v!r}" for k, v in want.items() if got[k] != v]


def _mailpit(server: str) -> list[dict]:
    """Every message the server received, oldest first (the API lists newest first)."""
    listing = _get_json(f"{MAILPIT[server]}/api/v1/messages?limit=200")["messages"]
    return [_get_json(f"{MAILPIT[server]}/api/v1/message/{m['ID']}") for m in reversed(listing)]


def _mail_problems(msg: dict, level: str, status: str, sid: str, user: str) -> list[str]:
    problems = []
    subject = f"[{INSTANCE}] backup {status}: {sid}"
    if msg.get("Subject") != subject:
        problems.append(f"subject {msg.get('Subject')!r}, expected {subject!r}")
    if msg.get("Username") != user:
        problems.append(f"SMTP login {msg.get('Username')!r}, expected {user!r}")
    text, body = msg.get("Text") or "", msg.get("HTML") or ""
    for needed in (f"Job: lvl-{level}", "Duration: "):
        if needed not in text:
            problems.append(f"text part lacks {needed!r}")
    if status != "error" and "Size: " not in text:
        problems.append("text part lacks 'Size: '")
    if status != "success":
        if RUNDATA not in text:
            problems.append("run data not verbatim in the text part")
        if html.escape(RUNDATA, quote=True) not in body:
            problems.append("run data not HTML-escaped in the HTML part")
        if any(markup in body for markup in RUNDATA_MARKUP):
            problems.append("run data as markup in the HTML part")
    return problems


def check() -> int:
    secret = os.environ["E2E_ALERT_SECRET"]
    token = os.environ["E2E_NTFY_TOKEN"]
    user = os.environ["E2E_SMTP_USER"]
    c = Checks()
    records = _get_json(f"{SINK}/_requests")
    mail = {server: _mailpit(server) for server in MAILPIT}

    for level, statuses in LEVELS.items():
        tag = f"level {level}"
        hooks = _at(records, f"/webhook/{level}")
        got = [json.loads(_body(r)).get("status") for r in hooks]
        if not c.expect(got == statuses, f"webhook {tag}: delivered {'/'.join(statuses)}",
                        f"got {got}"):
            continue
        sids = {s: json.loads(_body(r))["snapshot_id"] for s, r in zip(statuses, hooks)}
        problems = [f"{s}: {p}" for s, r in zip(statuses, hooks)
                    for p in _webhook_problems(r, level, s, secret)]
        c.expect(not problems, f"webhook {tag}: HMAC signature, payload, run data verbatim",
                 "; ".join(problems))

        teams = _at(records, f"/teams/{level}")
        problems = [f"{s}: {p}" for s, r in zip(statuses, teams)
                    for p in _teams_problems(r, level, s, sids[s])]
        c.expect(len(teams) == len(statuses) and not problems,
                 f"teams {tag}: {TEAMS_FORMAT[level]} card per status",
                 f"{len(teams)} posts; " + "; ".join(problems))

        for channel, key in (("slack", "text"), ("discord", "content")):
            posts = _at(records, f"/{channel}/{level}")
            want = [{key: f"[{INSTANCE}] backup {s}: {MESSAGE[s]} (snapshot {sids[s]})"}
                    for s in statuses]
            c.expect([json.loads(_body(r)) for r in posts] == want,
                     f"{channel} {tag}: one summary line per status",
                     [_body(r).decode("utf-8", "replace") for r in posts])

        posts = _at(records, f"/ntfy/{level}/backups")
        got = [(_body(r).decode("utf-8"), r["headers"].get("title"),
                r["headers"].get("authorization")) for r in posts]
        want = [(MESSAGE[s], f"backup {s}", f"Bearer {token}") for s in statuses]
        c.expect(got == want, f"ntfy {tag}: message, title and bearer token",
                 [(b, t, "Bearer ***" if a == f"Bearer {token}" else a) for b, t, a in got])

        pings = [r["path"] for r in records if r["path"].startswith(f"/hc/{level}")]
        want = [f"/hc/{level}/fail" if s == "error" else f"/hc/{level}" for s in statuses]
        c.expect(pings == want, f"healthchecks {tag}: ping, /fail on error", pings)

        chat = [r for r in records if r["path"].split("/")[1] in
                ("teams", "slack", "discord", "ntfy", "hc") and f"/{level}" in r["path"]]
        leaked = [r["path"] for r in chat
                  if not _no_run_data(_body(r).decode("utf-8", "replace") + json.dumps(r["headers"]))]
        c.expect(not leaked, f"chat channels {tag}: no run data", leaked)

        server = MAIL_SERVER[level][0]
        mails = [m for m in mail[server]
                 if [a.get("Address") for a in m.get("To") or []] == [f"{level}@backuphelper.test"]]
        mode = "SMTPS" if MAIL_SERVER[level][2] else "STARTTLS"
        got = [m.get("Subject", "").split(": ")[0] for m in mails]
        if c.expect(len(mails) == len(statuses),
                    f"email {tag}: {len(statuses)} mail(s) over {mode} with login", got):
            problems = [f"{s}: {p}" for s, m in zip(statuses, mails)
                        for p in _mail_problems(m, level, s, sids[s], user)]
            c.expect(not problems,
                     f"email {tag}: subject, duration, size, run data escaped in HTML and "
                     "verbatim in text", "; ".join(problems))

    untrusted = _at(records, "/webhook/untrusted")
    c.expect(len(untrusted) == 1, "untrusted SMTPS: the webhook after the refused mail delivered",
             len(untrusted))
    refused = [m for m in mail["mail-smtps"]
               if any(a.get("Address") == "untrusted@backuphelper.test" for a in m.get("To") or [])]
    c.expect(not refused, "untrusted SMTPS: no mail sent to a server the engine cannot verify",
             len(refused))

    c.expect(len(_at(records, "/stall/webhook")) == 1 and len(_at(records, "/slack/stall")) == 1,
             "stalled receiver: the hanging webhook was posted, Slack after it still delivered",
             [r["path"] for r in records if "stall" in r["path"]])
    return 1 if c.failed else 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "serve":
        return serve()
    if command == "ping":
        return ping()
    if command == "seed":
        return seed()
    if command == "config":
        print(json.dumps(config(sys.argv[2])))
        return 0
    if command == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
