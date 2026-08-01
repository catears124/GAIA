from __future__ import annotations

from gaia.collectors import AshbyCollector
from gaia.db import Database
from gaia.native_collectors import GoogleInternshipCollector
from gaia.source_catalog import save_candidates


def test_recursive_candidates_exclude_configured_global_sources(tmp_path) -> None:
    database = Database(tmp_path / "recursive-native.db")

    saved = save_candidates(
        database,
        [
            AshbyCollector("Quiet Robotics", "quiet-robotics"),
            GoogleInternshipCollector(),
        ],
        origin="recursive-source:domain:quiet.example:Quiet Robotics",
    )

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT source, kind
            FROM source_candidates
            WHERE origin=%s
            ORDER BY source
            """,
            ("recursive-source:domain:quiet.example:Quiet Robotics",),
        ).fetchall()

    assert saved == 1
    assert [(row["source"], row["kind"]) for row in rows] == [
        ("ashby:quiet-robotics", "ashby")
    ]
