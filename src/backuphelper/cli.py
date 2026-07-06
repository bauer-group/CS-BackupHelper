"""Command-line interface (Typer).

    backuphelper                 scheduler daemon (default)
    backuphelper --now           run every job once and exit
    backuphelper create|list|show|verify|restore|prune|download|config|healthcheck
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from .config.loader import load_config
from .config.models import Job, RootConfig
from .healthcheck import is_healthy
from .integrity.hashing import sha256_file
from .logging_setup import redact, setup_logging
from .notify.manager import AlertManager
from .runner import JobResult, restore_snapshot, run_job

app = typer.Typer(add_completion=False, help="BAUER GROUP central backup engine")
log = logging.getLogger(__name__)


# ── shared helpers (pure, unit-testable) ─────────────────────────────────────
def data_dir() -> Path:
    return Path(os.environ.get("BACKUP_DATA_DIR", "/data"))


def _run_one(job: Job, instance_name: str, dd: Path) -> JobResult:
    notifier = AlertManager(job.notifications)
    return run_job(job, data_dir=dd, instance_name=instance_name, notifier=notifier)


def run_all_now(cfg: RootConfig, dd: Path) -> int:
    """Run every job once. Exit code 1 if any job ended in error."""
    worst_ok = True
    for job in cfg.jobs:
        result = _run_one(job, cfg.instance_name, dd)
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
    import json

    manifest_path = dd / f"{snapshot_id}.manifest.json"
    artifact = find_artifact(dd, snapshot_id)
    if not manifest_path.exists() or artifact is None:
        return False
    expected = json.loads(manifest_path.read_text()).get("archive_sha256")
    return bool(expected) and sha256_file(artifact) == expected


# ── daemon ───────────────────────────────────────────────────────────────────
def run_daemon(cfg: RootConfig, dd: Path) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from .scheduler import build_trigger

    tz = os.environ.get("TZ", "Etc/UTC")
    sched = BlockingScheduler(timezone=tz)
    for job in cfg.jobs:
        def _job(job: Job = job) -> None:
            try:
                _run_one(job, cfg.instance_name, dd)
            except Exception:  # noqa: BLE001 - never let one run kill the daemon
                log.exception("scheduled run for job %s failed", job.name)

        sched.add_job(_job, trigger=build_trigger(job.schedule, tz), id=f"job:{job.name}",
                      coalesce=True, misfire_grace_time=3600, max_instances=1)
        if job.schedule.on_startup:
            sched.add_job(_job, trigger="date", run_date=datetime.now(), id=f"startup:{job.name}")
    log.info("scheduler started for %d job(s)", len(cfg.jobs))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown(wait=False)


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
def list_cmd() -> None:
    """List local snapshots."""
    rows = list_snapshots(data_dir())
    if not rows:
        typer.echo("no snapshots found")
        return
    for sid, size in rows:
        typer.echo(f"{sid:24s} {size:>12d} bytes")


@app.command()
def show(snapshot_id: str) -> None:
    """Show a snapshot's manifest."""
    manifest = data_dir() / f"{snapshot_id}.manifest.json"
    if not manifest.exists():
        typer.echo(f"snapshot {snapshot_id} not found")
        raise typer.Exit(1)
    typer.echo(manifest.read_text())


@app.command()
def verify(snapshot_id: str) -> None:
    """Verify a snapshot's archive against its manifest sha256."""
    if verify_snapshot(data_dir(), snapshot_id):
        typer.echo(f"OK {snapshot_id}")
        raise typer.Exit(0)
    typer.echo(f"FAILED {snapshot_id}")
    raise typer.Exit(2)


def _pick_job(cfg: RootConfig, job_name: Optional[str]) -> Optional[Job]:
    if not cfg.jobs:
        return None
    if job_name is None:
        return cfg.jobs[0]
    return next((j for j in cfg.jobs if j.name == job_name), None)


@app.command()
def restore(snapshot_id: str,
            force: bool = typer.Option(False, "--force", "-f", help="skip confirmation"),
            job: Optional[str] = typer.Option(None, "--job"),
            only: Optional[list[str]] = typer.Option(None, "--only", help="restore only these components")) -> None:
    """Restore a snapshot (DESTRUCTIVE — overwrites the live sources)."""
    setup_logging()
    target = _pick_job(load_config(), job)
    if target is None:
        typer.echo("no matching job configured")
        raise typer.Exit(1)
    if not force and not typer.confirm(f"This OVERWRITES live data for job '{target.name}'. Proceed?"):
        typer.echo("aborted")
        raise typer.Exit(0)
    ok = restore_snapshot(target, data_dir=data_dir(), snapshot_id=snapshot_id,
                          only=set(only) if only else None)
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
          dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Apply retention to local snapshots."""
    from .retention import Snapshot
    from .retention import manager as rm

    dd = data_dir()
    cfg = load_config()
    retention = cfg.jobs[0].retention if cfg.jobs else None
    if retention is None:
        typer.echo("no jobs configured")
        return
    if keep is not None:
        retention = retention.model_copy(update={"count": keep})
    sids = sorted(m.name[: -len(".manifest.json")] for m in dd.glob("*.manifest.json"))
    snaps = [Snapshot(s, datetime.now(timezone.utc)) for s in sids]
    pruned = rm.select_prunable(snaps, retention, datetime.now(timezone.utc))
    for sid in sorted(pruned):
        typer.echo(f"{'would prune' if dry_run else 'pruning'} {sid}")
        if not dry_run:
            for p in dd.glob(f"{sid}.*"):
                p.unlink(missing_ok=True)


@app.command("config")
def config_cmd(action: str = typer.Argument("print"),
               redacted: bool = typer.Option(False, "--redacted")) -> None:
    """Print the fully-merged effective config (secrets masked with --redacted)."""
    cfg = load_config()
    text = cfg.model_dump_json(indent=2)
    typer.echo(redact(text) if redacted else text)


@app.command()
def healthcheck() -> None:
    """Exit 0 if the last backup is fresh, 1 otherwise."""
    max_age = float(os.environ.get("BACKUP_HEALTHCHECK_MAX_AGE_HOURS", "26"))
    raise typer.Exit(0 if is_healthy(data_dir(), max_age) else 1)
