from __future__ import annotations

import argparse
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg


@dataclass(frozen=True, slots=True)
class Candidate:
    label: str
    url: str


def database_candidates() -> list[Candidate]:
    """Return unique configured Postgres endpoints without exposing secret values."""
    keys = (
        ("non_pooling", "POSTGRES_URL_NON_POOLING"),
        ("pooling", "POSTGRES_URL"),
        ("prisma", "POSTGRES_PRISMA_URL"),
        ("gaia", "GAIA_DATABASE_URL"),
    )
    seen: set[str] = set()
    output: list[Candidate] = []
    for label, key in keys:
        value = str(os.getenv(key) or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(Candidate(label, value))
    return output


def probe(url: str, connect_timeout: int) -> tuple[bool, str]:
    try:
        with psycopg.connect(url, connect_timeout=max(2, connect_timeout)) as connection:
            row = connection.execute("SELECT 1 AS ready").fetchone()
            if row is None:
                return False, "empty readiness query"
    except Exception as exc:  # noqa: BLE001 - connection failures vary by provider
        text = str(exc).replace("\n", " ").strip()
        return False, text[:220] or exc.__class__.__name__
    return True, "ready"


def select_database_url(
    *,
    timeout_seconds: float,
    connect_timeout: int,
    max_delay_seconds: float,
) -> tuple[Candidate | None, dict[str, object]]:
    candidates = database_candidates()
    started = time.monotonic()
    attempts = 0
    failures: dict[str, str] = {}
    if not candidates:
        return None, {
            "state": "missing",
            "attempts": 0,
            "elapsed_seconds": 0.0,
            "candidate_labels": [],
            "failures": {},
        }

    while True:
        for candidate in candidates:
            attempts += 1
            ok, reason = probe(candidate.url, connect_timeout)
            if ok:
                elapsed = time.monotonic() - started
                return candidate, {
                    "state": "ready",
                    "selected": candidate.label,
                    "attempts": attempts,
                    "elapsed_seconds": round(elapsed, 1),
                    "candidate_labels": [item.label for item in candidates],
                    "failures": failures,
                }
            failures[candidate.label] = reason

        elapsed = time.monotonic() - started
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            return None, {
                "state": "recovering",
                "attempts": attempts,
                "elapsed_seconds": round(elapsed, 1),
                "candidate_labels": [item.label for item in candidates],
                "failures": failures,
            }
        delay = min(
            max(1.0, max_delay_seconds),
            remaining,
            max(1.0, min(max_delay_seconds, 2 ** min(5, attempts // len(candidates)))),
        )
        delay = min(remaining, delay + random.uniform(0.0, min(1.0, delay * 0.1)))
        time.sleep(max(0.1, delay))


def write_github_env(path: Path, candidate: Candidate) -> None:
    # GitHub masks the source secret automatically; explicitly mask the selected
    # alias as defense in depth before writing it to subsequent-step environment.
    print(f"::add-mask::{candidate.url}")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"GAIA_DATABASE_URL={candidate.url}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select the first reachable configured Supabase/Postgres endpoint"
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--max-delay-seconds", type=float, default=30.0)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    candidate, report = select_database_url(
        timeout_seconds=max(0.0, args.timeout_seconds),
        connect_timeout=max(2, args.connect_timeout),
        max_delay_seconds=max(1.0, args.max_delay_seconds),
    )
    if args.json_output:
        args.json_output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, sort_keys=True))
    if candidate is None:
        raise SystemExit(2)
    if args.github_env:
        write_github_env(args.github_env, candidate)


if __name__ == "__main__":
    main()
