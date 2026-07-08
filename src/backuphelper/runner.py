"""The backup runner — orchestrates one job end to end.

    sources.produce → hash components → embedded manifest → deterministic bundle
    → (optional encrypt) → sidecar manifest (with archive_sha256) → put to every
    destination → retention per destination → tri-state notify.

The runner is the only place that knows the whole pipeline; every step is a
generic building block, and the notifier is injected (any object with
``notify(AlertEvent)``) so the engine stays decoupled from the notify package.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import tempfile

from .archive.bundle import create_bundle, extract_bundle
from .archive.manifest import Component, Manifest, read_manifest, sidecar_path, write_manifest
from .config.models import DestinationSpec, Job, RetentionConfig, SourceSpec
from .destinations.base import Destination
from .destinations.local import LocalDestination
from .destinations.s3 import S3Destination
from .encryption.engine import decrypt, encrypt
from .integrity.hashing import sha256_file
from .notify.base import AlertEvent
from .plugins.hooks import HookRegistry
from .plugins.registry import build_source
from .retention import Snapshot
from .retention import manager as retention_manager

log = logging.getLogger(__name__)

_SID_FORMAT = "%Y-%m-%d_%H-%M-%S"
_ENCRYPT_SUFFIX = {"age": ".age", "gpg": ".gpg"}


class Notifier(Protocol):
    def notify(self, event: AlertEvent) -> None: ...


@dataclass
class JobResult:
    status: str  # success | warning | error
    snapshot_id: str
    archive: Optional[Path]
    total_bytes: int
    components: list[Component]
    errors: list[str] = field(default_factory=list)


def run_job(
    job: Job,
    *,
    data_dir: Path,
    instance_name: str,
    notifier: Optional[Notifier] = None,
    now: Optional[datetime] = None,
    snapshot_id: Optional[str] = None,
    hooks: Optional[HookRegistry] = None,
) -> JobResult:
    now = now or datetime.now(timezone.utc)
    sid = snapshot_id or now.strftime(_SID_FORMAT)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    work = data_dir / ".work" / sid
    staging = work / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    started = now
    errors: list[str] = []

    if hooks:
        hooks.run("pre_backup", {"job": job.name, "snapshot_id": sid})

    components = _produce(job, staging, errors)
    ok = [c for c in components if not c.error]

    embedded = Manifest.build(snapshot_id=sid, instance_name=instance_name,
                              components=components, created_at=now.isoformat())
    (staging / "manifest.json").write_text(embedded.model_dump_json(indent=2), encoding="utf-8")

    archive = work / f"{sid}.tar.gz"
    create_bundle(staging, archive)
    artifact = _maybe_encrypt(archive, job, work, sid, errors)

    manifest = Manifest.build(snapshot_id=sid, instance_name=instance_name, components=components,
                              created_at=now.isoformat(), archive_sha256=sha256_file(artifact))
    sidecar = work / f"{sid}.manifest.json"
    write_manifest(manifest, sidecar)

    destinations = _build_destinations(job.destinations, data_dir)
    _upload(destinations, artifact, sidecar, sid, errors)
    for dest in destinations:
        _apply_retention(dest, job.retention, now, errors)

    _maybe_drop_local(job, data_dir, artifact.name, sid, errors)

    shutil.rmtree(work, ignore_errors=True)
    try:  # remove the now-empty .work parent so it never pollutes the data dir
        (data_dir / ".work").rmdir()
    except OSError:
        pass

    status = "success" if not errors else ("warning" if ok else "error")
    stored_path = data_dir / artifact.name
    stored = stored_path if stored_path.exists() else None
    result = JobResult(status=status, snapshot_id=sid, archive=stored,
                       total_bytes=manifest.total_bytes, components=components, errors=errors)

    if notifier:
        notifier.notify(_event(job, instance_name, sid, status, manifest, errors, started, now))
    if hooks:
        hooks.run("post_backup", {"job": job.name, "snapshot_id": sid, "status": status})
    log.info("job %s snapshot %s finished: %s", job.name, sid, status)
    return result


_NESTED_TAR_KINDS = {"filesystem", "s3"}


def restore_snapshot(
    job: Job,
    *,
    data_dir: Path,
    snapshot_id: str,
    only: Optional[set[str]] = None,
    hooks: Optional[HookRegistry] = None,
) -> bool:
    """Restore a snapshot: decrypt → extract → per-source restore. Destructive."""
    data_dir = Path(data_dir)
    _hydrate_from_destinations(job, data_dir, snapshot_id)
    artifact = _find_artifact(data_dir, snapshot_id)
    sidecar = data_dir / f"{snapshot_id}.manifest.json"
    if artifact is None or not sidecar.exists():
        log.error("snapshot %s not found (artifact or manifest missing)", snapshot_id)
        return False

    manifest = read_manifest(sidecar)
    if manifest.archive_sha256 and sha256_file(artifact) != manifest.archive_sha256:
        log.error("snapshot %s failed its sha256 integrity check — refusing to restore",
                  snapshot_id)
        return False
    specs = {_spec_component_name(s): s for s in job.sources}

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        bundle = _decrypt_if_needed(artifact, work)
        extracted = extract_bundle(bundle, work / "extracted")
        if hooks:
            hooks.run("pre_restore", {"job": job.name, "snapshot_id": snapshot_id,
                                      "extracted": extracted, "manifest": manifest})
        ok = True
        for comp in manifest.components:
            if comp.error or (only and comp.name not in only):
                continue
            spec = specs.get(comp.name)
            if spec is None:
                log.warning("no source config for component %s — skipped", comp.name)
                continue
            ok = _restore_component(spec, comp, extracted, work) and ok
        if hooks:
            hooks.run("post_restore", {"job": job.name, "snapshot_id": snapshot_id,
                                       "extracted": extracted, "manifest": manifest, "ok": ok})
    return ok


def _restore_component(spec: SourceSpec, comp: Component, extracted: Path, work: Path) -> bool:
    if comp.kind == "env":
        return True  # env snapshots are informational; not auto-applied
    try:
        source = build_source(spec.model_dump())
        if comp.kind in _NESTED_TAR_KINDS:
            nested = extracted / f"{comp.name}.tar.gz"
            comp_dir = extract_bundle(nested, work / f"c_{comp.name}")
            source.restore(comp_dir)
        else:  # db dumps live directly in the extracted dir
            source.restore(extracted)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("restore of component %s failed: %s", comp.name, exc)
        return False


def _decrypt_if_needed(artifact: Path, work: Path) -> Path:
    if artifact.suffix == ".age":
        out = work / artifact.with_suffix("").name
        return decrypt(artifact, out, mode="age")
    if artifact.suffix == ".gpg":
        out = work / artifact.with_suffix("").name
        return decrypt(artifact, out, mode="gpg")
    return artifact


def _find_artifact(data_dir: Path, snapshot_id: str) -> Optional[Path]:
    matches = sorted(data_dir.glob(f"{snapshot_id}.tar.gz*"))
    return matches[0] if matches else None


def _hydrate_from_destinations(job: Job, data_dir: Path, snapshot_id: str) -> None:
    """Off-site disaster recovery: when a snapshot's artifact + sidecar are gone
    from the local data dir, pull them back from the first S3 destination that
    holds them, so a restore works even after the local volume is lost. No-op
    when the snapshot is already present locally."""
    if _find_artifact(data_dir, snapshot_id) is not None and (
        data_dir / f"{snapshot_id}.manifest.json"
    ).exists():
        return
    manifest_key = f"{snapshot_id}.manifest.json"
    for spec in job.destinations:
        if spec.type != "s3":
            continue
        data = spec.model_dump(exclude={"type"})
        if not data.get("bucket"):
            continue
        data["ensure_bucket"] = False  # read-only path: never create a bucket on restore
        try:
            dest = S3Destination(data)
            keys = dest.list_keys(snapshot_id)
        except Exception as exc:  # noqa: BLE001 - a bad destination must not abort DR
            log.warning("s3 destination unavailable while hydrating %s: %s", snapshot_id, exc)
            continue
        archives = [k for k in keys if k.startswith(f"{snapshot_id}.tar.gz")]
        if not archives or manifest_key not in keys:
            continue
        for key in archives + [manifest_key]:
            dest.get(key, data_dir / key)
        log.info("hydrated snapshot %s from off-site s3 bucket %s", snapshot_id, data["bucket"])
        return


def remote_snapshot_ids(job: Job) -> set[str]:
    """Snapshot ids that exist in the job's off-site S3 destinations (by manifest
    key), so `list` can surface snapshots that are no longer on the local volume."""
    ids: set[str] = set()
    for spec in job.destinations:
        if spec.type != "s3":
            continue
        data = spec.model_dump(exclude={"type"})
        if not data.get("bucket"):
            continue
        data["ensure_bucket"] = False
        try:
            for key in S3Destination(data).list_keys():
                if key.endswith(".manifest.json"):
                    ids.add(key[: -len(".manifest.json")])
        except Exception as exc:  # noqa: BLE001 - a bad destination must not break list
            log.warning("could not list off-site s3 destination: %s", exc)
    return ids


def _spec_component_name(spec: SourceSpec) -> str:
    # Ask the source itself for the name it gives its component, so the restore
    # lookup can never diverge from what produce() actually wrote (a divergence
    # silently skips the component on restore). Fall back to the old heuristic if
    # the source cannot be built (unknown type / incomplete spec).
    try:
        return build_source(spec.model_dump()).component_name
    except Exception:  # noqa: BLE001
        extra = spec.model_extra or {}
        if extra.get("name"):
            return extra["name"]
        if spec.type in ("postgres", "mariadb", "mysql"):
            return extra.get("database") or extra.get("db") or "database"
        return spec.type


def _produce(job: Job, staging: Path, errors: list[str]) -> list[Component]:
    components: list[Component] = []
    for spec in job.sources:
        data = spec.model_dump()
        if not data.get("enabled", True):
            # Generic config-deactivation toggle: a source with "enabled": false is
            # simply not run (maps app include-toggles like BACKUP_INCLUDE_FILES /
            # BACKUP_DATABASE_DUMP=false onto the shared engine without hardcoding them).
            log.info("source %s disabled by config — skipping", data.get("name") or spec.type)
            continue
        if spec.type == "s3" and not data.get("bucket"):
            # "S3 source if configured, else skip" — an s3 source with no bucket is
            # simply not activated (mirrors the s3-destination behaviour), so a
            # DB-only deployment does not degrade to a partial warning every run.
            log.info("s3 source has no bucket configured — skipping (object storage not backed up)")
            continue
        try:
            source = build_source(data)
            staged = source.produce(staging)
        except Exception as exc:  # noqa: BLE001 - one bad source degrades to partial
            log.error("source %s failed: %s", spec.type, exc)
            errors.append(f"{spec.type}: {exc}")
            continue
        for sc in staged:
            if sc.error or not sc.path:
                errors.append(f"{sc.name}: {sc.error or 'no output'}")
                components.append(Component(name=sc.name, kind=sc.kind, size=0, sha256="",
                                            error=sc.error, metadata=sc.metadata))
            else:
                components.append(Component(name=sc.name, kind=sc.kind, size=sc.path.stat().st_size,
                                            sha256=sha256_file(sc.path), metadata=sc.metadata))
    return components


def _maybe_encrypt(archive: Path, job: Job, work: Path, sid: str, errors: list[str]) -> Path:
    mode = job.encryption.mode
    if mode == "none":
        return archive
    out = work / f"{sid}.tar.gz{_ENCRYPT_SUFFIX[mode]}"
    try:
        return encrypt(archive, out, mode=mode, recipient=job.encryption.recipient)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"encryption failed: {exc}")
        return archive


def _build_destinations(specs: list[DestinationSpec], data_dir: Path) -> list[Destination]:
    destinations: list[Destination] = []
    for spec in specs:
        if spec.type == "local":
            destinations.append(LocalDestination(data_dir))
        elif spec.type == "s3":
            data = spec.model_dump(exclude={"type"})
            if not data.get("bucket"):
                # "S3 if configured, else local" — an S3 target with no bucket is
                # simply not configured; skip it (keeps local-only deployments).
                log.info("s3 destination has no bucket configured — skipping (local-only)")
                continue
            destinations.append(S3Destination(data))
    if not destinations:
        destinations.append(LocalDestination(data_dir))  # never silently drop the backup
    return destinations


def _has_local(specs: list[DestinationSpec]) -> bool:
    return any(s.type == "local" for s in specs)


def _maybe_drop_local(job: Job, data_dir: Path, artifact_name: str, sid: str,
                      errors: list[str]) -> None:
    """Drop the local copy after a clean off-site upload when keep_local is off."""
    has_s3 = any(s.type == "s3" for s in job.destinations)
    if job.keep_local or not has_s3 or not _has_local(job.destinations):
        return
    if any("upload failed" in e for e in errors):
        return  # keep local as a safety net when off-site upload had trouble
    (data_dir / artifact_name).unlink(missing_ok=True)
    (data_dir / f"{sid}.manifest.json").unlink(missing_ok=True)


def _upload(destinations: list[Destination], artifact: Path, sidecar: Path, sid: str,
            errors: list[str]) -> None:
    for dest in destinations:
        try:
            dest.put(artifact, artifact.name)
            dest.put(sidecar, f"{sid}.manifest.json")
        except Exception as exc:  # noqa: BLE001
            log.error("upload to %s failed: %s", type(dest).__name__, exc)
            errors.append(f"upload failed: {exc}")


def _apply_retention(dest: Destination, cfg: RetentionConfig, now: datetime,
                     errors: list[str]) -> None:
    try:
        # Only top-level artifacts are snapshots; ignore any nested staging keys.
        sids = sorted({k[: -len(".manifest.json")] for k in dest.list_keys()
                       if k.endswith(".manifest.json") and "/" not in k})
        snapshots = [Snapshot(s, parse_snapshot_timestamp(s, now)) for s in sids]
        for pruned in retention_manager.select_prunable(snapshots, cfg, now):
            for key in list(dest.list_keys(prefix=f"{pruned}.")):
                dest.delete(key)
    except Exception as exc:  # noqa: BLE001
        log.error("retention on %s failed: %s", type(dest).__name__, exc)
        errors.append(f"retention failed: {exc}")


def parse_snapshot_timestamp(sid: str, fallback: datetime) -> datetime:
    try:
        return datetime.strptime(sid, _SID_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def _event(job: Job, instance: str, sid: str, status: str, manifest: Manifest,
           errors: list[str], started: datetime, finished: datetime) -> AlertEvent:
    message = "snapshot completed" if status == "success" else "snapshot completed with errors"
    return AlertEvent(
        status=status, title=f"backup {status}", message=message, instance=instance,
        snapshot_id=sid, job=job.name, total_bytes=manifest.total_bytes,
        duration_seconds=max(0.0, (finished - started).total_seconds()), errors=list(errors),
    )
