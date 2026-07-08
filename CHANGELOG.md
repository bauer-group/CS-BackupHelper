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
