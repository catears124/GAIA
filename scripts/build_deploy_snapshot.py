"""Build the compact public SQLite snapshot used by Vercel.

The source crawler database is intentionally ignored by git. This command
copies only product-facing records into a fresh database and removes internal
collection history that the web application does not need.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def build(source: Path, destination: Path) -> None:
    if not source.exists():
        raise SystemExit(f"source database does not exist: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.db")
    if temporary.exists():
        temporary.unlink()
    shutil.copyfile(source, temporary)

    with closing(sqlite3.connect(temporary)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            DELETE FROM postings
            WHERE active != 1
               OR target_match NOT IN ('exact', 'year_confirmed', 'source_confirmed')
            """
        )
        connection.execute(
            """
            DELETE FROM sync_runs
            WHERE id NOT IN (
                SELECT id FROM sync_runs
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC
                LIMIT 2
            )
            """
        )
        connection.execute(
            """
            DELETE FROM source_health
            WHERE last_run_id != (SELECT MAX(id) FROM sync_runs)
            """
        )
        connection.execute(
            """
            DELETE FROM benchmark_cases
            WHERE version != (SELECT MAX(version) FROM benchmark_cases)
            """
        )
        connection.commit()
        connection.execute("VACUUM")

    temporary.replace(destination)
    size_mb = destination.stat().st_size / 1024 / 1024
    print(f"wrote {destination} ({size_mb:.1f} MiB)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/gaia.db"))
    parser.add_argument("--output", type=Path, default=Path("deploy/gaia-snapshot.db"))
    arguments = parser.parse_args()
    build(arguments.source, arguments.output)


if __name__ == "__main__":
    main()
