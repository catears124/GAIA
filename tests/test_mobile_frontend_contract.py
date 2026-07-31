from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "gaia" / "frontend"


def read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_mobile_filters_are_collapsed_without_hiding_desktop_controls() -> None:
    script = read("app-improvements.js")

    assert 'window.matchMedia("(max-width: 720px)")' in script
    assert 'details.className = "mobile-filter-disclosure"' in script
    assert 'details.open = details.dataset.mobileOpen === "true"' in script
    assert 'details.open = true' in script
    assert '.mobile-filter-disclosure:not([open])>.filter-grid{display:none!important}' in script
    assert '.mobile-filter-disclosure[open]>.filter-grid{display:grid!important}' in script


def test_mobile_filter_summary_reports_real_active_controls() -> None:
    script = read("app-improvements.js")

    for control in (
        'trust: "all"',
        'category: ""',
        'company: ""',
        'location: ""',
        'target: ""',
        '"posted-within": "0"',
        'sort: "newest"',
        'remote: false',
    ):
        assert control in script
    assert 'countNode.textContent = count ? `${count} active` : "All jobs"' in script
    assert 'details.classList.toggle("has-active-filters", count > 0)' in script
    assert 'grid.addEventListener("input", updateCount)' in script
    assert 'grid.addEventListener("change", updateCount)' in script


def test_mobile_layout_prioritizes_search_presets_and_results() -> None:
    script = read("app-improvements.js")

    assert '.quick-actions{order:2!important;display:flex!important' in script
    assert 'overflow-x:auto' in script
    assert 'scroll-snap-type:x proximity' in script
    assert '.mobile-filter-disclosure{order:3;display:block' in script
    assert '.page-intro h1{max-width:100%;font-size:clamp(2rem,8.6vw,2.55rem)!important' in script
    assert '#result-note{display:none!important}' in script


def test_mobile_header_and_safari_safe_area_are_compact() -> None:
    script = read("app-improvements.js")

    assert '.topbar-actions{display:contents!important}' in script
    assert '.theme-toggle{grid-column:3;grid-row:1' in script
    assert '.freshness{grid-column:1/-1!important;grid-row:2' in script
    assert 'env(safe-area-inset-bottom)' in script
    assert '.toast{bottom:calc(5.25rem + env(safe-area-inset-bottom))!important' in script


def test_immutable_mobile_asset_is_cache_busted() -> None:
    html = read("index.html")

    assert '/assets/app-improvements.js?v=1.1.0' in html
    assert '/assets/app-improvements.js?v=1.0.0' not in html
