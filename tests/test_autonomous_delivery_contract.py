from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "gaia"


def test_discord_delivery_selects_from_families_not_posting_history() -> None:
    source = (SRC / "discord_notify_fast.py").read_text(encoding="utf-8")
    query = source.split('rows = connection.execute(', 1)[1]

    assert "FROM families AS family" in query
    assert "jsonb_array_elements(candidate.openings)" in query
    assert "discord_notification_deliveries" in query
    assert "FROM postings" not in query
    assert "GROUP BY posting.family_key" not in query


def test_direct_delivery_never_calls_vercel() -> None:
    source = (SRC / "autonomous_delivery.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/lead-promotion.yml").read_text(encoding="utf-8")

    assert "verify_fresh_leads" in source
    assert "ensure_public_feed_current" in source
    assert "discord_notify_fast" in source
    assert "httpx" not in source
    assert "BASE_URL" not in workflow
    assert "api/maintenance/diagnostics/promote-leads" not in workflow
    assert "python -m gaia.autonomous_delivery" in workflow
    assert 'cron: "3,8,13,18,23,28,33,38,43,48,53,58 * * * *"' in workflow


def test_stale_snapshot_is_not_reported_as_success() -> None:
    workflow = (ROOT / ".github/workflows/static-snapshot.yml").read_text(encoding="utf-8")

    assert "max_age_hours=2" in workflow
    assert 'state=failure' in workflow
    assert 'description="Snapshot refresh failed; only an old recovery copy is available"' in workflow
    assert 'exit "$fail"' in workflow


def test_autonomous_delivery_installs_due_queue_indexes() -> None:
    source = (SRC / "autonomous_delivery.py").read_text(encoding="utf-8")

    assert "idx_postings_runtime_lead_due" in source
    assert "idx_families_discord_verified_due" in source
    assert "idx_families_discord_lead_due" in source
