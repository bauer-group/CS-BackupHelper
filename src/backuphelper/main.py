"""Container entrypoint — delegates to the Typer CLI app.

  (default)  scheduler daemon    |  --now  run once  |  <command> …
"""

from __future__ import annotations

from .cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
