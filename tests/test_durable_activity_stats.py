from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gaia import api as legacy
from gaia.activity_api import install_activity_api
from gaia.db import Database
from gaia.models import CollectorResult, Posting


def test_found_today_is_a_discovery_event_not_an_active_only_count(
    tmp_path, monkeypatch
) -> None:
    database = Database(tmp_path / "durable-activity.db")
    posting = Posting(
        company="Skydio",
        title="Software Engineer Intern Fall 2026/Winter 2027",
        apply_url=(
            "https://jobs.ashbyhq.com/skydio/"
            "f6320e9b-4eed-408d-8d37-d509fb0406ee"
        ),
        source="ashby:skydio",
        source_id="f6320e9b-4eed-408d-8d37-d509fb0406ee",
        source_mode="direct",
        category="software",
        year=2027,
        target_match="year_confirmed",
    )
    database.apply_result(
        CollectorResult(
            source=posting.source,
            postings=[posting],
            complete=True,
            mode="board",
            rows_scanned=1,
            expected_rows=1,
            status="ok",
        )
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE postings
            SET active=FALSE, removed_at=now()
            WHERE posting_key=%s
            """,
            (posting.posting_key,),
        )
    database.rebuild_families()

    monkeypatch.setattr(legacy, "db", database)
    app = FastAPI()
    install_activity_api(app)
    response = TestClient(app).get("/api/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["role_families"] == 0
    assert body["active_listings"] == 0
    assert body["new_today"] == 1
    assert body["new_urls_24h"] == 1
    assert body["removed_urls_24h"] == 1
    assert body["activity_units"]["new_today"] == "role_family"
