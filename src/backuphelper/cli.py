"""Command-line interface (Typer).

    backuphelper                 scheduler daemon (default)
    backuphelper --now           run every job once and exit
    backuphelper create|list|show|verify|restore|prune|download|config|healthcheck
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from .config.loader import ConfigError, load_config
from .config.models import Job, RootConfig
from .healthcheck import check as check_health
from .integrity.hashing import sha256_file
from .logging_setup import redact_data, setup_logging
from .notify.manager import AlertManager
from .plugins.commands import register_command_plugins
from .plugins.hooks import discover_hooks
from .runner import (
    JobResult,
    _hydrate_from_destinations,
    remote_snapshot_ids,
    restore_snapshot,
    run_job,
)
from .snapshots import LOCAL, job_slug, parse_snapshot_id, pending_owners, scope_for
from .state import record_daemon_start

app = typer.Typer(add_completion=False, help="BAUER GROUP central backup engine")
log = logging.getLogger(__name__)


# ── shared helpers (pure, unit-testable) ─────────────────────────────────────
def data_dir() -> Path:
    return Path(os.environ.get("BACKUP_DATA_DIR", "/data"))


def _run_one(job: Job, cfg: RootConfig, dd: Path) -> JobResult:
    notifier = AlertManager(job.notifications)
    return run_job(job, data_dir=dd, instance_name=cfg.instance_name, notifier=notifier,
                   hooks=discover_hooks(), scope=scope_for(cfg.jobs, job))


def run_all_now(cfg: RootConfig, dd: Path) -> int:
    """Run every job once. Exit code 1 if any job ended in error."""
    worst_ok = True
    for job in cfg.jobs:
        result = _run_one(job, cfg, dd)
        worst_ok = worst_ok and result.status != "error"
    return 0 if worst_ok else 1


def find_artifact(dd: Path, snapshot_id: str) -> Optional[Path]:
    matches = sorted(dd.glob(f"{snapshot_id}.tar.gz*"))
    return matches[0] if matches else None


def list_snapshots(dd: Path) -> list[tuple[str, int]]:
    rows = []
    for manifest in sorted(dd.glob("*.manifest.json")):
        sid = manifest.name[: -len(".manifest.json")]
        artifact = find_artifact(dd, sid)
        rows.append((sid, artifact.stat().st_size if artifact else 0))
    return rows


def verify_snapshot(dd: Path, snapshot_id: str) -> bool:
    manifest_path = dd / f"{snapshot_id}.manifest.json"
    artifact = find_artifact(dd, snapshot_id)
    if not manifest_path.exists() or artifact is None:
        return False
    expected = json.loads(manifest_path.read_text()).get("archive_sha256")
    return bool(expected) and sha256_file(artifact) == expected


# ── daemon ───────────────────────────────────────────────────────────────────
def run_daemon(cfg: RootConfig, dd: Path) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from .scheduler import build_trigger, install_signal_drain

    try:  # starts the healthcheck's grace for "no backup yet" - before any job runs
        record_daemon_start(dd)
    except OSError as exc:  # the healthcheck reports an unwritable data dir itself
        log.error("could not record the daemon start in %s: %s", dd, exc)
    tz = os.environ.get("TZ", "Etc/UTC")
    sched = BlockingScheduler(timezone=tz)
    for job in cfg.jobs:
        def _job(job: Job = job) -> None:
            try:
                _run_one(job, cfg, dd)
            except Exception:  # noqa: BLE001 - never let one run kill the daemon
                log.exception("scheduled run for job %s failed", job.name)

        sched.add_job(_job, trigger=build_trigger(job.schedule, tz), id=f"job:{job.name}",
                      coalesce=True, misfire_grace_time=3600, max_instances=1)
        if job.schedule.on_startup:
            sched.add_job(_job, trigger="date", run_date=datetime.now(), id=f"startup:{job.name}")
    install_signal_drain(sched)  # SIGTERM/SIGINT drain the running job (docker stop)
    log.info("scheduler started for %d job(s)", len(cfg.jobs))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown(wait=True)


# ── commands ─────────────────────────────────────────────────────────────────
@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context, now: bool = typer.Option(False, "--now", help="run once and exit")) -> None:
    if ctx.invoked_subcommand is not None:
        return
    cfg = load_config()
    setup_logging(os.environ.get("BACKUP_LOG_LEVEL", "INFO"), os.environ.get("BACKUP_LOG_FORMAT", "console"))
    if now:
        raise typer.Exit(run_all_now(cfg, data_dir()))
    run_daemon(cfg, data_dir())


@app.command()
def create() -> None:
    """Run every job once now."""
    setup_logging()
    raise typer.Exit(run_all_now(load_config(), data_dir()))


@app.command("list")
def list_cmd(job: Optional[str] = typer.Option(None, "--job")) -> None:
    """List snapshots — local plus any that live only in the off-site S3 target."""
    dd = data_dir()
    sizes = dict(list_snapshots(dd))
    target = _pick_job(load_config(), job)
    if target is not None:
        for sid in remote_snapshot_ids(target):
            sizes.setdefault(sid, 0)  # 0 bytes = present off-site only, not local
    if not sizes:
        typer.echo("no snapshots found")
        return
    width = max([24] + [len(sid) for sid in sizes])  # job-scoped ids are longer
    for sid in sorted(sizes):
        size = sizes[sid]
        where = "" if size else "  (off-site only)"
        typer.echo(f"{sid:{width}s} {size:>12d} bytes{where}")


@app.command()
def show(snapshot_id: str) -> None:
    """Show a snapshot's manifest."""
    manifest = data_dir() / f"{snapshot_id}.manifest.json"
    if not manifest.exists():
        typer.echo(f"snapshot {snapshot_id} not found")
        raise typer.Exit(1)
    typer.echo(manifest.read_text())


