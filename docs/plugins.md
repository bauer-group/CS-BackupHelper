The BackupHelper extension API: how a consuming repo adds app-specific backup logic without changing the engine — **Source plugins** (auto-discovered via entry points) and **lifecycle hooks** (opt-in phases the runner invokes).

## The principle

The engine knows _how_ to move bytes safely — hash, bundle deterministically,
encrypt, apply retention, upload, notify, restore. The consuming repo knows _what_
the bytes mean. The core therefore **never imports an application SDK**;
app-specific behaviour (an n8n CLI export, a NocoDB REST export, quiescing a
service, an `ENCRYPTION_KEY` cross-check before a destructive restore) lives in
the repo as a **Source plugin** or a **lifecycle hook**, never inside this engine.

## Source plugins

A source answers one question: _what to capture_. It dumps one backend into a
staging directory and returns the artifacts it produced; the engine does
everything else.

### The `Source` contract

`backuphelper.sources.base.Source` is an ABC:

```python
class Source(ABC):
    type: ClassVar[str] = ""                       # the config discriminator

    def __init__(self, spec: Mapping[str, Any]): ...

    @abstractmethod
    def produce(self, staging_dir: Path) -> list[StagedComponent]: ...

    def restore(self, staged_dir: Path) -> None:   # optional
        raise NotImplementedError(f"{self.type} source does not support restore")
```

- `type` is the string used in config (`{"type": "nocodb", ...}`) and the
  entry-point name.
- The constructor receives the source's config `spec`; the whole spec dict is
  kept on `self.spec` (config specs are open — extra keys are preserved — so a
  plugin validates its own fields, e.g. with a Pydantic model).
- `produce(staging_dir)` writes files into `staging_dir` and returns a list of
  `StagedComponent`. Report a failure by returning a component with `error=` set
  and `path=None` rather than raising, so one bad source degrades the job to a
  partial snapshot instead of aborting it.
- `restore(staged_dir)` is **optional**. Omit it and the base raises
  `NotImplementedError`; implement it for sources that can be restored.

`StagedComponent` is a dataclass:

```python
@dataclass
class StagedComponent:
    name: str                       # component name (also its restore key)
    kind: str                       # usually == self.type
    path: Optional[Path]            # the staged file, or None on failure
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None
```

Raise `backuphelper.sources.base.SourceError` from `restore` (or from `produce`
if you must fail hard) to signal a source-level failure.

### Complete minimal example

A plugin package that adds a `nocodb` source backing up a NocoDB base via its
REST API.

**1 — the source class** (`myapp_backup/nocodb_source.py`):

```python
from __future__ import annotations

from pathlib import Path

from backuphelper.sources.base import Source, StagedComponent, SourceError


class NocoDBSource(Source):
    type = "nocodb"

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        out = staging_dir / "nocodb.json"
        try:
            # self.spec holds the config keys from the job's source entry.
            data = _export_via_rest(self.spec["base_url"], self.spec["token"])
        except Exception as exc:  # degrade to a partial snapshot, don't abort
            return [StagedComponent(name="nocodb", kind=self.type, path=None,
                                    error=f"nocodb export failed: {exc}")]
        out.write_text(data, encoding="utf-8")
        return [StagedComponent(name="nocodb", kind=self.type, path=out,
                                metadata={"base_url": self.spec["base_url"]})]

    def restore(self, staged_dir: Path) -> None:
        payload = (Path(staged_dir) / "nocodb.json").read_text(encoding="utf-8")
        try:
            _import_via_rest(self.spec["base_url"], self.spec["token"], payload)
        except Exception as exc:
            raise SourceError(f"nocodb restore failed: {exc}") from exc
```

**2 — register it** under the `backuphelper.sources` entry-point group in the
plugin package's own `pyproject.toml`:

```toml
[project.entry-points."backuphelper.sources"]
nocodb = "myapp_backup.nocodb_source:NocoDBSource"
```

**3 — install it into the image** in the repo's meta-Dockerfile:

```dockerfile
FROM ghcr.io/bauer-group/cs-backuphelper/backuphelper:1
USER root
COPY myapp_backup/ /opt/myapp_backup/myapp_backup/
COPY pyproject.toml /opt/myapp_backup/
RUN pip install --no-cache-dir /opt/myapp_backup
USER backup
```

