from __future__ import annotations

from gaia.quality import (
    canonical_company,
    canonical_source_name,
    clean_text,
    is_specific_application_url,
    normalize_locations,
)


def test_company_aliases_remove_flags_and_merge_known_variants():
    assert canonical_company("BAE Systems 🇺🇸") == "BAE Systems"
    assert canonical_company("DE Shaw") == "D. E. Shaw"
    assert canonical_company("D. E. Shaw") == "D. E. Shaw"
    assert canonical_company("Susquehanna International Group (SIG)") == "Susquehanna International Group"


def test_location_parser_repairs_registry_markdown_garbage():
    assert normalize_locations("4 locations**Nashua, NHHudson, NHManchester, NHMerrimack, NH") == [
        "Nashua, NH",
        "Hudson, NH",
        "Manchester, NH",
        "Merrimack, NH",
    ]
    assert normalize_locations("3 locations**New York, NYChicago, ILAustin, TX · —") == [
        "New York, NY",
        "Chicago, IL",
        "Austin, TX",
    ]
    assert normalize_locations(["2026-07-24", "(multiple US)"]) == ["United States"]


def test_text_cleanup_removes_nested_markdown_links_and_decorative_emoji():
    assert clean_text(
        "[Quantitative Researcher - Intern [2027 Summer]](https://example.com/job)"
    ) == "Quantitative Researcher - Intern [2027 Summer]"
    assert clean_text("⭐️ Software Engineering Intern") == "Software Engineering Intern"


def test_source_catalog_identity_is_case_stable_without_destroying_case_sensitive_values():
    assert canonical_source_name("ashby:AtomicSemi") == "ashby:atomicsemi"
    assert canonical_source_name("ashby:atomicsemi") == "ashby:atomicsemi"
    assert canonical_source_name("workday:Salesforce:Futureforce_Internships") == (
        "workday:salesforce:futureforce_internships"
    )


def test_specific_application_url_rejects_employer_home_and_search_pages():
    assert not is_specific_application_url("https://www.uber.com/")
    assert not is_specific_application_url(
        "https://www.google.com/about/careers/applications/jobs/results/"
    )
    assert is_specific_application_url(
        "https://www.tower-research.com/open-positions/?gh_jid=8044334"
    )
    assert is_specific_application_url(
        "https://jobs.ashbyhq.com/example/05a5dc9e-5c5b-4f92-b2bf-93c9348d264c"
    )
