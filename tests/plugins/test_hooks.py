"""Tests for the lifecycle hook registry (opt-in, empty by default)."""

import pytest

from backuphelper.plugins.hooks import HookRegistry, PHASES, discover_hooks


def test_discover_hooks_calls_each_plugin_register_fn():
    # A hooks plugin exposes a register(registry) callable under the
    # backuphelper.hooks entry-point group; discovery calls each one.
    calls = []

    def reg_a(registry):
        registry.register("pre_backup", lambda ctx: calls.append(("a", ctx)))

    def reg_b(registry):
        registry.register("pre_restore", lambda ctx: calls.append(("b", ctx)))

    reg = discover_hooks(load=lambda: [("a", reg_a), ("b", reg_b)])
    reg.run("pre_backup", {"x": 1})
    reg.run("pre_restore", {"y": 2})
    assert calls == [("a", {"x": 1}), ("b", {"y": 2})]


def test_discover_hooks_skips_a_broken_plugin():
    def good(registry):
        registry.register("pre_backup", lambda ctx: None)

    def bad(registry):
        raise RuntimeError("boom")

    reg = discover_hooks(load=lambda: [("good", good), ("bad", bad)])
    reg.run("pre_backup", None)  # good registered, bad skipped, no raise


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
