from __future__ import annotations

import httpx
import pytest

from gaia.classify import classify
from gaia.db import Database
from gaia.link_validation import validate_application_links
from gaia.models import CollectorResult, Posting


@pytest.mark.asyncio
async def test_link_validation_records_statuses_and_retires_closed_roles(tmp_path):
    db = Database(tmp_path / "gaia.db")
    postings = [
        classify(
            Posting(
                company="Example",
                title="Software Engineer Intern, Summer 2027",
                apply_url=f"https://jobs.example.com/job/{source_id}",
                source="direct:test",
                source_id=source_id,
            )
        )
        for source_id in ("active", "protected", "closed")
    ]
    db.apply_result(CollectorResult("direct:test", postings, True, "board", 3, 3))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/active"):
            return httpx.Response(200, text="<h1>Software Engineer Intern</h1>")
        if request.url.path.endswith("/protected"):
            return httpx.Response(403)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await validate_application_links(db, client)

    assert summary.as_dict() == {
        "checked": 3,
        "active": 1,
        "protected": 1,
        "closed": 1,
        "failed": 0,
    }
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT source_id, active, link_http_status, link_status FROM postings "
            "ORDER BY source_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("active", 1, 200, "active"),
        ("closed", 0, 404, "closed"),
        ("protected", 1, 403, "protected"),
    ]
    assert db.list_families()["items"][0]["opening_count"] == 2
