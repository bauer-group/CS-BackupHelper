"""Tests for scheduler trigger construction (both schedule input styles)."""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backuphelper.config.models import ScheduleConfig
from backuphelper.scheduler import build_trigger


def test_interval_mode_builds_interval_trigger():
    trig = build_trigger(ScheduleConfig(mode="interval", interval_hours=6), "UTC")
    assert isinstance(trig, IntervalTrigger)
    assert trig.interval.total_seconds() == 6 * 3600


def test_cron_string_builds_cron_trigger():
    trig = build_trigger(ScheduleConfig(mode="cron", cron="15 3 * * *"), "UTC")
    assert isinstance(trig, CronTrigger)
    fields = {f.name: str(f) for f in trig.fields}
    assert fields["hour"] == "3" and fields["minute"] == "15"


def test_field_based_schedule_builds_cron_trigger():
    trig = build_trigger(
        ScheduleConfig(mode="cron", hour="4", minute="30", day_of_week="mon,wed"), "UTC"
    )
    assert isinstance(trig, CronTrigger)
    fields = {f.name: str(f) for f in trig.fields}
    assert fields["hour"] == "4" and fields["minute"] == "30"
