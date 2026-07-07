Backup outcomes can be pushed to one or more alert channels — email, Microsoft Teams, Slack, Discord, ntfy, a signed generic webhook, or a Healthchecks.io-style dead-man's switch.

## Overview

Every job carries its own `notifications` block. After a run finishes, the runner builds one `AlertEvent` (status `success` / `warning` / `error`) and hands it to the `AlertManager`, which:

1. **Gates by severity** — drops the event if its status does not clear the configured `level`.
2. **Fans out** to each name in `channels`, building only those channels.
3. **Isolates faults** — a channel that raises is logged and skipped; the others still receive the alert.

See the [configuration](configuration.md) reference for how the `notifications` block sits inside a job.

## The `notifications` block

```json
{
  "notifications": {
    "channels": ["webhook", "teams"],
    "level": "warnings",
    "webhook": { "url": "https://ci.example.com/hooks/backup", "secret": "${WEBHOOK_SECRET}" },
    "teams":   { "url": "https://outlook.office.com/webhook/...", "format": "adaptive" }
  }
}
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `channels` | list of string | `[]` | Which channels to deliver to. Each entry names a sub-config below. An empty list disables notifications. |
| `level` | `errors` \| `warnings` \| `all` | `warnings` | Minimum severity that is delivered (see gating below). |
| `email` | object | see [Email](#email) | Per-channel sub-config. |
| `webhook` | object | see [Webhook](#webhook) | Per-channel sub-config. |
| `teams` | object | see [Microsoft Teams](#microsoft-teams) | Per-channel sub-config. |
| `slack` | object | see [Slack](#slack) | Per-channel sub-config. |
| `discord` | object | see [Discord](#discord) | Per-channel sub-config. |
| `ntfy` | object | see [ntfy](#ntfy) | Per-channel sub-config. |
| `healthchecks` | object | see [Healthchecks](#healthchecks-dead-mans-switch) | Per-channel sub-config. |

A name in `channels` must match one of the sub-config keys above. Every sub-config always exists with defaults, so you only override the fields you need. An unknown channel name is logged as a warning and skipped.

## Severity gating

The `level` sets which statuses clear the gate. The gate is evaluated once per event, before any channel is built:

| `level` | `success` | `warning` | `error` |
| --- | --- | --- | --- |
| `errors` | dropped | dropped | delivered |
| `warnings` *(default)* | dropped | delivered | delivered |
| `all` | delivered | delivered | delivered |

An unrecognized `level` value falls back to `warnings`. If `channels` is empty, nothing is delivered regardless of `level`.

> Note: successful runs are only delivered when `level` is `all`. This matters for the [Healthchecks](#healthchecks-dead-mans-switch) channel, whose "still alive" ping needs successful runs to reach it.

## Per-channel fault isolation

Each channel is delivered independently inside its own `try`/`except`. If a channel is misconfigured or its send raises (bad URL, SMTP auth failure, HTTP error), the failure is logged with a stack trace and delivery continues to the remaining channels. One broken channel never suppresses a working one, and a channel failure does not fail the backup job.

## Channels

### Email

Sends a multipart text + HTML message over SMTP. STARTTLS and login are applied only when configured.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `host` | string | `null` | SMTP server. Required — send raises without it. |
| `port` | int | `587` | SMTP port. |
| `tls` | bool | `true` | Issue `STARTTLS` before sending. |
| `username` | string | `null` | Login is performed only when both `username` and `password` are set. |
| `password` | string | `null` | |
| `sender` | string | `null` | `From` header. |
| `recipients` | list of string | `[]` | `To` header. Required — send raises when empty. |

The subject is `[<instance>] backup <status>: <snapshot_id>`. The body includes job, duration, size and any errors.

```json
{ "channels": ["email"], "level": "warnings",
  "email": {
    "host": "smtp.example.com", "port": 587, "tls": true,
    "username": "backup@example.com", "password": "${SMTP_PASSWORD}",
    "sender": "backup@example.com", "recipients": ["ops@example.com"]
  }
}
```

### Webhook

A deterministic JSON POST, optionally HMAC-SHA256 signed. See [Webhook signing](#webhook-signing) for the signature contract.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Target URL. Required — send raises without it. |
| `secret` | string | `null` | HMAC-SHA256 signing key. When set, an `X-Signature-256` header is added. |

The POST body is `application/json` with these keys (serialized with sorted keys):

```json
{
  "errors": [],
  "instance": "iam",
  "job": "main",
  "message": "snapshot completed",
  "metrics": {},
  "snapshot_id": "2026-07-05_03-15-00",
  "status": "success"
}
```

### Microsoft Teams

Posts to a Teams incoming webhook as an Adaptive Card (v1.4, the current Teams-native format) or a legacy MessageCard.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Teams incoming webhook. Required — send raises without it. |
| `format` | `adaptive` \| `messagecard` | `adaptive` | Card format. |

The card is colored by status: green (`success`), amber (`warning`), red (`error`) — Adaptive Cards use the semantic words `Good` / `Warning` / `Attention`; MessageCards use a `themeColor` hex. Instance, job and snapshot are rendered as a fact list.

```json
{ "channels": ["teams"],
  "teams": { "url": "https://outlook.office.com/webhook/...", "format": "adaptive" } }
