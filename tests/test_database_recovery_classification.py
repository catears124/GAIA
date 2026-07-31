from __future__ import annotations

import psycopg

from gaia.wait_for_database import is_retryable_database_error


def test_transient_database_errors_are_retryable() -> None:
    assert is_retryable_database_error(
        psycopg.OperationalError("the database system is starting up")
    )
    assert is_retryable_database_error(
        psycopg.OperationalError("connection timed out")
    )
    assert is_retryable_database_error(
        psycopg.OperationalError("server closed the connection unexpectedly")
    )


def test_invalid_connection_configuration_fails_fast() -> None:
    assert not is_retryable_database_error(
        psycopg.ProgrammingError('invalid URI query parameter: "supa"')
    )
    assert not is_retryable_database_error(
        psycopg.ProgrammingError('invalid connection option "dashboard_tag"')
    )
    assert not is_retryable_database_error(
        psycopg.OperationalError('could not translate host name "bad host"')
    )