@app.command()
def verify(snapshot_id: str, job: Optional[str] = typer.Option(None, "--job")) -> None:
    """Verify a snapshot's archive against its manifest sha256 (pulls it from the
    off-site S3 target first if it is not on the local volume)."""
    target = _pick_job(load_config(), job, snapshot_id)
    if target is not None:
        _hydrate_from_destinations(target, data_dir(), snapshot_id)
    if verify_snapshot(data_dir(), snapshot_id):
        typer.echo(f"OK {snapshot_id}")
        raise typer.Exit(0)
    typer.echo(f"FAILED {snapshot_id}")
    raise typer.Exit(2)


def _pick_job(cfg: RootConfig, job_name: Optional[str],
              snapshot_id: Optional[str] = None) -> Optional[Job]:
    """The job named by --job; without it the job a job-scoped snapshot id
    names, else the first job."""
    if not cfg.jobs:
        return None
    if job_name is not None:
        return next((j for j in cfg.jobs if j.name == job_name), None)
    _, slug = parse_snapshot_id(snapshot_id) if snapshot_id else (None, None)
    owner = next((j for j in cfg.jobs if slug is not None and job_slug(j.name) == slug), None)
    return owner or cfg.jobs[0]


@app.command()
def restore(snapshot_id: str,
            force: bool = typer.Option(False, "--force", "-f", help="skip confirmation"),
            job: Optional[str] = typer.Option(None, "--job"),
            only: Optional[list[str]] = typer.Option(None, "--only", help="restore only these components")) -> None:
    """Restore a snapshot (DESTRUCTIVE — overwrites the live sources)."""
    setup_logging()
    target = _pick_job(load_config(), job, snapshot_id)
    if target is None:
        typer.echo("no matching job configured")
        raise typer.Exit(1)
    if not force and not typer.confirm(f"This OVERWRITES live data for job '{target.name}'. Proceed?"):
        typer.echo("aborted")
        raise typer.Exit(0)
    ok = restore_snapshot(target, data_dir=data_dir(), snapshot_id=snapshot_id,
                          only=set(only) if only else None, hooks=discover_hooks())
    typer.echo("restore complete" if ok else "restore finished with errors")
    raise typer.Exit(0 if ok else 1)


