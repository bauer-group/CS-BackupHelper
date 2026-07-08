"""Tests for CLI command-plugin discovery (the ``backuphelper.commands`` group).

Consuming repos register a ``typer.Typer`` group here to add app-specific
operator subcommands (e.g. NocoDB's restore-schema/records/attachments) without
forking the engine.
"""

import typer

from backuphelper.plugins.commands import register_command_plugins


def _group(cmd_name: str) -> typer.Typer:
    t = typer.Typer()

    @t.command(cmd_name)
    def _cmd() -> None:  # pragma: no cover - never invoked in these tests
        ...

    return t


def test_register_mounts_plugin_group():
    app = typer.Typer()
    mounted = register_command_plugins(app, load=lambda: [("nocodb", _group("restore-schema"))])
    assert mounted == ["nocodb"]
    assert "nocodb" in [g.name for g in app.registered_groups]


def test_factory_callable_is_supported():
    # An entry point may resolve to a zero-arg factory returning the Typer group.
    app = typer.Typer()
    mounted = register_command_plugins(app, load=lambda: [("n8n", lambda: _group("import-workflow"))])
    assert mounted == ["n8n"]
    assert "n8n" in [g.name for g in app.registered_groups]


def test_non_typer_and_broken_are_skipped():
    app = typer.Typer()
    mounted = register_command_plugins(app, load=lambda: [("ok", _group("x")), ("bad", object())])
    assert mounted == ["ok"]  # a non-Typer plugin must not break the CLI
