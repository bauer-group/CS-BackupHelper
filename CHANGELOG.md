# Changelog

All notable changes to this project are documented here. This file is maintained
automatically by [semantic-release](https://github.com/semantic-release/semantic-release)
on every release to `main`.

## [1.8.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.7...v1.8.0) (2026-10-09)

### 🚀 Features

* **notify:** added implicit TLS (SMTPS) to the email channel ([7ecdfd5](https://github.com/bauer-group/CS-BackupHelper/commit/7ecdfd5a97b21c0db5c3a9decf0491c603ffb145))

### 🐛 Bug Fixes

* **notify:** bounded the SMTP session of the email channel by a timeout ([f1aa554](https://github.com/bauer-group/CS-BackupHelper/commit/f1aa5542406e4c26c4bef572227a55ba329d1cf3))
* **notify:** showed duration, size and error line breaks in HTML mails ([8e2f846](https://github.com/bauer-group/CS-BackupHelper/commit/8e2f846e30fe69b9cb1c20415249cc7d6005909d))

## [1.7.7](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.6...v1.7.7) (2026-10-08)

### 🐛 Bug Fixes

* **healthcheck:** stopped reporting failed or missing backups as healthy ([f2d52db](https://github.com/bauer-group/CS-BackupHelper/commit/f2d52db253099d72326d9a1f537b64000458539e))
* **runner:** reported a run with a failed component as error ([a528150](https://github.com/bauer-group/CS-BackupHelper/commit/a528150300f7dff4cafe0e6715f1feb925ec1ffe))

## [1.7.6](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.5...v1.7.6) (2026-10-08)

### 🐛 Bug Fixes

* **notify:** escaped run data in the HTML part of alert emails ([7e9597a](https://github.com/bauer-group/CS-BackupHelper/commit/7e9597a30475f0b8fd8652576eae4b800e998baa))

## [1.7.5](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.4...v1.7.5) (2026-10-06)

### 🐛 Bug Fixes

* **config:** kept secret values out of validation error texts ([51696ca](https://github.com/bauer-group/CS-BackupHelper/commit/51696ca6c20109d894f7cbf84ce12f374a1c5750))
* **logging:** redacted secret_key and other credential keys ([6d123a4](https://github.com/bauer-group/CS-BackupHelper/commit/6d123a4384284f188a953c6575395ee7437b3dbd))
* **notify:** dropped empty email recipients instead of sending to them ([f7249d2](https://github.com/bauer-group/CS-BackupHelper/commit/f7249d2bcf0eff262014572dcced2f8b2a44d90f))
* **restore:** failed fast on an --only name that cannot be restored ([6506bc9](https://github.com/bauer-group/CS-BackupHelper/commit/6506bc978baebb4651ff6cd8c8eda98519782c80))
* **runner:** degraded a failing destination instead of aborting the run ([d1d5072](https://github.com/bauer-group/CS-BackupHelper/commit/d1d5072dbd197c76ac81ac82c3c889d0f5a4e608))
* **runner:** kept the local copy unless an off-site upload succeeded ([1268224](https://github.com/bauer-group/CS-BackupHelper/commit/1268224bef392653da8295f6a8076c2008d8fd8c))
* **runner:** recorded a raising source as an errored manifest component ([85da3d7](https://github.com/bauer-group/CS-BackupHelper/commit/85da3d76eecc53d77e56104fc3fc881d11fd314a))
* **runner:** sent an error alert when a run aborts ([9ffe3d9](https://github.com/bauer-group/CS-BackupHelper/commit/9ffe3d948c32ad0a985b2e53dd14d74fc0bb8de6))
* **runner:** warned loudly when encryption fails and the snapshot ships unencrypted ([a377c11](https://github.com/bauer-group/CS-BackupHelper/commit/a377c1106b7cb41a2e03b863590204f727522a66))
* **sources:** backed up the readable part of a filesystem source and reported the rest ([eea5a73](https://github.com/bauer-group/CS-BackupHelper/commit/eea5a73e6c37524b8559d89f787c34aa3b9be123))
* **sources:** failed a filesystem source on an unreadable directory ([4e3c238](https://github.com/bauer-group/CS-BackupHelper/commit/4e3c2382c0dff9bac6f746f15ea2f3696c3ddaa2))
* **sources:** kept readable subdirs when one subdirs root is unreadable ([f21122b](https://github.com/bauer-group/CS-BackupHelper/commit/f21122b72e0e9a96d799c7750ec37c6ad86c8c85))

## [1.7.4](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.3...v1.7.4) (2026-10-02)

### 🔧 Maintenance

* **ci:** removed issue AI summary workflow ([a6839c2](https://github.com/bauer-group/CS-BackupHelper/commit/a6839c2aac248e7b53999cd50508c92944dbcc5b)), references [bauer-group/automation-templates#105](https://github.com/bauer-group/automation-templates/issues/105)
* **deps:** update base image python-alpine ([3523711](https://github.com/bauer-group/CS-BackupHelper/commit/352371125cf7675f6e666ee65008d1afcced4832))

## [1.7.3](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.2...v1.7.3) (2026-09-18)

### 🔧 Maintenance

* **deps:** update base image python-alpine ([5966745](https://github.com/bauer-group/CS-BackupHelper/commit/5966745c72a7b8722cba27f1bf5c2089269f043b))

## [1.7.2](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.1...v1.7.2) (2026-09-01)

## [1.7.1](https://github.com/bauer-group/CS-BackupHelper/compare/v1.7.0...v1.7.1) (2026-08-06)

### 🐛 Bug Fixes

* **ci:** added the missing permissions block ([f7bfce2](https://github.com/bauer-group/CS-BackupHelper/commit/f7bfce2ba1141e8f4a567fc52bfdc57c59183d91))

## [1.7.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.6.0...v1.7.0) (2026-07-08)

### 🚀 Features

* **postgres:** added exclude_table_data (dump structure, drop rows) ([4d1fc21](https://github.com/bauer-group/CS-BackupHelper/commit/4d1fc21b336849307a243d894df7c068a9106e99))

## [1.6.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.5.4...v1.6.0) (2026-07-08)

### 🚀 Features

* **hooks:** auto-discovered lifecycle hooks via the backuphelper.hooks group ([4dc9986](https://github.com/bauer-group/CS-BackupHelper/commit/4dc9986bd5a750fe1fa0ac68aa887e2f032d21a6))

### 🐛 Bug Fixes

* **cli:** redacted config output by default (--show-secrets to reveal) ([0fd917c](https://github.com/bauer-group/CS-BackupHelper/commit/0fd917c023a6e44d02c46508db7163e5e93671a4))
* **s3:** verified object size after a single-part put ([ade7186](https://github.com/bauer-group/CS-BackupHelper/commit/ade7186b67b8736ff9f11720695ef5e3499b45fd))

## [1.5.4](https://github.com/bauer-group/CS-BackupHelper/compare/v1.5.3...v1.5.4) (2026-07-08)

### 🐛 Bug Fixes

* **filesystem:** accepted a CSV string for subdirs / exclude ([775f4e9](https://github.com/bauer-group/CS-BackupHelper/commit/775f4e95ebf5cfc160c3cf0b2ad1eb8c9a4521e7))

## [1.5.3](https://github.com/bauer-group/CS-BackupHelper/compare/v1.5.2...v1.5.3) (2026-07-08)

### 🐛 Bug Fixes

* **s3:** streamed snapshot download to disk to avoid OOM ([9e27827](https://github.com/bauer-group/CS-BackupHelper/commit/9e27827e56b038a2372ed8c735448ff7a6664aae))
* **scheduler:** drained the running job on SIGTERM/SIGINT ([3697335](https://github.com/bauer-group/CS-BackupHelper/commit/3697335cf963256aed101210461a15af5468aefd))

## [1.5.2](https://github.com/bauer-group/CS-BackupHelper/compare/v1.5.1...v1.5.2) (2026-07-08)

### 🐛 Bug Fixes

* **runner:** matched restore components to sources by the source's own name ([b4b0219](https://github.com/bauer-group/CS-BackupHelper/commit/b4b02198a195c8bb312afffe8a767d19031e035f))

## [1.5.1](https://github.com/bauer-group/CS-BackupHelper/compare/v1.5.0...v1.5.1) (2026-07-08)

### 🐛 Bug Fixes

* **postgres:** failed loudly on a plain-SQL restore error ([3c09d06](https://github.com/bauer-group/CS-BackupHelper/commit/3c09d06fe06fbf9b136c23e7f870ad5c6a9abbf0))

## [1.5.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.4.0...v1.5.0) (2026-07-08)

### 🚀 Features

* **runner:** added a generic per-source enabled toggle ([4f37809](https://github.com/bauer-group/CS-BackupHelper/commit/4f378095ae0bad0be612fb11302b0a4aca4571d6))

## [1.4.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.3.0...v1.4.0) (2026-07-08)

### 🚀 Features

* **cli:** added a plugin command-injection extension point ([79c1857](https://github.com/bauer-group/CS-BackupHelper/commit/79c18570aca7a0d5d9756f4c8c1b3b74e8fc97f8))

## [1.3.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.2.0...v1.3.0) (2026-07-08)

### 🚀 Features

* **runner:** restored off-site S3 fetch + sha256 gate on restore ([e7c0691](https://github.com/bauer-group/CS-BackupHelper/commit/e7c0691a2e537416982e2d4d221dfc5fc59f791e))

## [1.2.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.1.0...v1.2.0) (2026-07-08)

### 🚀 Features

* **runner:** skipped an s3 source with no bucket (opt-in object storage) ([13c0767](https://github.com/bauer-group/CS-BackupHelper/commit/13c0767f05c64ddf4d989af0b18e0cfd995320af))

## [1.1.0](https://github.com/bauer-group/CS-BackupHelper/compare/v1.0.2...v1.1.0) (2026-07-07)

### 🚀 Features

* **config:** accepted a comma-separated string for notification channels ([8a3970e](https://github.com/bauer-group/CS-BackupHelper/commit/8a3970e91db07ff045dbc34fde168a344e34dcd9))

## [1.0.2](https://github.com/bauer-group/CS-BackupHelper/compare/v1.0.1...v1.0.2) (2026-07-07)

### 🐛 Bug Fixes

* **runner:** skipped an S3 destination with no bucket (local-only fallback) ([9954d40](https://github.com/bauer-group/CS-BackupHelper/commit/9954d4054975f069eb99afbe9b52d97e8d559ad8))

## [1.0.1](https://github.com/bauer-group/CS-BackupHelper/compare/v1.0.0...v1.0.1) (2026-07-07)

## 1.0.0 (2026-07-07)

### 🚀 Features

* central reusable backup engine (BackupHelper v1) ([87ed4b3](https://github.com/bauer-group/CS-BackupHelper/commit/87ed4b36365c255b2483480d121a18c95f2fa034))