@app.command()
def download(snapshot_id: str, dest: Path = typer.Argument(..., help="target directory")) -> None:
    """Copy a snapshot's archive + manifest out of the data dir."""
    import shutil

    dd = data_dir()
    artifact = find_artifact(dd, snapshot_id)
    if artifact is None:
        typer.echo(f"snapshot {snapshot_id} not found")
        raise typer.Exit(1)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, dest / artifact.name)
    sidecar = dd / f"{snapshot_id}.manifest.json"
    if sidecar.exists():
        shutil.copy2(sidecar, dest / sidecar.name)
    typer.echo(f"downloaded {artifact.name} → {dest}")


@app.command()
def prune(keep: Optional[int] = typer.Option(None, "--keep"),
          dry_run: bool = typer.Option(False, "--dry-run"),
          job: Optional[str] = typer.Option(
              None, "--job", help="prune only this job's snapshots (default: every job's)")) -> None:
    """Apply retention to local snapshots: every job's own snapshots by its own
    policy, never another job's."""
    from .retention import Snapshot
    from .retention import manager as rm
    from .runner import parse_snapshot_timestamp

    dd = data_dir()
    cfg = load_config()
    if not cfg.jobs:
        typer.echo("no jobs configured")
        return
    targets = [j for j in cfg.jobs if job is None or j.name == job]
    if not targets:
        typer.echo(f"no job named {job!r} configured")
        raise typer.Exit(1)

    now = datetime.now(timezone.utc)
    sids = sorted(m.name[: -len(".manifest.json")] for m in dd.glob("*.manifest.json"))
    marked = pending_owners(dd)
    for target in targets:
        retention = target.retention
        if keep is not None:
            retention = retention.model_copy(update={"count": keep})
        scope = scope_for(cfg.jobs, target)
        # Parse the real timestamp from the id so age/GFS behave as in the daemon.
        snaps = [Snapshot(s, parse_snapshot_timestamp(s, now)) for s in sids
                 if scope.owns(s, LOCAL, marked.get(s))]
        for sid in sorted(rm.select_prunable(snaps, retention, now)):
            typer.echo(f"{'would prune' if dry_run else 'pruning'} {sid}")
            if not dry_run:
                for p in dd.glob(f"{sid}.*"):
                    p.unlink(missing_ok=True)


@app.command("config")
def config_cmd(action: str = typer.Argument("print"),
               show_secrets: bool = typer.Option(
                   False, "--show-secrets", help="print secrets in cleartext (default: redacted)"),
               redacted: bool = typer.Option(
                   False, "--redacted", hidden=True,
                   help="(deprecated) redaction is now the default")) -> None:
    """Print the fully-merged effective config. Secrets are REDACTED by default;
    pass --show-secrets to reveal them."""
    cfg = load_config()
    if show_secrets:
        typer.echo(cfg.model_dump_json(indent=2))
        return
    # Redact the parsed structure, not the rendered text: every credential key is
    # masked whatever its value type, and the output stays valid JSON.
    typer.echo(json.dumps(redact_data(cfg.model_dump(mode="json")), indent=2, ensure_ascii=False))


@app.command()
def healthcheck() -> None:
    """Exit 0 if backups work (every job's last run fresh and without failed
    components, or a fresh daemon still in its grace), 1 otherwise; prints the
    reason."""
    max_age = float(os.environ.get("BACKUP_HEALTHCHECK_MAX_AGE_HOURS", "26"))
    try:
        jobs = load_config().jobs
    except (ConfigError, OSError) as exc:
        # The daemon cannot start with this config either; judge the data dir
        # without it - all runs together, BACKUP_HEALTHCHECK_MAX_AGE_HOURS.
        typer.echo(f"config not loaded, all jobs are judged together: {exc}", err=True)
        jobs = []
    health = check_health(data_dir(), max_age, jobs=jobs)
    typer.echo(f"{'healthy' if health.healthy else 'unhealthy'}: {health.reason}")
    raise typer.Exit(0 if health.healthy else 1)


# Mount any app-specific subcommand groups a consuming repo registered under the
# ``backuphelper.commands`` entry-point group (e.g. NocoDB restore-* commands).
register_command_plugins(app)
