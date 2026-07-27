from __future__ import annotations

from gaia.classify import classify
from gaia.inventory import InventoryStore
from gaia.inventory_runtime import COVERAGE_KINDS, FALLBACK_KINDS, SUPPORTED_CATALOG_KINDS
from gaia.models import Posting


def posting(title: str, *, description: str = "", employment_type: str = "") -> Posting:
    return Posting(
        company="Example",
        title=title,
        apply_url="https://example.com/jobs/123",
        source="greenhouse:example",
        source_id="123",
        source_mode="direct",
        description=description,
        employment_type=employment_type,
    )


def test_program_style_title_is_not_dropped() -> None:
    item = classify(posting("Summer Technology Analyst Program 2027"))
    assert item.target_match == "exact"
    assert item.category == "software"


def test_employer_description_can_supply_internship_evidence() -> None:
    item = classify(
        posting(
            "Machine Learning Student Researcher 2027",
            description="This summer internship joins our applied AI team.",
        )
    )
    assert item.target_match == "exact"
    assert item.category == "ml-ai"


def test_general_engineering_program_is_retained_as_technical() -> None:
    item = classify(posting("Engineering Summer Associate 2027"))
    assert item.target_match == "exact"
    assert item.category == "other-technical"


def test_source_cadence_is_independent_and_dormant_sources_still_run() -> None:
    assert InventoryStore._default_interval("greenhouse", "current") == 15 * 60
    assert InventoryStore._default_interval("workday-search", "current") == 30 * 60
    assert InventoryStore._default_interval("domain", "current") == 6 * 3600
    assert InventoryStore._default_interval("greenhouse", "historical") == 24 * 3600


def test_fallbacks_are_scheduled_but_do_not_define_market_coverage() -> None:
    assert FALLBACK_KINDS == {"domain", "verification"}
    assert FALLBACK_KINDS <= SUPPORTED_CATALOG_KINDS
    assert FALLBACK_KINDS.isdisjoint(COVERAGE_KINDS)
    assert {"greenhouse", "lever", "ashby", "workday-search"} <= COVERAGE_KINDS
