from pathlib import Path


FRONTEND = Path(__file__).parents[1] / "src" / "gaia" / "frontend"


def test_production_shell_loads_dark_mode_assets() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert '/assets/theme.js?v=1.0.0' in html
    assert '/assets/dark-mode.css?v=1.0.0' in html
    assert 'id="theme-toggle"' in html
    assert 'aria-label="Switch to dark mode"' in html


def test_theme_assets_support_persistence_and_dark_palette() -> None:
    script = (FRONTEND / "theme.js").read_text(encoding="utf-8")
    stylesheet = (FRONTEND / "dark-mode.css").read_text(encoding="utf-8")

    assert 'gaia:theme' in script
    assert 'prefers-color-scheme: dark' in script
    assert 'document.documentElement.dataset.theme' in script
    assert ':root[data-theme="dark"]' in stylesheet
    assert 'color-scheme: dark' in stylesheet
