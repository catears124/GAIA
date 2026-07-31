from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

import psycopg

from .db_base import _database_url

_PERMANENT_CONFIGURATION_ERRORS = (
    "invalid uri query parameter",
    "invalid connection option",
    "missing \"=\" after",
    "invalid integer value",
    "invalid sslmode value",
    "could not translate host name",
    "password authentication failed",
    "role does not exist",
    "database does not exist",
    "no pg_hba.conf entry",
    "invalid port number",
)
_PERMANENT_SQLSTATES = {
    "28000",  # invalid_authorization_specification
    "28P01",  # invalid_password
    "3D000",  # invalid_catalog_name
}
_STATE_BY_EXIT_CODE = {0: "ready", 1: "recovering", 2: "invalid"}


def is_retryable_database_error(error: BaseException) -> bool:
    """Separate transient database recovery from broken connection configuration."""

    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in _PERMANENT_SQLSTATES:
        return False
    message = str(error).casefold()
    return not any(marker in message for marker in _PERMANENT_CONFIGURATION_ERRORS)


def _write_state_output(exit_code: int, output_path: str | None) -> None:
    """Publish a stable state for CI callers without forcing shell exit-code parsing."""

    if not output_path:
        return
    state = _STATE_BY_EXIT_CODE.get(exit_code, "failed")
    path = Path(output_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"state={state}\n")
        handle.write(f"exit_code={exit_code}\n")


def wait_for_database(*, timeout_seconds: int, max_delay_seconds: float) -> int:
    """Wait through transient Supabase failover/restart windows without stampeding it.

    Exit codes are intentionally stable for workflow callers: 0 means ready, 1 means
    transient recovery exceeded the deadline, and 2 means the connection configuration
    is invalid and retrying cannot help.
    """

    deadline = time.monotonic() + max(1, timeout_seconds)
    attempt = 0
    last_error = "database unavailable"
    try:
        url = _database_url(None)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        print(f"database configuration is invalid: {exc}", file=sys.stderr, flush=True)
        return 2

    while time.monotonic() < deadline:
        attempt += 1
        remaining_before_connect = max(0.0, deadline - time.monotonic())
        connect_timeout = max(
            1,
            min(15, max(1, int(max_delay_seconds)), max(1, int(remaining_before_connect))),
        )
        try:
            with psycopg.connect(
                url,
                connect_timeout=connect_timeout,
                application_name="gaia-readiness",
                prepare_threshold=None,
            ) as connection:
                connection.execute("SELECT 1").fetchone()
            print(f"database ready after {attempt} attempt(s)")
            return 0
        except psycopg.Error as exc:
            last_error = str(exc).splitlines()[0]
            if not is_retryable_database_error(exc):
                print(
                    f"database configuration is invalid: {last_error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            base = min(max_delay_seconds, 2 ** min(attempt, 5))
            delay = min(remaining, base + random.uniform(0.0, min(1.5, base / 3)))
            print(
                f"database not ready (attempt {attempt}): {last_error}; retrying in {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    print(
        f"database did not recover within {timeout_seconds}s: {last_error}",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait for PostgreSQL readiness")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("GAIA_DB_RECOVERY_TIMEOUT", "600")),
    )
    parser.add_argument(
        "--max-delay-seconds",
        type=float,
        default=float(os.getenv("GAIA_DB_RECOVERY_MAX_DELAY", "30")),
    )
    parser.add_argument(
        "--github-output",
        default=os.getenv("GITHUB_OUTPUT"),
        help="Append state and exit_code outputs for GitHub Actions callers",
    )
    args = parser.parse_args()
    exit_code = wait_for_database(
        timeout_seconds=args.timeout_seconds,
        max_delay_seconds=max(1.0, args.max_delay_seconds),
    )
    _write_state_output(exit_code, args.github_output)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