> **Discovery is by installed distribution metadata, not by import path.** The
> registry reads entry points with `importlib.metadata.entry_points(group="backuphelper.sources")`,
> so the plugin must be **`pip install`ed** (which registers the entry point) —
> merely dropping a file onto `PYTHONPATH` will not register it.

**4 — use it** in the job config (env or `BACKUP_CONFIG_JSON`):

```json
{ "type": "nocodb", "base_url": "http://nocodb:8080", "token": "${NOCODB_TOKEN}" }
```

### Discovery and precedence

`backuphelper.plugins.registry` resolves a source `type` to a class:

- `ENTRY_POINT_GROUP = "backuphelper.sources"`.
- `get_source_class(type_name)` returns a built-in if the name matches one,
  otherwise loads plugins from the entry-point group.
- `build_source(spec)` constructs the resolved class from the spec.
- **Built-ins win over plugins of the same name.** The built-in names are
  `postgres`, `mariadb`, `mysql`, `s3`, `filesystem`, `env` — do not shadow
  these; register your plugin under a distinct `type`.
- A plugin that fails to load is skipped, not fatal: a broken third-party plugin
  cannot break discovery of the others.

## Lifecycle hooks

Hooks are **opt-in** extension points the runner invokes around a job. The
zero-coupling online dump is the default — with **no hooks registered, nothing
runs**. A repo registers hooks to quiesce an app, run a pre-restore safety gate,
or do post-restore cleanup.

### The phases

`backuphelper.plugins.hooks.PHASES` defines six phases:

| Phase | When |
| --- | --- |
| `pre_backup` | before a job produces any source |
| `post_backup` | after a job finishes (context carries the final `status`) |
| `pre_dump` | reserved dump-level phase |
| `post_dump` | reserved dump-level phase |
| `pre_restore` | before any component is restored (a **gate**) |
| `post_restore` | after all components are restored |

The runner currently invokes `pre_backup` / `post_backup` around `run_job` and
`pre_restore` / `post_restore` around `restore_snapshot`; `pre_dump` / `post_dump`
are defined phases reserved for finer dump-level wiring.

### A raising hook aborts

`HookRegistry.run(phase, context)` calls each registered hook and **does not
swallow exceptions** — a raising hook propagates. So a `pre_*` hook is a gate: if
it raises, the operation stops before any destructive work. The canonical use is
a `pre_restore` `ENCRYPTION_KEY` cross-check that refuses to restore an archive
encrypted under a different key:

```python
import os

from backuphelper.plugins.hooks import HookRegistry


def guard_encryption_key(context) -> None:
    # Abort the restore unless the current key matches the one recorded at backup.
    current = os.environ.get("ENCRYPTION_KEY", "")
    expected = _key_fingerprint_from_manifest(context["snapshot_id"])
    if _fingerprint(current) != expected:
        raise RuntimeError(
            "ENCRYPTION_KEY does not match the snapshot's key — restore aborted"
        )


registry = HookRegistry()
registry.register("pre_restore", guard_encryption_key)
```

Because `pre_restore` fires before the per-component restore loop, a raise here
stops the restore before it overwrites any live data.

### Registering and running hooks

`HookRegistry` is the wiring point:

```python
registry = HookRegistry()
registry.register("pre_backup", quiesce_app)     # ValueError on an unknown phase
registry.register("post_backup", resume_app)
```

The runner accepts a registry programmatically:

```python
from backuphelper.runner import run_job, restore_snapshot

run_job(job, data_dir=dd, instance_name=name, hooks=registry)
restore_snapshot(job, data_dir=dd, snapshot_id=sid, hooks=registry)
```

Unlike Source plugins, hooks are **not** auto-discovered from entry points — they
are handed to the runner in code. A repo that needs hooks wires a small custom
entrypoint that builds the registry and drives the runner, keeping all
app-specific logic in the repo.

## See also

- [deployment.md](./deployment.md) — the meta-Dockerfile that installs a plugin.
- [migration.md](./migration.md) — which fleet repos need a plugin vs. plain config.
- [../README.md](../README.md) — built-in sources and configuration layers.
