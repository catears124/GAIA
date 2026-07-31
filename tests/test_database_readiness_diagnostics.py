from __future__ import annotations

import json
from pathlib import Path

import pytest

from gaia import wait_for_database as readiness


class FakeDatabaseError(Exception):
    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _ReadyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query: str):
        return self

    def fetchone(self):
        return (1,)


def test_probe_emits_ready_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "_database_url", lambda _value: "postgresql://example")
    monkeypatch.setattr(readiness.psycopg, "connect", lambda *_args, **_kwargs: _ReadyConnection())

    result = readiness.probe_database(timeout_seconds=2, max_delay_seconds=1)

    assert result.exit_code == 0
    assert result.state == "ready"
    assert result.attempts == 1
    assert result.reason == "database accepted a read probe"


def test_probe_keeps_permanent_sqlstate_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "_database_url", lambda _value: "postgresql://example")
    error = readiness.psycopg.OperationalError("bad password")
    error.sqlstate = "28P01"
    monkeypatch.setattr(readiness.psycopg, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = readiness.probe_database(timeout_seconds=2, max_delay_seconds=1)

    assert result.exit_code == 2
    assert result.state == "invalid"
    assert result.sqlstate == "28P01"
    assert result.attempts == 1


def test_probe_does_not_mislabel_internal_failure_as_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "_database_url", lambda _value: "postgresql://example")
    monkeypatch.setattr(readiness.psycopg, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("driver exploded")))

    result = readiness.probe_database(timeout_seconds=2, max_delay_seconds=1)

    assert result.exit_code == 3
    assert result.state == "failed"
    assert result.reason == "driver exploded"


def test_outputs_include_reason_attempts_and_elapsed(tmp_path: Path) -> None:
    result = readiness.ReadinessResult(
        exit_code=1,
        state="recovering",
        attempts=4,
        elapsed_seconds=12.3456,
        reason="server is starting up",
        sqlstate="57P03",
    )
    github_output = tmp_path / "github-output"
    json_output = tmp_path / "readiness.json"

    readiness._write_outputs(result, str(github_output))
    readiness._write_json(result, str(json_output))

    text = github_output.read_text()
    assert "state=recovering" in text
    assert "exit_code=1" in text
    assert "attempts=4" in text
    assert "elapsed_seconds=12.346" in text
    assert "reason=server is starting up" in text
    assert "sqlstate=57P03" in text
    assert json.loads(json_output.read_text()) == {
        "attempts": 4,
        "elapsed_seconds": 12.3456,
        "exit_code": 1,
        "reason": "server is starting up",
        "sqlstate": "57P03",
        "state": "recovering",
    }


def test_json_write_is_atomic_and_creates_parent(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "result.json"
    result = readiness.ReadinessResult(0, "ready", 1, 0.1, "ok")

    readiness._write_json(result, str(destination))

    assert destination.is_file()
    assert not destination.with_suffix(".json.tmp").exists()
