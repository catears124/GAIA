from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_snapshot_transport_reframes_fresh_fallback_as_operational_mode() -> None:
    script = (FRONTEND / "remote-snapshot.js").read_text(encoding="utf-8")

    assert 'window.addEventListener("gaia:stale-data", presentSnapshotMode)' in script
    assert 'event?.detail?.source !== "deployed-snapshot"' in script
    assert "Snapshot mode · inventory refreshed" in script
    assert "Search and apply links remain available." in script
    assert 'banner.dataset.mode = "snapshot"' in script


def test_snapshot_status_asset_is_cache_busted() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert 'remote-snapshot.js?v=1.0.1' in html
