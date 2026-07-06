"""Optional client-side encryption-at-rest of an archive via age or gpg."""

from __future__ import annotations

import subprocess
from pathlib import Path


class EncryptionError(RuntimeError):
    """Raised when an encryption/decryption step is misconfigured or fails."""


def _run_checked(argv: list[str], run) -> None:
    """Invoke ``run`` with ``argv`` and raise ``EncryptionError`` on failure.

    The tool name (argv[0]) and stderr are surfaced for diagnostics; the
    recipient and other argv items are intentionally not repeated in the
    message to avoid leaking key identities into higher log levels.
    """
    proc = run(argv)
    if proc.returncode != 0:
        stderr = proc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise EncryptionError(
            f"{argv[0]} exited with {proc.returncode}: {stderr}".rstrip()
        )


def encrypt(
    path: Path,
    out: Path,
    *,
    mode: str,
    recipient: str | None = None,
    run=subprocess.run,
) -> Path:
    """Encrypt ``path`` into ``out`` using ``mode``; ``none`` is a passthrough."""
    if mode == "none":
        return path
    if mode in ("age", "gpg") and not recipient:
        raise EncryptionError(f"mode {mode!r} requires a recipient")
    if mode == "age":
        argv = [
            "age",
            "--encrypt",
            "--recipient",
            recipient,
            "--output",
            str(out),
            str(path),
        ]
        _run_checked(argv, run)
        return out
    if mode == "gpg":
        argv = [
            "gpg",
            "--batch",
            "--yes",
            "--encrypt",
            "--recipient",
            recipient,
            "--output",
            str(out),
            str(path),
        ]
        _run_checked(argv, run)
        return out
    raise EncryptionError(f"unknown encryption mode {mode!r}")


def decrypt(
    path: Path,
    out: Path,
    *,
    mode: str,
    run=subprocess.run,
) -> Path:
    """Decrypt ``path`` into ``out`` using ``mode``; ``none`` is a passthrough."""
    if mode == "none":
        return path
    if mode == "age":
        argv = ["age", "--decrypt", "--output", str(out), str(path)]
        _run_checked(argv, run)
        return out
    if mode == "gpg":
        argv = [
            "gpg",
            "--batch",
            "--yes",
            "--decrypt",
            "--output",
            str(out),
            str(path),
        ]
        _run_checked(argv, run)
        return out
    raise EncryptionError(f"unknown encryption mode {mode!r}")
