from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaia.static_snapshot_http import SnapshotExportError, _validate_live_health


def health(*, healthy: bool, fresh: int = 5, total: int = 6) -> dict[str, object]:
    return {
        "ok": healthy,
        "stale": False,
        "inventory": {
            "healthy": healthy,
            "fresh": fresh,
            "total": total,
            "latest_activity_at": datetime.now(UTC).isoformat(),
        },
    }


def test_fresh_degraded_inventory_is_exportable_without_being_relabelled_healthy() -> None:
    payload = health(healthy=False)

    inventory = _validate_live_health(payload)

    assert inventory["healthy"] is False
    assert inventory["fresh"] == 5
    assert payload["ok"] is False


def test_healthy_inventory_remains_exportable() -> None:
    assert _validate_live_health(health(healthy=True))["healthy"] is True


def test_stale_empty_and_freshness_free_inventory_fail_closed() -> None:
    stale = health(healthy=False)
    stale["stale"] = True
    with pytest.raises(SnapshotExportError, match="stale"):
        _validate_live_health(stale)
    with pytest.raises(SnapshotExportError, match="empty"):
        _validate_live_health(health(healthy=False, fresh=0, total=0))
    with pytest.raises(SnapshotExportError, match="invalid freshness"):
        _validate_live_health(health(healthy=False, fresh=0, total=6))


def test_dishonest_ok_state_is_rejected() -> None:
    payload = health(healthy=False)
    payload["ok"] = True

    with pytest.raises(SnapshotExportError, match="dishonestly"):
        _validate_live_health(payload)


def test_activity_must_be_recent_and_not_in_the_future(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    payload = health(healthy=False)
    payload["inventory"]["latest_activity_at"] = (now - timedelta(hours=7)).isoformat()  # type: ignore[index]
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MAX_ACTIVITY_MINUTES", "360")
    with pytest.raises(SnapshotExportError, match="older than"):
        _validate_live_health(payload, now=now)

    payload["inventory"]["latest_activity_at"] = (now + timedelta(minutes=6)).isoformat()  # type: ignore[index]
    with pytest.raises(SnapshotExportError, match="future"):
        _validate_live_health(payload, now=now)


def test_activity_window_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
    payload = health(healthy=False)
    payload["inventory"]["latest_activity_at"] = (now - timedelta(hours=25)).isoformat()  # type: ignore[index]
    monkeypatch.setenv("GAIA_STATIC_SNAPSHOT_MAX_ACTIVITY_MINUTES", "999999")

    with pytest.raises(SnapshotExportError, match="1440 minutes"):
        _validate_live_health(payload, now=now)
