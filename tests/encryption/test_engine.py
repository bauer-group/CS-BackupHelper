"""Tests for optional client-side encryption-at-rest via age/gpg subprocess."""

from subprocess import CompletedProcess

import pytest

from backuphelper.encryption.engine import EncryptionError, decrypt, encrypt


class FakeRun:
    """Records the argv it was called with and returns a canned success."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.argv: list[str] | None = None
        self.calls = 0
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **kw):
        self.argv = argv
        self.calls += 1
        return CompletedProcess(argv, self._returncode, b"", self._stderr)


def test_mode_none_returns_input_and_never_calls_run(tmp_path):
    src = tmp_path / "archive.tar"
    src.write_bytes(b"data")
    out = tmp_path / "archive.tar.enc"
    fake = FakeRun()

    result = encrypt(src, out, mode="none", run=fake)

    assert result == src
    assert fake.calls == 0


def test_age_encrypt_builds_expected_argv_and_returns_out(tmp_path):
    src = tmp_path / "archive.tar"
    out = tmp_path / "archive.tar.age"
    fake = FakeRun()

    result = encrypt(src, out, mode="age", recipient="age1abc", run=fake)

    assert result == out
    assert fake.argv == [
        "age",
        "--encrypt",
        "--recipient",
        "age1abc",
        "--output",
        str(out),
        str(src),
    ]


def test_gpg_encrypt_builds_expected_argv_and_returns_out(tmp_path):
    src = tmp_path / "archive.tar"
    out = tmp_path / "archive.tar.gpg"
    fake = FakeRun()

    result = encrypt(src, out, mode="gpg", recipient="key@example.com", run=fake)

    assert result == out
    assert fake.argv == [
        "gpg",
        "--batch",
        "--yes",
        "--encrypt",
        "--recipient",
        "key@example.com",
        "--output",
        str(out),
        str(src),
    ]


@pytest.mark.parametrize("mode", ["age", "gpg"])
def test_missing_recipient_raises_encryption_error(tmp_path, mode):
    src = tmp_path / "archive.tar"
    out = tmp_path / "archive.tar.enc"
    fake = FakeRun()

    with pytest.raises(EncryptionError):
        encrypt(src, out, mode=mode, recipient=None, run=fake)

    assert fake.calls == 0


def test_unknown_mode_raises_encryption_error(tmp_path):
    src = tmp_path / "archive.tar"
    out = tmp_path / "archive.tar.enc"
    fake = FakeRun()

    with pytest.raises(EncryptionError):
        encrypt(src, out, mode="rot13", recipient="x", run=fake)

    assert fake.calls == 0


def test_nonzero_returncode_raises_with_stderr(tmp_path):
    src = tmp_path / "archive.tar"
    out = tmp_path / "archive.tar.age"
    fake = FakeRun(returncode=2, stderr=b"age: no such recipient")

    with pytest.raises(EncryptionError) as excinfo:
        encrypt(src, out, mode="age", recipient="age1abc", run=fake)

    assert "age: no such recipient" in str(excinfo.value)


def test_age_decrypt_builds_expected_argv_and_returns_out(tmp_path):
    src = tmp_path / "archive.tar.age"
    out = tmp_path / "archive.tar"
    fake = FakeRun()

    result = decrypt(src, out, mode="age", run=fake)

    assert result == out
    assert fake.argv == [
        "age",
        "--decrypt",
        "--output",
        str(out),
        str(src),
    ]


def test_gpg_decrypt_builds_expected_argv_and_returns_out(tmp_path):
    src = tmp_path / "archive.tar.gpg"
    out = tmp_path / "archive.tar"
    fake = FakeRun()

    result = decrypt(src, out, mode="gpg", run=fake)

    assert result == out
    assert fake.argv == [
        "gpg",
        "--batch",
        "--yes",
        "--decrypt",
        "--output",
        str(out),
        str(src),
    ]
