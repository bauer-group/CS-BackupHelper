BackupHelper can optionally encrypt each archive client-side — with [age](https://age-encryption.org) or GnuPG — before it is written to any destination, so off-site copies are unreadable without your private key.

## Overview

Encryption is per job and off by default. When enabled, it runs in the pipeline right after the deterministic `tar.gz` bundle is built and **before** the archive is uploaded to any destination:

```
sources → bundle (tar.gz) → encrypt (age | gpg) → sidecar manifest → upload → retention
```

The stored artifact gains a `.age` or `.gpg` suffix, and the manifest's `archive_sha256` is computed over the **encrypted** artifact. Both `age` and `gnupg` are installed in the container image, so no extra tooling is required.

See the [configuration](configuration.md) reference for where `encryption` sits inside a job.

## Configuration

```json
{
  "encryption": { "mode": "age", "recipient": "age1qz...publickey" }
}
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `none` \| `age` \| `gpg` | `none` | Encryption backend. `none` is a passthrough (archive stored as-is). |
| `recipient` | string | `null` | The public recipient. **Required** for `age` and `gpg` — encryption raises without it. |

- For **age**, `recipient` is an age public key (e.g. `age1qz...`).
- For **gpg**, `recipient` is a key id, fingerprint or email present in the encrypting keyring.

## What runs under the hood

The engine shells out to the CLI tools. The exact argument vectors are:

| Mode | Encrypt | Resulting suffix |
| --- | --- | --- |
| `age` | `age --encrypt --recipient <recipient> --output <out> <archive>` | `.age` |
| `gpg` | `gpg --batch --yes --encrypt --recipient <recipient> --output <out> <archive>` | `.gpg` |

| Mode | Decrypt (during restore) |
| --- | --- |
| `age` | `age --decrypt --output <out> <artifact>` |
| `gpg` | `gpg --batch --yes --decrypt --output <out> <artifact>` |

Decryption relies on the matching **private key** being available to the tool in the environment where restore runs — the secret keyring for `gpg`, the age identity for `age`.

## Restore auto-decrypts by suffix

Restore does not need to be told the encryption mode. It selects the decrypt backend from the artifact's file suffix:

- `*.tar.gz.age` → decrypted with `age`
- `*.tar.gz.gpg` → decrypted with `gpg`
- `*.tar.gz` → used as-is (no decryption)

So `backuphelper restore <snapshot_id>` transparently decrypts an encrypted snapshot before extracting it, provided the private key is present.

## Failure behavior

If encryption fails (tool missing, bad recipient, non-zero exit), the runner records an error, **falls back to storing the unencrypted archive**, and reports the job as a `warning` rather than aborting. Treat a warning status on an encryption-enabled job as a signal that the stored copy may be unencrypted, and check the logs.

## Key generation

### age

Generate an identity (keep the private half safe — it is what restores the backups):

```bash
age-keygen -o age-identity.txt
# Public key: age1qz9v...           <- use this as encryption.recipient
```

Set the public key as the recipient:

```json
{ "encryption": { "mode": "age", "recipient": "age1qz9v..." } }
```

At restore time, `age-identity.txt` (the private identity) must be available to the `age` CLI in the restore environment.

### gpg

Use an existing keypair, or generate one, then point `recipient` at a key present in the keyring:

```json
{ "encryption": { "mode": "gpg", "recipient": "ops@example.com" } }
```

The corresponding secret key must be in the keyring of whatever runs `backuphelper restore`.
