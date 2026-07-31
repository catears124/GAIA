from __future__ import annotations

import psycopg

from gaia import wait_for_database


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Connection:
    def execute(self, query: str):
        assert query == "SELECT 1"
        return self

    def fetchone(self):
        return (1,)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_wait_for_database_recovers_after_transient_failures(monkeypatch):
    attempts = 0
    clock = _Clock()

    def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise psycopg.OperationalError("database system is not accepting connections")
        return _Connection()

    monkeypatch.setattr(wait_for_database, "_database_url", lambda value: "postgresql://test")
    monkeypatch.setattr(wait_for_database.psycopg, "connect", connect)
    monkeypatch.setattr(wait_for_database.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(wait_for_database.time, "sleep", clock.sleep)
    monkeypatch.setattr(wait_for_database.random, "uniform", lambda a, b: 0.0)

    assert wait_for_database.wait_for_database(timeout_seconds=30, max_delay_seconds=4) == 0
    assert attempts == 3


def test_wait_for_database_exits_after_deadline(monkeypatch):
    clock = _Clock()
    connect_timeouts: list[int] = []

    def connect(*args, **kwargs):
        connect_timeouts.append(kwargs["connect_timeout"])
        raise psycopg.OperationalError("hot standby mode is disabled")

    monkeypatch.setattr(wait_for_database, "_database_url", lambda value: "postgresql://test")
    monkeypatch.setattr(wait_for_database.psycopg, "connect", connect)
    monkeypatch.setattr(wait_for_database.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(wait_for_database.time, "sleep", clock.sleep)
    monkeypatch.setattr(wait_for_database.random, "uniform", lambda a, b: 0.0)

    assert wait_for_database.wait_for_database(timeout_seconds=5, max_delay_seconds=30) == 1
    assert clock.value == 5
    assert connect_timeouts
    assert max(connect_timeouts) <= 5


def test_sqlstate_authentication_failures_are_permanent():
    error = psycopg.OperationalError("connection rejected")
    error._sqlstate = "28P01"
    assert wait_for_database.is_retryable_database_error(error) is False


def test_state_output_is_machine_readable(tmp_path):
    output = tmp_path / "github-output"
    wait_for_database._write_state_output(0, str(output))
    wait_for_database._write_state_output(1, str(output))
    wait_for_database._write_state_output(2, str(output))
    wait_for_database._write_state_output(9, str(output))

    assert output.read_text(encoding="utf-8").splitlines() == [
        "state=ready",
        "exit_code=0",
        "state=recovering",
        "exit_code=1",
        "state=invalid",
        "exit_code=2",
        "state=failed",
        "exit_code=9",
    ]
