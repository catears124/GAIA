from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .dynamic_market_discovery import SNAPSHOT_VERSION, deserialize_candidates
from .live_inventory import LiveDatabase
from .quality import canonical_source_name
from .source_catalog import save_candidates


def capture_snapshot(
    snapshot: dict[str, Any],
    *,
    batch_size: int = 250,
) -> dict[str, Any]:
    """Persist market-discovered source evidence without probing employer boards.

    Discovery and validation intentionally have separate clocks. Candidate writes are
    committed in small batches so a database interruption cannot discard a whole search
    result containing thousands of source candidates.
    """
    if int(snapshot.get("version") or 0) != SNAPSHOT_VERSION:
        raise ValueError("unsupported dynamic market snapshot version")
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("dynamic market snapshot is missing candidates")

    database = LiveDatabase(migrate=False)
    candidates = deserialize_candidates([row for row in rows if isinstance(row, dict)])
    with database.connect() as connection:
        known_rows = connection.execute(
            "SELECT source FROM source_catalog WHERE validated"
        ).fetchall()
        known = {str(row["source"]) for row in known_rows}
        connection.execute(
            """
            DELETE FROM source_candidates AS candidate
            USING source_catalog AS catalog
            WHERE candidate.source=catalog.source
              AND catalog.validated
            """
        )

    unknown = [
        collector
        for collector in candidates
        if canonical_source_name(collector.name) not in known
    ]
    chunk = max(25, min(int(batch_size), 500))
    written = 0
    batches = 0
    for start in range(0, len(unknown), chunk):
        written += save_candidates(
            database,
            unknown[start : start + chunk],
            origin="dynamic-github-market",
        )
        batches += 1

    base = snapshot.get("summary")
    summary: dict[str, Any] = dict(base) if isinstance(base, dict) else {}
    summary.update(
        {
            "candidate_rows_in_snapshot": len(candidates),
            "candidate_rows_already_validated": len(candidates) - len(unknown),
            "candidate_rows_written": written,
            "candidate_write_batches": batches,
            "candidate_validation_deferred": True,
        }
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist GAIA market source candidates for the independent probe queue"
    )
    parser.add_argument("--snapshot-input", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()
    payload = json.loads(args.snapshot_input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dynamic market snapshot must be a JSON object")
    result = capture_snapshot(payload, batch_size=args.batch_size)
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