```

### Slack

Posts to a Slack incoming webhook as `{"text": "<summary>"}`.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Slack incoming webhook. Required — send raises without it. |

The summary line is `[<instance>] <title>: <message> (snapshot <id>)`.

```json
{ "channels": ["slack"], "slack": { "url": "https://hooks.slack.com/services/..." } }
```

### Discord

Posts to a Discord webhook as `{"content": "<summary>"}` (same summary line as Slack).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Discord webhook. Required — send raises without it. |

```json
{ "channels": ["discord"], "discord": { "url": "https://discord.com/api/webhooks/..." } }
```

### ntfy

POSTs the event message as a plain-text body to `url` (with `topic` appended when set). The title becomes the ntfy `Title` header.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Base ntfy URL. Required — send raises without it. |
| `topic` | string | `null` | Appended to the URL as `<url>/<topic>`. |
| `token` | string | `null` | Sent as `Authorization: Bearer <token>` for private ntfy instances. |

```json
{ "channels": ["ntfy"],
  "ntfy": { "url": "https://ntfy.sh", "topic": "backups", "token": "${NTFY_TOKEN}" } }
```

### Healthchecks (dead-man's switch)

Pings a Healthchecks.io-style monitoring check. A `success` or `warning` outcome pings the base check URL (the switch stays alive); an `error` pings the `<url>/fail` endpoint so the monitor flips the check red. The event message is sent as the request body so it appears in the check's log.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Base check URL. Required — send raises without it. |

```json
{ "channels": ["healthchecks"], "level": "all",
  "healthchecks": { "url": "https://hc-ping.com/<uuid>" } }
```

> Set `level` to `all` when using Healthchecks as a dead-man's switch. With the default `warnings` level, successful runs are gated out and never ping the check, so it would eventually go stale and report a false failure.

## Webhook signing

When `webhook.secret` is set, the request is signed so the receiver can prove it came from BackupHelper and was not tampered with.

**The contract:**

- The body is the JSON payload serialized with **sorted keys** (`json.dumps(payload, sort_keys=True)`), UTF-8 encoded. Signing those exact bytes is what makes the signature reproducible.
- The signature is `HMAC-SHA256(secret, body)`, hex-encoded.
- It is sent in the header:

  ```
  X-Signature-256: sha256=<hex-digest>
  ```

- `Content-Type` is `application/json`.

**Receiver-side verification (Python):**

```python
import hashlib
import hmac


def verify_signature(secret: str, raw_body: bytes, header_value: str) -> bool:
    """Return True if X-Signature-256 matches an HMAC-SHA256 of the raw body.

    raw_body MUST be the exact bytes received on the wire — do not re-serialize
    the parsed JSON, or the digest will not match.
    """
    if not header_value.startswith("sha256="):
        return False
    received = header_value[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)
```

Flask example:

```python
from flask import Flask, request, abort

app = Flask(__name__)
SECRET = "the-same-secret-configured-in-backuphelper"


@app.post("/hooks/backup")
def backup_hook():
    sig = request.headers.get("X-Signature-256", "")
    if not verify_signature(SECRET, request.get_data(), sig):
        abort(401)
    payload = request.get_json()
    # ... handle payload["status"], payload["snapshot_id"], ...
    return "", 204
```

Always verify against the **raw request bytes** (`request.get_data()`), not a re-encoded copy of the parsed JSON, and compare with a constant-time function such as `hmac.compare_digest`.
