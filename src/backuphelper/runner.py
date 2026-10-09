"""The backup runner — orchestrates one job end to end.

    sources.produce → hash components → embedded manifest → deterministic bundle
    → (optional encrypt) → sidecar manifest (with archive_sha256) → put to every
    destination → retention per destination → tri-state notify.

The runner is the only place that knows the whole pipeline; every step is a
generic building block, and the notifier is injected (any object with
``notify(AlertEvent)``) so the engine stays decoupled from the notify package.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import tempfile

from pydantic import ValidationError

from .archive.bundle import create_bundle, extract_bundle
from .archive.manifest import Component, Manifest, read_manifest, sidecar_path, write_manifest
from .config.models import DestinationSpec, Job, RetentionConfig, SourceSpec
from .destinations.base import Destination
from .destinations.local import LocalDestination
from .destinations.s3 import S3Destination
from .encryption.engine import decrypt, encrypt
from .integrity.hashing import sha256_file
from .logging_setup import redact
from .notify.base import AlertEvent
from .plugins.hooks import HookRegistry
from .plugins.registry import build_source
from .sources.base import StagedComponent
from .retention import Snapshot
from .retention import manager as retention_manager
from .snapshots import (
    LOCAL,
    PENDING_SUFFIX,
    SnapshotScope,
    parse_snapshot_id,
    pending_owners,
    s3_place,
)
from .state import RunRecord, record_run

log = logging.getLogger(__name__)

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


def job_status(components: list[Component], errors: list[str], *, stored: bool) -> str:
    """The outcome of one run (docs/cli.md#run-status):

    * ``error``   — the snapshot was stored nowhere, or a component failed
      completely (a failed pg_dump, a raising plugin or S3 source, a missing
      path): the snapshot lacks that data, so the run must not pass;
    * ``warning`` — everything was backed up, but something non-fatal went
      wrong (a source skipped unreadable files, an S3 destination failed while
      another copy exists, encryption fell back to plaintext, retention failed);
    * ``success`` — nothing to report.

    Disabled and unconfigured sources produce no component and do not count."""
    if not stored or any(c.error for c in components):
        return "error"
    return "warning" if errors else "success"


def _outcome(status: str, components: list[Component], stored: bool) -> str:
    """The alert message: one fixed text per outcome, never run data (the
    details are in the event's errors)."""
    if not stored:
        return "snapshot was not stored on any destination"
    if any(c.error for c in components):
        return "snapshot is incomplete - a component failed"
    return "snapshot completed with warnings" if status == "warning" else "snapshot completed"


def run_job(
    job: Job,
    *,
    data_dir: Path,
    instance_name: str,
    notifier: Optional[Notifier] = None,
    now: Optional[datetime] = None,
    snapshot_id: Optional[str] = None,
    hooks: Optional[HookRegistry] = None,
    scope: Optional[SnapshotScope] = None,
) -> JobResult:
    """Run ``job`` once. ``scope`` says how its snapshots are named among the
    configured jobs (snapshots.scope_for); without it the job is treated as
    the only one: plain timestamp ids."""
    now = now or datetime.now(timezone.utc)
    scope = scope or SnapshotScope(job.name)
    sid = snapshot_id or scope.new_id(now)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    work = data_dir / ".work" / sid
    staging = work / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    started = now
    errors: list[str] = []
    components: list[Component] = []

    try:
        if hooks:
            hooks.run("pre_backup", {"job": job.name, "snapshot_id": sid})

        components = _produce(job, staging, errors)

        embedded = Manifest.build(snapshot_id=sid, instance_name=instance_name,
                                  components=components, created_at=now.isoformat())
        (staging / "manifest.json").write_text(embedded.model_dump_json(indent=2), encoding="utf-8")

        archive = work / f"{sid}.tar.gz"
        create_bundle(staging, archive)
        artifact = _maybe_encrypt(archive, job, work, sid, errors)

        manifest = Manifest.build(snapshot_id=sid, instance_name=instance_name,
                                  components=components, created_at=now.isoformat(),
                                  archive_sha256=sha256_file(artifact))
        sidecar = work / f"{sid}.manifest.json"
        write_manifest(manifest, sidecar)

        delivery = _deliver(job, data_dir, artifact, sidecar, sid, errors)
        if not delivery.stored:
            log.error("snapshot %s was not stored on any destination", sid)
            errors.append("snapshot was not stored on any destination — this run left no copy")
        _track_offsite(job, data_dir, sid, delivery, errors)
        # Other jobs may store in the same places: every destination is pruned
        # of this job's own snapshots only (snapshots.SnapshotScope.owns).
        for dest in delivery.destinations:
            _apply_retention(dest, job.retention, now, errors, scope, data_dir)
        if delivery.fallback is not None:
            # Only this job's own fallback copies, never the whole data dir.
            _apply_retention(delivery.fallback, job.retention, now, errors, scope, data_dir,
                             only=set(_pending_ids(data_dir, job.name)))

        _maybe_drop_local(job, data_dir, artifact.name, sid, delivery.stored, errors)
    except Exception as exc:
        # An aborted run (a raising pre_backup gate, a full disk while bundling)
        # must not fail silently: send an error alert, then propagate as before.
        errors.append(f"run aborted: {type(exc).__name__}: {_describe(exc)}")
        _record_state(data_dir, job, sid, "error", started, components)
        if notifier:
            try:
                notifier.notify(_event(job, instance_name, sid, "error", 0, errors, started, now,
                                       message="run aborted before it finished"))
            except Exception:  # noqa: BLE001 - never mask the original failure
                log.exception("could not send the alert for aborted job %s", job.name)
        raise
    finally:
        # Always remove the staging area — also when a hook gate or the disk
        # aborts the run — so it never pollutes the data dir.
        shutil.rmtree(work, ignore_errors=True)
        try:  # and the now-empty .work parent
            (data_dir / ".work").rmdir()
        except OSError:
            pass

    stored_anywhere = bool(delivery.stored)
    status = job_status(components, errors, stored=stored_anywhere)
    stored_path = data_dir / artifact.name
    stored = stored_path if stored_path.exists() else None
    result = JobResult(status=status, snapshot_id=sid, archive=stored,
                       total_bytes=manifest.total_bytes, components=components, errors=errors)
    _record_state(data_dir, job, sid, status, started, components)

    if notifier:
        notifier.notify(_event(job, instance_name, sid, status, manifest.total_bytes, errors,
                               started, now,
                               message=_outcome(status, components, stored_anywhere)))
    if hooks:
        hooks.run("post_backup", {"job": job.name, "snapshot_id": sid, "status": status})
    log.info("job %s snapshot %s finished: %s", job.name, sid, status)
    return result


def _record_state(data_dir: Path, job: Job, sid: str, status: str, started: datetime,
                  components: list[Component]) -> None:
    """Leave this run's outcome for the healthcheck (backuphelper.state). It
    must never fail the run: the snapshot and the alert matter more, and a data
    dir that cannot take the record is reported by the healthcheck itself."""
    try:
        record_run(data_dir, RunRecord(
            job=job.name, snapshot_id=sid, status=status, started_at=started,
            failed_components=tuple(c.name for c in components if c.error)))
    except Exception as exc:  # noqa: BLE001
        log.error("could not record the outcome of job %s for the healthcheck: %s",
                  job.name, _describe(exc))


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
    if only and not _only_is_restorable(only, manifest, specs, job.name):
        return False

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


def _only_is_restorable(only: set[str], manifest: Manifest, specs: dict[str, SourceSpec],
                        job_name: str) -> bool:
    """Validate an ``only`` selection BEFORE any hook or component touches live
    data: a typo, a component that failed at backup time or one without a source
    in this job would otherwise be skipped silently and report success."""
    by_name = {c.name: c for c in manifest.components}
    problems = []
    for name in sorted(only):
        comp = by_name.get(name)
        if comp is None:
            problems.append(f"component {name!r} is not in snapshot {manifest.snapshot_id}")
        elif comp.error:
            problems.append(f"component {name!r} failed at backup time and holds no data: "
                            f"{comp.error}")
        elif name not in specs:
            problems.append(f"component {name!r} has no matching source in job {job_name!r}")
    if not problems:
        return True
    for problem in problems:
        log.error("cannot restore: %s", problem)
    valid = sorted(n for n, c in by_name.items() if not c.error and n in specs)
    log.error("nothing was restored — valid components of snapshot %s: %s",
              manifest.snapshot_id, ", ".join(valid) or "none")
    return False


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
        log.error("restore of component %s failed: %s", comp.name, _describe(exc))
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
            log.warning("s3 destination unavailable while hydrating %s: %s", snapshot_id,
                        _describe(exc))
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
            log.warning("could not list off-site s3 destination: %s", _describe(exc))
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
        if not _enabled(data.get("enabled", True)):
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
        before = set(staging.iterdir())
        try:
            source = build_source(data)
            staged = source.produce(staging)
        except Exception as exc:  # noqa: BLE001 - one bad source degrades to partial
            # Record the failure like a returned error (size 0, no hash) so the
            # manifest — and `show` — never silently omits a configured source,
            # and drop any half-written output so it is not shipped in the archive.
            name = _spec_component_name(spec)
            message = f"{type(exc).__name__}: {_describe(exc)}"
            log.error("source %s (%s) failed: %s", name, spec.type, message)
            errors.append(f"{name}: {message}")
            components.append(Component(name=name, kind=spec.type, size=0, sha256="",
                                        error=message))
            _remove_new_entries(staging, before)
            continue
        for sc in staged:
            if sc.error or not sc.path:
                # No file is a failure even without an error text: the component
                # holds no data, so it must not pass as a good (restorable) one.
                error = redact(sc.error) if sc.error else "no output"
                errors.append(f"{sc.name}: {error}")
                components.append(Component(name=sc.name, kind=sc.kind, size=0, sha256="",
                                            error=error, metadata=sc.metadata))
            else:
                components.append(Component(name=sc.name, kind=sc.kind, size=sc.path.stat().st_size,
                                            sha256=sha256_file(sc.path), metadata=sc.metadata))
                _report_source_warnings(sc, errors)
    return components


def _enabled(value: object) -> bool:
    """A source's ``enabled`` toggle. A discrete env override passes a number
    for a source's own key as text (config.loader), so a number given as text
    counts as that number did before ("0" disables); other text is truthy."""
    if isinstance(value, str):
        try:
            number = json.loads(value)
        except ValueError:
            return bool(value)
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            return bool(number)
    return bool(value)


def _report_source_warnings(sc: StagedComponent, errors: list[str]) -> None:
    """A source can report non-fatal problems in ``metadata["warnings"]`` (e.g.
    unreadable files it skipped): the component stays valid and restorable, but
    the job degrades to warning so the gap is logged and alerted, never silent."""
    warnings = [str(w) for w in (sc.metadata.get("warnings") or [])]
    if not warnings:
        return
    shown = "; ".join(warnings[:5]) + (f" (+{len(warnings) - 5} more in the manifest)"
                                       if len(warnings) > 5 else "")
    message = redact(f"incomplete, skipped: {shown}")
    log.warning("source %s is %s", sc.name, message)
    errors.append(f"{sc.name}: {message}")


def _remove_new_entries(directory: Path, before: set[Path]) -> None:
    for path in set(directory.iterdir()) - before:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _maybe_encrypt(archive: Path, job: Job, work: Path, sid: str, errors: list[str]) -> Path:
    mode = job.encryption.mode
    if mode == "none":
        return archive
    out = work / f"{sid}.tar.gz{_ENCRYPT_SUFFIX[mode]}"
    try:
        return encrypt(archive, out, mode=mode, recipient=job.encryption.recipient)
    except Exception as exc:  # noqa: BLE001
        # Availability over confidentiality (docs/encryption.md): the run still
        # stores the plaintext archive, but it must never do so silently.
        reason = _describe(exc)
        log.error("encryption (%s) failed: %s - snapshot %s is stored UNENCRYPTED on "
                  "every destination", mode, reason, sid)
        errors.append(f"encryption ({mode}) failed, snapshot stored UNENCRYPTED: {reason}")
        return archive


def _build_destinations(specs: list[DestinationSpec], data_dir: Path,
                        errors: list[str]) -> list[Destination]:
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
            try:  # builds the client and (ensure_bucket) heads/creates the bucket
                destinations.append(S3Destination(data))
            except Exception as exc:  # noqa: BLE001 - a bad target must not lose the snapshot
                reason = _describe(exc)
                log.error("s3 destination %r unavailable: %s", data["bucket"], reason)
                errors.append(f"s3 destination {data['bucket']!r} unavailable: {reason}")
    return destinations


@dataclass
class _Delivery:
    destinations: list[Destination]  # built from the job's own specs
    stored: list[Destination]  # every destination that stored this snapshot
    fallback: Optional[LocalDestination] = None  # set when the data dir only stood in


def _deliver(job: Job, data_dir: Path, artifact: Path, sidecar: Path, sid: str,
             errors: list[str]) -> _Delivery:
    """Build every destination and upload to it.

    A destination that cannot be built or written degrades the job to a warning
    while the others still receive the snapshot. With nothing configured at all
    the data dir is the silent default. If destinations ARE configured but none
    stored the snapshot, it is kept in the data dir as a fallback copy instead of
    being deleted with the work dir; _track_offsite ships it later."""
    failures: list[str] = []
    destinations = _build_destinations(job.destinations, data_dir, failures)
    if not destinations and not failures:
        destinations = [LocalDestination(data_dir)]  # nothing configured: local default
    stored = _upload(destinations, artifact, sidecar, sid, failures)
    fallback = None
    if not stored and not any(isinstance(d, LocalDestination) for d in destinations):
        fallback = LocalDestination(data_dir)
        stored = _upload([fallback], artifact, sidecar, sid, failures)
        if stored:
            log.warning("snapshot %s reached no destination — kept it in %s", sid, data_dir)
            failures.append("no destination stored the snapshot — kept it in the local data"
                            " dir until an off-site destination is reachable again")
    errors.extend(failures)
    return _Delivery(destinations, stored, fallback)


def _has_local(specs: list[DestinationSpec]) -> bool:
    return any(s.type == "local" for s in specs)


def _offsite_configured(job: Job) -> bool:
    return any(s.type == "s3" and (s.model_extra or {}).get("bucket") for s in job.destinations)


def _pending_ids(data_dir: Path, job_name: str) -> list[str]:
    """Snapshots of this job that are in the data dir but not yet off-site."""
    return [sid for sid, owner in pending_owners(data_dir).items() if owner == job_name]


def _track_offsite(job: Job, data_dir: Path, sid: str, delivery: _Delivery,
                   errors: list[str]) -> None:
    """Keep the off-site copy complete across an outage.

    When the job has an off-site (S3) target configured but no off-site
    destination stored this snapshot, its copy in the data dir is marked pending
    (``<id>.offsite-pending.json``, naming the job). The next run that reaches an
    off-site destination uploads every pending snapshot of the job there and
    drops the marker — and the local copy too when the job keeps none (S3-only,
    or keep_local=false). Retention and ``prune`` remove a marker together with
    its snapshot (same ``<id>.`` prefix)."""
    offsite = [d for d in delivery.stored if not isinstance(d, LocalDestination)]
    if not offsite:
        if _offsite_configured(job) and delivery.stored:
            (data_dir / f"{sid}{PENDING_SUFFIX}").write_text(
                json.dumps({"job": job.name}), encoding="utf-8")
        return
    keeps_local = job.keep_local and _has_local(job.destinations)
    for pending in _pending_ids(data_dir, job.name):
        marker = data_dir / f"{pending}{PENDING_SUFFIX}"
        artifact = _find_artifact(data_dir, pending)
        sidecar = data_dir / f"{pending}.manifest.json"
        if artifact is None or not sidecar.exists():
            marker.unlink(missing_ok=True)  # pruned or removed in the meantime
            continue
        failures: list[str] = []
        if len(_upload(offsite, artifact, sidecar, pending, failures)) < len(offsite):
            errors.extend(f"pending snapshot {pending}: {f}" for f in failures)
            continue  # stays pending, retried on the next run
        marker.unlink(missing_ok=True)
        if not keeps_local:
            artifact.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
        log.info("uploaded pending snapshot %s off-site", pending)


def _maybe_drop_local(job: Job, data_dir: Path, artifact_name: str, sid: str,
                      stored: list[Destination], errors: list[str]) -> None:
    """keep_local=false: drop the local copy, but only once an off-site destination
    has actually stored THIS snapshot. What counts is the upload result, not the
    configured specs — an S3 spec may be unconfigured (empty bucket) or may have
    failed, and deleting the local copy then deletes the only one."""
    if job.keep_local or not _has_local(job.destinations):
        return
    if not any(isinstance(d, LocalDestination) for d in stored):
        return  # the local put itself failed — nothing to drop
    if not any(not isinstance(d, LocalDestination) for d in stored):
        later = (" — kept the local copy; the next run that reaches an off-site"
                 " destination uploads it" if _offsite_configured(job)
                 else " (no S3 bucket configured) — kept the local copy")
        log.warning("keep_local is false but no off-site destination stored snapshot %s%s",
                    sid, later)
        errors.append(f"keep_local is false but no off-site destination stored the snapshot{later}")
        return
    (data_dir / artifact_name).unlink(missing_ok=True)
    (data_dir / f"{sid}.manifest.json").unlink(missing_ok=True)


def _upload(destinations: list[Destination], artifact: Path, sidecar: Path, sid: str,
            errors: list[str]) -> list[Destination]:
    """Put the archive + sidecar to every destination; return the ones that stored
    BOTH (a remote copy without its manifest cannot be listed, verified or
    hydrated). S3 puts verify the remote object size before they return."""
    stored: list[Destination] = []
    for dest in destinations:
        try:
            dest.put(artifact, artifact.name)
            dest.put(sidecar, f"{sid}.manifest.json")
        except Exception as exc:  # noqa: BLE001
            reason = _describe(exc)
            log.error("upload to %s failed: %s", type(dest).__name__, reason)
            errors.append(f"upload failed: {reason}")
            continue
        stored.append(dest)
    return stored


def _place(dest: Destination) -> str:
    if isinstance(dest, S3Destination):
        return s3_place(dest.cfg.endpoint, dest.cfg.bucket, dest.cfg.prefix)
    return LOCAL


def _apply_retention(dest: Destination, cfg: RetentionConfig, now: datetime,
                     errors: list[str], scope: SnapshotScope, data_dir: Path,
                     only: Optional[set[str]] = None) -> None:
    """Prune the job's own snapshots on ``dest`` - or only the ids in ``only``."""
    try:
        place = _place(dest)
        marked = pending_owners(data_dir) if place == LOCAL else {}
        # Only top-level artifacts are snapshots; ignore any nested staging keys.
        sids = sorted({k[: -len(".manifest.json")] for k in dest.list_keys()
                       if k.endswith(".manifest.json") and "/" not in k})
        mine = [s for s in sids
                if (s in only if only is not None else scope.owns(s, place, marked.get(s)))]
        snapshots = [Snapshot(s, parse_snapshot_timestamp(s, now)) for s in mine]
        for pruned in retention_manager.select_prunable(snapshots, cfg, now):
            for key in list(dest.list_keys(prefix=f"{pruned}.")):
                dest.delete(key)
    except Exception as exc:  # noqa: BLE001
        reason = _describe(exc)
        log.error("retention on %s failed: %s", type(dest).__name__, reason)
        errors.append(f"retention failed: {reason}")


def _describe(exc: BaseException) -> str:
    """An exception as text for the job errors, the alert, the manifest and the
    log. A pydantic ValidationError is rendered without its input values (a
    plugin's own config model may not hide them, and the value can be a secret),
    and the result is redacted like a log line (key=value pairs, user:pass@)."""
    if isinstance(exc, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'value'} - {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False))
        return redact(f"invalid {exc.title} config: {details}")
    return redact(str(exc))


def parse_snapshot_timestamp(sid: str, fallback: datetime) -> datetime:
    """When a snapshot was taken, from its id (plain or job-scoped); ``fallback``
    for an id the engine did not generate."""
    when, _ = parse_snapshot_id(sid)
    return when or fallback


def _event(job: Job, instance: str, sid: str, status: str, total_bytes: int,
           errors: list[str], started: datetime, finished: datetime,
           message: str) -> AlertEvent:
    return AlertEvent(
        status=status, title=f"backup {status}", message=message, instance=instance,
        snapshot_id=sid, job=job.name, total_bytes=total_bytes,
        duration_seconds=max(0.0, (finished - started).total_seconds()), errors=list(errors),
    )
