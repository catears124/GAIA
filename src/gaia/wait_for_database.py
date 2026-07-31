from __future__ import annotations

import argparse
import os
import random
import sys
import time

import psycopg

from .db_base import _database_url


def wait_for_database(*, timeout_seconds: int, max_delay_seconds: float) -> int:
    """Wait through transient Supabase failover/restart windows without stampeding it."""

    deadline = time.monotonic() + max(1, timeout_seconds)
    attempt = 0
    last_error = "database unavailable"
    url = _database_url(None)

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with psycopg.connect(
                url,
                connect_timeout=min(15, max(3, int(max_delay_seconds))),
                application_name="gaia-readiness",
                prepare_threshold=None,
            ) as connection:
                connection.execute("SELECT 1").fetchone()
            print(f"database ready after {attempt} attempt(s)")
            return 0
        except psycopg.Error as exc:
            last_error = str(exc).splitlines()[0]
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
    args = parser.parse_args()
    raise SystemExit(
        wait_for_database(
            timeout_seconds=args.timeout_seconds,
            max_delay_seconds=max(1.0, args.max_delay_seconds),
        )
    )


if __name__ == "__main__":
    main()
