from __future__ import annotations

from gaia.quality import canonical_company, canonical_source_name, normalize_locations


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


def test_source_catalog_identity_is_case_stable_without_destroying_case_sensitive_values():
    assert canonical_source_name("ashby:AtomicSemi") == "ashby:atomicsemi"
    assert canonical_source_name("ashby:atomicsemi") == "ashby:atomicsemi"
