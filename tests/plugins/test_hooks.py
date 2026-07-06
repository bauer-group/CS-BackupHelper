"""Tests for the lifecycle hook registry (opt-in, empty by default)."""

import pytest

from backuphelper.plugins.hooks import HookRegistry, PHASES


def test_phases_are_defined():
    assert set(PHASES) == {"pre_backup", "post_backup", "pre_dump", "post_dump",
                           "pre_restore", "post_restore"}


def test_run_is_noop_when_nothing_registered():
    HookRegistry().run("pre_dump", {"x": 1})  # must not raise


def test_registered_hook_receives_context():
    reg = HookRegistry()
    seen = []
    reg.register("pre_dump", lambda ctx: seen.append(ctx))
    reg.run("pre_dump", {"job": "main"})
    assert seen == [{"job": "main"}]


def test_hooks_run_in_registration_order():
    reg = HookRegistry()
    order = []
    reg.register("post_backup", lambda ctx: order.append("a"))
    reg.register("post_backup", lambda ctx: order.append("b"))
    reg.run("post_backup", None)
    assert order == ["a", "b"]


def test_registering_unknown_phase_raises():
    with pytest.raises(ValueError):
        HookRegistry().register("whenever", lambda ctx: None)


def test_a_raising_pre_restore_hook_aborts_by_propagating():
    reg = HookRegistry()
    reg.register("pre_restore", lambda ctx: (_ for _ in ()).throw(RuntimeError("key mismatch")))
    with pytest.raises(RuntimeError, match="key mismatch"):
        reg.run("pre_restore", None)
