from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "gaia" / "frontend"


def test_published_snapshot_transport_loads_before_resilience_layers() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    snapshot = html.index("/assets/remote-snapshot.js")
    primary = html.index("/assets/api-resilience.js")
    compatibility = html.index("/assets/emergency-outage.js")
    app = html.index("/assets/app-v2.js")
    assert snapshot < primary < compatibility < app
    assert 'emergency-outage.js?v=2.0.0' in html


def test_remote_snapshot_bypasses_vercel_functions() -> None:
    source = (FRONTEND / "remote-snapshot.js").read_text(encoding="utf-8")

    assert "raw.githubusercontent.com/catears124/GAIA/snapshot-data" in source
    assert 'url.pathname === LOCAL_PATH' in source
    assert 'cache: "no-store"' in source
    assert 'mode: "cors"' in source
    assert "return nativeFetch(input, init);" in source


def test_legacy_emergency_runtime_is_inert() -> None:
    source = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")

    assert "MAX_EMERGENCY_AGE_MS = 0" in source
    assert "retireLegacyState" in source
    assert 'LEGACY_BANNER_ID = "gaia-emergency-banner"' in source
    assert "window.fetch =" not in source
    assert "localStorage" not in source
    assert "Live inventory is offline" not in source
    assert "durable device backup" not in source


def test_resilience_runtime_is_the_only_read_fallback_wrapper() -> None:
    primary = (FRONTEND / "api-resilience.js").read_text(encoding="utf-8")
    compatibility = (FRONTEND / "emergency-outage.js").read_text(encoding="utf-8")

    assert "window.fetch = async function resilientFetch" in primary
    assert "window.fetch =" not in compatibility
