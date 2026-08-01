from __future__ import annotations

from gaia.db import Database
from gaia.models import CollectorResult, Posting

SOURCE = "ashby:skydio"


def _posting() -> Posting:
    return Posting(
        company="Skydio",
        title="Product Management Intern",
        apply_url=(
            "https://jobs.ashbyhq.com/skydio/"
            "1ec2fe3c-3fb2-4485-870d-764a3e5f5baf"
        ),
        source=SOURCE,
        source_id="1ec2fe3c-3fb2-4485-870d-764a3e5f5baf",
        source_mode="direct",
        category="product",
        target_match="exact",
    )


def _result(postings: list[Posting]) -> CollectorResult:
    return CollectorResult(
        source=SOURCE,
        postings=postings,
        complete=True,
        mode="board",
        rows_scanned=len(postings),
        expected_rows=len(postings),
        status="ok" if postings else "empty",
    )


def test_one_complete_omission_keeps_recent_listing_visible(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GAIA_REMOVAL_GRACE_SECONDS", "1800")
    database = Database(tmp_path / "one-omission.db")
    item = _posting()
    database.apply_result(_result([item]))

    database.apply_result(_result([]))

    with database.connect() as connection:
        row = connection.execute(
            "SELECT active, removed_at FROM postings WHERE posting_key=%s",
            (item.posting_key,),
        ).fetchone()
        family = connection.execute(
            "SELECT family_key FROM families WHERE family_key=%s",
            (item.family_key,),
        ).fetchone()
    assert row["active"] is True
    assert row["removed_at"] is not None
    assert family is not None


def test_second_aged_omission_confirms_removal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GAIA_REMOVAL_GRACE_SECONDS", "60")
    database = Database(tmp_path / "confirmed-removal.db")
    item = _posting()
    database.apply_result(_result([item]))
    database.apply_result(_result([]))
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE postings
            SET removed_at=now() - interval '2 minutes'
            WHERE posting_key=%s
            """,
            (item.posting_key,),
        )

    database.apply_result(_result([]))

    with database.connect() as connection:
        row = connection.execute(
            "SELECT active, removed_at FROM postings WHERE posting_key=%s",
            (item.posting_key,),
        ).fetchone()
        family = connection.execute(
            "SELECT family_key FROM families WHERE family_key=%s",
            (item.family_key,),
        ).fetchone()
    assert row["active"] is False
    assert row["removed_at"] is not None
    assert family is None


def test_reappearance_cancels_pending_removal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GAIA_REMOVAL_GRACE_SECONDS", "1800")
    database = Database(tmp_path / "reappearance.db")
    item = _posting()
    database.apply_result(_result([item]))
    database.apply_result(_result([]))

    database.apply_result(_result([item]))

    with database.connect() as connection:
        row = connection.execute(
            "SELECT active, removed_at FROM postings WHERE posting_key=%s",
            (item.posting_key,),
        ).fetchone()
    assert row["active"] is True
    assert row["removed_at"] is None


def test_recent_legacy_hard_removal_is_restored_to_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GAIA_LEGACY_REMOVAL_RESTORE_SECONDS", "21600")
    database = Database(tmp_path / "legacy-removal.db")
    item = _posting()
    database.apply_result(_result([item]))
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE postings
            SET active=FALSE, removed_at=NULL
            WHERE posting_key=%s
            """,
            (item.posting_key,),
        )

    other = CollectorResult(
        source="greenhouse:other",
        postings=[],
        complete=False,
        mode="board",
        rows_scanned=0,
        status="partial",
    )
    database.apply_result(other)

    with database.connect() as connection:
        row = connection.execute(
            "SELECT active, removed_at FROM postings WHERE posting_key=%s",
            (item.posting_key,),
        ).fetchone()
        family = connection.execute(
            "SELECT family_key FROM families WHERE family_key=%s",
            (item.family_key,),
        ).fetchone()
    assert row["active"] is True
    assert row["removed_at"] is not None
    assert family is not None
