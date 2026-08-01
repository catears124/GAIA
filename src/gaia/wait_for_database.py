from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
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
    "tenant/user",
    "tenant or user not found",
)
_PERMANENT_SQLSTATES = {
    "28000",
    "28P01",
    "3D000",
}
_STATE_BY_EXIT_CODE = {0: "ready", 1: "recovering", 2: "invalid", 3: "failed"}


@dataclass(frozen=True)
class ReadinessResult:
    exit_code: int
    state: str
    attempts: int
    elapsed_seconds: float
    reason: str
    sqlstate: str | None = None


def is_retryable_database_error(error: BaseException) -> bool:
    """Separate transient database recovery from broken connection configuration."""
    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in _PERMANENT_SQLSTATES:
        return False
    message = str(error).casefold()
    return not any(marker in message for marker in _PERMANENT_CONFIGURATION_ERRORS)


def _single_line(value: object) -> str:
    return " ".join(str(value).splitlines()).strip() or "database unavailable"


def _write_outputs(result: ReadinessResult, output_path: str | None) -> None:
    if not output_path:
        return
    path = Path(output_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"state={result.state}\n")
        handle.write(f"exit_code={result.exit_code}\n")
        handle.write(f"attempts={result.attempts}\n")
        handle.write(f"elapsed_seconds={result.elapsed_seconds:.3f}\n")
        handle.write(f"reason={result.reason[:500]}\n")
        handle.write(f"sqlstate={result.sqlstate or ''}\n")


def _write_json(result: ReadinessResult, path_value: str | None) -> None:
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(result), sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def probe_database(*, timeout_seconds: int, max_delay_seconds: float) -> ReadinessResult:
    """Return a machine-readable result for one bounded PostgreSQL readiness window."""
    started = time.monotonic()
    timeout_seconds = max(1, int(timeout_seconds))
    max_delay_seconds = max(1.0, float(max_delay_seconds))
    deadline = started + timeout_seconds
    attempt = 0
    last_error = "database unavailable"
    last_sqlstate: str | None = None

    try:
        url = _database_url(None)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        return ReadinessResult(2, "invalid", 0, time.monotonic() - started, _single_line(exc))

    while time.monotonic() < deadline:
        attempt += 1
        remaining_before_connect = max(0.0, deadline - time.monotonic())
        connect_timeout = max(1, min(15, int(max_delay_seconds), max(1, int(remaining_before_connect))))
        try:
            with psycopg.connect(url, connect_timeout=connect_timeout, application_name="gaia-readiness", prepare_threshold=None) as connection:
                connection.execute("SELECT 1").fetchone()
            return ReadinessResult(0, "ready", attempt, time.monotonic() - started, "database accepted a read probe")
        except psycopg.Error as exc:
            last_error = _single_line(exc)
            last_sqlstate = getattr(exc, "sqlstate", None)
            if not is_retryable_database_error(exc):
                return ReadinessResult(2, "invalid", attempt, time.monotonic() - started, last_error, last_sqlstate)
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            base = min(max_delay_seconds, 2 ** min(attempt, 5))
            delay = min(remaining, base + random.uniform(0.0, min(1.5, base / 3)))
            print(f"database not ready (attempt {attempt}): {last_error}; retrying in {delay:.1f}s", file=sys.stderr, flush=True)
            if delay > 0:
                time.sleep(delay)
        except Exception as exc:
            return ReadinessResult(3, "failed", attempt, time.monotonic() - started, _single_line(exc))

    return ReadinessResult(1, "recovering", attempt, time.monotonic() - started, last_error, last_sqlstate)


def wait_for_database(*, timeout_seconds: int, max_delay_seconds: float) -> int:
    result = probe_database(timeout_seconds=timeout_seconds, max_delay_seconds=max_delay_seconds)
    stream = sys.stdout if result.exit_code == 0 else sys.stderr
    print(f"database state={result.state} attempts={result.attempts} elapsed={result.elapsed_seconds:.1f}s reason={result.reason}", file=stream, flush=True)
    return result.exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait for PostgreSQL readiness")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("GAIA_DB_RECOVERY_TIMEOUT", "600")))
    parser.add_argument("--max-delay-seconds", type=float, default=float(os.getenv("GAIA_DB_RECOVERY_MAX_DELAY", "30")))
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT"))
    parser.add_argument("--json-output", default=os.getenv("GAIA_DB_READINESS_JSON"))
    args = parser.parse_args()
    result = probe_database(timeout_seconds=args.timeout_seconds, max_delay_seconds=args.max_delay_seconds)
    _write_outputs(result, args.github_output)
    _write_json(result, args.json_output)
    stream = sys.stdout if result.exit_code == 0 else sys.stderr
    print(f"database state={result.state} attempts={result.attempts} elapsed={result.elapsed_seconds:.1f}s reason={result.reason}", file=stream, flush=True)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
