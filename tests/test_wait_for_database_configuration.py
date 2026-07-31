from __future__ import annotations

import psycopg

from gaia import wait_for_database


def test_wait_for_database_rejects_missing_configuration(monkeypatch):
    def missing(value):
        raise RuntimeError("GAIA_DATABASE_URL is required")

    monkeypatch.setattr(wait_for_database, "_database_url", missing)

    assert wait_for_database.wait_for_database(timeout_seconds=600, max_delay_seconds=30) == 2


def test_wait_for_database_rejects_permanent_connection_error_without_retry(monkeypatch):
    attempts = 0

    def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise psycopg.OperationalError("role gaia does not exist")

    monkeypatch.setattr(wait_for_database, "_database_url", lambda value: "postgresql://test")
    monkeypatch.setattr(wait_for_database.psycopg, "connect", connect)

    assert wait_for_database.wait_for_database(timeout_seconds=600, max_delay_seconds=30) == 2
    assert attempts == 1


def test_retryability_classification_covers_transient_and_permanent_failures():
    assert wait_for_database.is_retryable_database_error(
        psycopg.OperationalError("database system is not accepting connections")
    )
    assert wait_for_database.is_retryable_database_error(
        psycopg.OperationalError("terminating connection due to administrator command")
    )
    assert not wait_for_database.is_retryable_database_error(
        psycopg.OperationalError("database gaia does not exist")
    )
    assert not wait_for_database.is_retryable_database_error(
        psycopg.OperationalError("no pg_hba.conf entry for host")
    )
