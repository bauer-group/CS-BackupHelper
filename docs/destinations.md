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

The archive and its `<snapshot-id>.manifest.json` sidecar are `put` to **every** configured destination, and retention (count / age / GFS / smart-last) is applied independently **per destination**. A failed upload to one destination degrades the job to a partial/warning state rather than aborting the others.

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

When `ensure_bucket` is true, the destination checks the bucket with `head_bucket` on startup and creates it if missing (adding a `LocationConstraint` for any region other than `us-east-1`). Set `ensure_bucket: false` if the credentials are not allowed to create buckets.

```bash
# List and verify snapshots across local + remote destinations
backuphelper list
backuphelper verify <snapshot-id>
```

---

See [configuration](configuration.md) for schedule, retention, encryption and notification settings that wrap these destinations, and [sources](sources.md) for what each snapshot contains.
