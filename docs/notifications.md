Backup outcomes can be pushed to one or more alert channels — email, Microsoft Teams, Slack, Discord, ntfy, a signed generic webhook, or a Healthchecks.io-style dead-man's switch.

## Overview

Every job carries its own `notifications` block. After a run finishes — or aborts, e.g. because a `pre_backup` hook raised or the disk filled up while bundling (status `error`, `run aborted: …`) — the runner builds one `AlertEvent` (status `success` / `warning` / `error`) and hands it to the `AlertManager`, which:

1. **Gates by severity** — drops the event if its status does not clear the configured `level`.
2. **Fans out** to each name in `channels`, building only those channels.
3. **Isolates faults** — a channel that raises is logged and skipped; the others still receive the alert.

What each status means is listed under [run status](cli.md#run-status). Since 1.7.7 a run in which one component failed completely (a failed `pg_dump`, a raising plugin or S3 source, a missing path) is an `error`, so it is delivered at every `level`; up to 1.7.6 it was a `warning` as long as another component succeeded.

See the [configuration](configuration.md) reference for how the `notifications` block sits inside a job.

CI delivers every channel to real receivers on each pull request (`scripts/e2e.sh alerts`): an HTTP receiver that checks the webhook signature and the Teams, Slack, Discord, ntfy and Healthchecks payloads, and two SMTP servers — STARTTLS and SMTPS, login required — with a certificate from a private CA. It checks the level gating, the escaping of run data, the refusal of an SMTPS certificate the engine does not trust and the time limit for a receiver that never answers.

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

Each channel is delivered independently inside its own `try`/`except`. If its send raises (bad URL, SMTP auth failure, HTTP error), the failure is logged with a stack trace and delivery continues to the remaining channels. One broken channel never suppresses a working one, and a channel failure does not fail the backup job.

Every delivery is bounded in time. The HTTP channels (webhook, Teams, Slack, Discord, ntfy, Healthchecks) must connect within 30 seconds, and each wait for the receiver's answer is limited to 30 seconds as well; a receiver that does not answer in time — a hanging endpoint or proxy — fails its channel like any other delivery error, and the next channel is delivered. Up to 1.10.0 there was no limit: such a receiver held up the run that sent the alert, and under the daemon every later run of the job. The email channel has its own limit, see [connection security](#connection-security).

A channel that is listed in `channels` but lacks its required setting (an empty `url`, an email channel without `host` or without any recipient address) is **not configured** rather than failing: it is skipped with one warning line, e.g. `notification channel 'email' skipped — not configured: email channel has no recipient address`, and nothing is sent.

## Channels

### Email

Sends a multipart text + HTML message over SMTP — with STARTTLS (the default), implicit TLS (SMTPS) or plain, see [connection security](#connection-security). Login is performed only when configured.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `host` | string | `null` | SMTP server. Required — without it the channel is skipped with a warning. |
| `port` | int | `587` | SMTP port. Set `465` (or your server's SMTPS port) together with `implicit_tls`. |
| `tls` | bool | `true` | Issue `STARTTLS` after connecting. Not used when `implicit_tls` is `true`. |
| `implicit_tls` | bool | `false` | Speak TLS from the first byte (SMTPS, usually port `465`) instead of upgrading a plain connection with STARTTLS. Verifies the server certificate. Engines up to 1.7.7 do not know this key and ignore it. |
| `username` | string | `null` | Login is performed only when both `username` and `password` are set. |
| `password` | string | `null` | |
| `sender` | string | `null` | `From` header. |
| `recipients` | list of string | `[]` | `To` header. Each entry may hold several addresses separated by `,` or `;` (a plain string works too); whitespace is stripped and empty entries are dropped, so `[""]` from an unset variable means *no recipients*. Required — with no address left the channel is skipped with a warning instead of sending. |

The subject is `[<instance>] backup <status>: <snapshot_id>`. The body — plain-text and HTML part alike — includes the job, the run's duration and size (each only when the alert carries a value for it) and every error text. In the HTML part a multi-line error text keeps its line breaks and indentation (`white-space: pre-wrap`), e.g. the stderr lines of a failed `pg_dump`; mail clients that ignore that style show it as one wrapped paragraph. Up to 1.7.7 the HTML part showed neither duration nor size. Up to 1.10.0 every alert carried a duration of 0 s, so no mail showed one; the duration is the wall time of the run, from its start to the alert.

Every value the HTML part shows — title, message, instance, job, snapshot id, status and each error text — is HTML-escaped. An error text that contains markup or `<`, `>`, `&` (a crafted file name below a filesystem source, a database or exception message) therefore appears as literal text and is never interpreted by the mail client. The plain-text part carries the same values unchanged.

```json
{ "channels": ["email"], "level": "warnings",
  "email": {
    "host": "smtp.example.com", "port": 587, "tls": true,
    "username": "backup@example.com", "password": "${SMTP_PASSWORD}",
    "sender": "backup@example.com", "recipients": ["ops@example.com"]
  }
}
```

#### Connection security

Choose the mode the SMTP server expects on the port you use:

| The server offers | Settings |
| --- | --- |
| STARTTLS on a submission port (usually 587) | `"port": 587, "tls": true` — the default |
| Implicit TLS / SMTPS (usually 465) | `"port": 465, "implicit_tls": true` |
| No TLS at all (an internal relay, usually port 25) | `"port": 25, "tls": false` |

`"tls": true` keeps meaning STARTTLS, so existing configs need no change. With `implicit_tls` set, `tls` is not used: the session is encrypted before the first SMTP command. A mode that does not match the port fails the email channel instead of sending — implicit TLS against a STARTTLS port fails the TLS handshake at once (`WRONG_VERSION_NUMBER`); a plain or STARTTLS connection to an SMTPS port gets no greeting, because the server waits for a TLS handshake, and fails at the time limit below at the latest.

The two TLS modes check certificates differently:

- **`implicit_tls`** verifies the server certificate against the CA certificates installed in the image and checks that it is issued for `host`. A self-signed certificate, one from a private CA, or a `host` the certificate is not issued for is refused (`CERTIFICATE_VERIFY_FAILED`) before anything — the login included — is sent. To trust a private CA, mount a PEM file with its certificate and set the container's `SSL_CERT_FILE` environment variable to it. The file *replaces* the image's CA store for every connection the engine verifies against it — the HTTP alert channels included — so add the public CAs (`/etc/ssl/certs/ca-certificates.crt`) to it when other channels post to public endpoints.
- **STARTTLS (`tls`)** uses the default of Python's `smtplib`, unchanged from earlier releases: the session is encrypted, but the server certificate is not verified.

Each step of the SMTP session (connect, TLS handshake, greeting, every command, the message upload) must complete within 60 seconds. A server that does not answer in time fails the email channel like any other delivery error (logged, other channels still receive the alert) instead of blocking the run. Up to 1.7.7 there was no limit: a server that never answered, such as an SMTPS port waiting for a TLS handshake, blocked the run and every later scheduled run of the job.

```json
{ "channels": ["email"],
  "email": {
    "host": "smtp.example.com", "port": 465, "implicit_tls": true,
    "username": "backup@example.com", "password": "${SMTP_PASSWORD}",
    "sender": "backup@example.com", "recipients": ["ops@example.com"]
  }
}
```

### Webhook

A deterministic JSON POST, optionally HMAC-SHA256 signed. See [Webhook signing](#webhook-signing) for the signature contract.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Target URL. Required — without it the channel is skipped with a warning. |
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

`errors` (and `metrics`) carry run data verbatim: a receiver that shows them — or `message` — in HTML, Markdown or a chat message must escape them on its side. See [run data and escaping](#run-data-and-escaping).

### Microsoft Teams

Posts to a Teams incoming webhook as an Adaptive Card (v1.4, the current Teams-native format) or a legacy MessageCard.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Teams incoming webhook. Required — without it the channel is skipped with a warning. |
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
| `url` | string | `null` | Slack incoming webhook. Required — without it the channel is skipped with a warning. |

The summary line is `[<instance>] <title>: <message> (snapshot <id>)`.

```json
{ "channels": ["slack"], "slack": { "url": "https://hooks.slack.com/services/..." } }
```

### Discord

Posts to a Discord webhook as `{"content": "<summary>"}` (same summary line as Slack).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Discord webhook. Required — without it the channel is skipped with a warning. |

```json
{ "channels": ["discord"], "discord": { "url": "https://discord.com/api/webhooks/..." } }
```

### ntfy

POSTs the event message as a plain-text body to `url` (with `topic` appended when set). The title becomes the ntfy `Title` header.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Base ntfy URL. Required — without it the channel is skipped with a warning. |
| `topic` | string | `null` | Appended to the URL as `<url>/<topic>`. |
| `token` | string | `null` | Sent as `Authorization: Bearer <token>` for private ntfy instances. |

```json
{ "channels": ["ntfy"],
  "ntfy": { "url": "https://ntfy.sh", "topic": "backups", "token": "${NTFY_TOKEN}" } }
```

### Healthchecks (dead-man's switch)

Pings a Healthchecks.io-style monitoring check. A `success` or `warning` outcome pings the base check URL (the switch stays alive); an `error` — including a run with a failed component — pings the `<url>/fail` endpoint so the monitor flips the check red. The event message is sent as the request body so it appears in the check's log.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `url` | string | `null` | Base check URL. Required — without it the channel is skipped with a warning. |

```json
{ "channels": ["healthchecks"], "level": "all",
  "healthchecks": { "url": "https://hc-ping.com/<uuid>" } }
```

> Set `level` to `all` when using Healthchecks as a dead-man's switch. With the default `warnings` level, successful runs are gated out and never ping the check, so it would eventually go stale and report a false failure.

## Run data and escaping

Most of an alert is fixed engine text or your own config: the `status`, the title `backup <status>`, the outcome `message` (one of a few fixed sentences such as `snapshot is incomplete - a component failed`), the `instance` and `job` names from the config and the generated snapshot id. **Run data** is what the backup run itself produces, and it can contain anything a file name, a database or an exception message can contain — markup included:

- `errors` — e.g. a file name below a filesystem source, a `pg_dump` stderr line, an exception message;
- `metrics` — reserved for values from source plugins; the engine currently always sends it empty, but treat it like `errors`.

Where run data goes, and in what form:

| Channel | Run data sent | Form |
| --- | --- | --- |
| Email | `errors` | HTML part: HTML-escaped (shown as text, never as markup). Plain-text part: verbatim. |
| Webhook | `errors`, `metrics` | JSON-encoded, **not** escaped for any markup language. |
| Slack, Discord | none | One summary line: instance, title, message, snapshot id. |
| Microsoft Teams | none | Title, message; instance, job and snapshot id as facts. |
| ntfy | none | `message` as plain-text body, title as `Title` header. |
| Healthchecks | none | `message` as body. |

**Webhook receivers must escape.** JSON encoding makes the webhook body structurally safe, but every string arrives exactly as the engine produced it, `<`, `>`, `&` and quotes included. A receiver that renders `errors`, `message` or `metrics` as HTML — a dashboard, a ticket, a mail it builds — must escape them itself (e.g. Python's `html.escape()`, or a template engine with auto-escaping). The same applies when it forwards them into a format with its own markup, such as Slack `mrkdwn` (escape `&`, `<`, `>`) or Markdown.

**Chat channels never carry run data.** Slack, Discord, Teams, ntfy and Healthchecks receive neither `errors` nor `metrics`, so nothing a backup run produces can inject markup, links or mentions there; the error details reach you by email or webhook only. These channels send their few fields verbatim, and the platforms do interpret markup in them — Slack link and mention syntax (`<…>`), Discord mentions and Markdown, Markdown in Teams cards. As those values come from your config, keep `instance_name` and job names plain (letters, digits, `-`, `_`, `.`).

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
