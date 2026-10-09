Destinations are the *where it lands* side of a job — a keyed object store of backup artifacts. The engine stages and bundles a snapshot locally, then hands each destination the finished archive plus its `sha256` sidecar manifest. See [sources](sources.md) for what goes into a snapshot and [configuration](configuration.md) for the job model.

## The destination model

There are exactly **two** destination backends — `local` and `s3`. The `destinations` list is *closed* to these two (`type` is `local` or `s3`); anything else is a config error.

| type | backend | role |
| --- | --- | --- |
| `local` | a directory tree under the data dir | the working/default store |
| `s3` | any S3-compatible bucket | the off-site target |

Every destination implements the same contract — `put` / `get` / `list_keys` / `delete` / `exists` — and `list_keys` is always returned sorted, so snapshot ordering is deterministic across platforms.

### Policy: S3 when configured, otherwise local

`local` is **always** the working store: the pipeline stages and bundles every snapshot on local disk (under `<data_dir>/.work/<snapshot-id>/`) regardless of where it ultimately ships. If no destinations are listed, a job defaults to a single `local` destination.

- List only `local` → snapshots stay on the local data dir (the default).
- List only `s3` → snapshots ship off-site to the bucket.
- List **both** → the archive and its sidecar are written to each; you keep a local copy *and* an off-site copy.
- List **both** with `"keep_local": false` → the local copy is deleted, but only after an off-site destination has actually stored *this* snapshot (archive and manifest uploaded, remote size verified). When no off-site copy exists — the S3 bucket is unset, the target is unreachable, or the upload failed — the local copy is kept and the job reports a `warning`, so the only copy is never deleted; a reachable S3 target picks it up on the next run (see [Catching up after an S3 outage](#catching-up-after-an-s3-outage)).

The archive and its `<snapshot-id>.manifest.json` sidecar are `put` to **every** configured destination, and retention (count / age / GFS / smart-last) is applied independently **per destination**.

### When a destination fails

A failing destination does not abort the run, and a snapshot is only lost when no destination at all could store it:

- An S3 destination that cannot be set up at the start of the upload phase — rejected credentials (`403`), unreachable endpoint, DNS error, bucket creation denied under `ensure_bucket` — is skipped for this run. An upload that fails (or fails its size check) counts the same way.
- Either case degrades the job to `warning`. The other destinations still receive the snapshot, and the error text is in the job's alert and in the log.
- If the snapshot reached **no** configured destination (e.g. an S3-only job whose bucket is unreachable), it is kept in the local data dir as a fallback and the alert says so. The job's retention prunes only **its own** fallback copies there — never snapshots of other jobs that share the data dir.
- If even that fails (e.g. the data dir is full), no copy of the run exists: the job reports `error`, so `--now` exits `1` and an alert goes out even at `level: errors`.
- The staging area under `<data_dir>/.work/` is always removed, even when a run is aborted (for example by a `pre_backup` hook).

#### Catching up after an S3 outage

A snapshot that should be off-site but is not — an S3-only job's fallback copy, or a job listing both `local` and `s3` whose S3 upload failed — is marked with a `<snapshot-id>.offsite-pending.json` file next to its manifest (it names the job). The next run of that job that reaches an off-site destination uploads every pending snapshot there and removes the marker. When the job keeps no local copies (S3-only, or `"keep_local": false`) the local copy goes too, so no snapshot is left in the data dir once S3 is back (only the run records in `.state/` stay) and the [healthcheck](deployment.md#the-functional-healthcheck) does not age on a leftover manifest. A pending upload that fails again stays pending and is retried on the following run. Retention and `prune` delete a marker together with its snapshot.

```json
{
  "destinations": [
    { "type": "local" },
    { "type": "s3", "bucket": "offsite", "prefix": "iam/" }
  ]
}
```

---

## `local`

Artifacts are stored under `root/<key>`, where `root` is the job's data directory (there are no per-spec config fields — a `local` entry is just `{ "type": "local" }`). Parent directories are created on write; `list_keys` returns keys relative to `root` in posix form, sorted, so ordering is stable across platforms.

```json
{
  "destinations": [
    { "type": "local" }
  ]
}
```

This is the default when `destinations` is omitted, and it is also the working store even when you ship off-site to S3.

---

## `s3`

Ships artifacts to any S3-compatible bucket. The upload path is deliberately **hand-rolled** rather than delegated to boto3's `upload_file`/TransferManager, because backups routinely target MinIO and Ceph/RGW, which are strict about multipart semantics.

| field | default | description |
| --- | --- | --- |
| `bucket` | *(required)* | target bucket name |
| `endpoint` | `null` | S3-compatible endpoint URL; `null` targets AWS |
| `region` | `"eu-central-1"` | region |
| `access_key` | `""` | access key id (empty → default credential chain) |
| `secret_key` | `""` | secret access key |
| `prefix` | `""` | key prefix; transparently prepended to every key |
| `force_path_style` | `true` | path-style addressing (needed for MinIO/Ceph); `false` uses virtual-host style |
| `verify_tls` | `true` | verify the endpoint's TLS certificate; `false` switches verification off — see [TLS certificate verification](#tls-certificate-verification) |
| `ca_bundle` | `null` | path to a PEM file with the CA certificate(s) to trust for the endpoint, e.g. a private CA (ignored when `verify_tls` is `false`) |
| `multipart_threshold` | `104857600` | `100 * 1024 * 1024` (100 MiB): files below this take a single `put_object` |
| `multipart_chunk_size` | `52428800` | `50 * 1024 * 1024` (50 MiB): size of each multipart part |
| `ensure_bucket` | `true` | create the bucket on first use if it does not exist |

```json
{
  "destinations": [
    {
      "type": "s3",
      "endpoint": "https://minio:9000",
      "bucket": "offsite",
      "region": "eu-central-1",
      "access_key": "${S3_ACCESS_KEY}",
      "secret_key": "${S3_SECRET_KEY}",
      "prefix": "iam/"
    }
  ]
}
```

### Equal-chunk multipart upload

Files smaller than `multipart_threshold` are uploaded in a single `put_object`. Larger files are split into **equal** `multipart_chunk_size` parts (only the final part is shorter), so every part is uniform:

1. `create_multipart_upload` opens the upload.
2. Each fixed-size chunk is sent with `upload_part` under an incrementing part number, collecting ETags.
3. `complete_multipart_upload` assembles the parts.
4. **Post-upload verification:** `head_object` reads back the object's `ContentLength` and it is compared against the local file size — a mismatch raises an error (the object is not silently accepted).

**Abort on failure.** If any step of the multipart upload raises, the in-flight upload is cleaned up with `abort_multipart_upload` (best-effort; a failure to abort is logged) and the original error is re-raised, so no orphaned parts are left behind.

All network calls are wrapped in a retry helper, so transient errors retry with backoff. Keys are transparently prefixed with `prefix`.

### S3-compatible endpoints

The client is built with **path-style addressing** (when `force_path_style` is true) and **SigV4** (`signature_version="s3v4"`) — the combination that makes non-AWS providers work. Point `endpoint` at your provider:

- **MinIO / Ceph RGW / Garage** — self-hosted; keep `force_path_style: true`.
- **Cloudflare R2, Backblaze B2, Wasabi** — set `endpoint` to the provider's S3 URL and the matching `region`.

When `ensure_bucket` is true, the destination checks the bucket with `head_bucket` on startup and creates it if missing (adding a `LocationConstraint` for any region other than `us-east-1`). Set `ensure_bucket: false` if the credentials are not allowed to create buckets. If the check or the creation fails, the destination is skipped for that run (see [When a destination fails](#when-a-destination-fails)).

### TLS certificate verification

An `https://` endpoint's certificate is verified, as before these options existed: against boto3's default CA bundle, or the file the `AWS_CA_BUNDLE` environment variable names. The same two fields exist on the [`s3` source](sources.md#s3).

- **Private CA** (a self-hosted MinIO or Ceph behind an internal PKI): mount the CA certificate into the container and set `ca_bundle` to its path. The file *replaces* the default bundle for this endpoint, so it must contain every CA the endpoint's chain needs. A path that does not exist is an error (the destination is skipped for the run).
- **No verification**: `"verify_tls": false` switches the check off. Every run logs a warning naming the endpoint, and urllib3 prints an `InsecureRequestWarning` to stderr for **every request** — an `s3` source mirror makes about two per object, so expect a long log.

> **Security warning.** Without verification the connection is still encrypted, but the
> server is no longer authenticated: anyone on the network path can impersonate the
> endpoint, read and alter the backup archives, and capture the S3 credentials. Use it
> only for a short test or a trusted, isolated network, and prefer `ca_bundle` — it keeps
> the protection with a self-signed or private-CA certificate.

```json
{
  "destinations": [
    {
      "type": "s3",
      "endpoint": "https://minio.internal:9000",
      "bucket": "offsite",
      "access_key": "${S3_ACCESS_KEY}",
      "secret_key": "${S3_SECRET_KEY}",
      "ca_bundle": "/certs/internal-ca.pem"
    }
  ]
}
```

```bash
# List and verify snapshots across local + remote destinations
backuphelper list
backuphelper verify <snapshot-id>
```

---

See [configuration](configuration.md) for schedule, retention, encryption and notification settings that wrap these destinations, and [sources](sources.md) for what each snapshot contains.
