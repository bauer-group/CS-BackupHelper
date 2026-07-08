"""Scheduler — APScheduler cron/interval, accepting both schedule input styles.

Accepts a raw 5-field cron string OR field-based (hour/minute/day_of_week), plus
a fixed-interval mode. Uses coalesce + max_instances=1 + a misfire grace so a
missed/overlapping run never piles up, and drains cleanly on SIGTERM/SIGINT.
"""

from __future__ import annotations

import logging
import signal
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config.models import ScheduleConfig

log = logging.getLogger(__name__)


def install_signal_drain(sched, *, register: Callable = signal.signal) -> Callable:
    """Install SIGTERM/SIGINT handlers that stop the scheduler and WAIT for the
    running job to finish before exiting (``shutdown(wait=True)``).

    Without this, ``docker compose stop`` (SIGTERM) hard-kills the daemon at the
    stop grace period with a backup mid-run — a partial upload with no clean
    abort. Returns the installed handler (for testing)."""
    def _drain(signum=None, frame=None) -> None:
        log.info("received signal %s — draining the running job before shutdown", signum)
        sched.shutdown(wait=True)

    register(signal.SIGTERM, _drain)
    register(signal.SIGINT, _drain)
    return _drain


def build_trigger(cfg: ScheduleConfig, timezone: str):
    if cfg.mode == "interval":
        return IntervalTrigger(hours=cfg.interval_hours, timezone=timezone)
    if cfg.hour is not None or cfg.minute is not None or cfg.day_of_week is not None:
        return CronTrigger(
            minute=cfg.minute or "*",
            hour=cfg.hour or "*",
            day_of_week=cfg.day_of_week or "*",
            timezone=timezone,
        )
    return CronTrigger.from_crontab(cfg.cron, timezone=timezone)


def build_scheduler(cfg: ScheduleConfig, timezone: str, run_job: Callable[[], object]) -> BlockingScheduler:
    sched = BlockingScheduler(timezone=timezone)

    def _guarded() -> None:
        try:
            run_job()
        except Exception:  # noqa: BLE001 - one bad run must not kill the daemon
            log.exception("scheduled backup run failed")

    sched.add_job(_guarded, trigger=build_trigger(cfg, timezone), id="backup",
                  coalesce=True, misfire_grace_time=3600, max_instances=1)
    if cfg.on_startup:
        sched.add_job(_guarded, trigger="date", run_date=datetime.now(), id="startup")
    return sched


def run(sched: BlockingScheduler) -> None:
    install_signal_drain(sched)
    log.info("scheduler started")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown(wait=True)  # drain the running job rather than abandon it
