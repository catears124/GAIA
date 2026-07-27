from gaia.classify import classify
from gaia.models import Posting


def role(title: str):
    return classify(
        Posting(
            company="Example",
            title=title,
            apply_url="https://example.com/job",
            source="registry:test",
            source_id=title,
            source_mode="registry",
        ),
        source_confirms_2027=True,
    )


def test_registry_cannot_promote_explicit_summer_2026():
    assert role("Software Engineer Intern - Summer 2026").target_match == "wrong_year"


def test_registry_cannot_promote_explicit_fall_2027():
    assert role("Software Engineer Intern - Fall 2027").target_match == "wrong_season"


def test_registry_can_fill_only_an_omitted_year():
    item = role("Software Engineer Intern - Summer")
    assert item.target_match == "source_confirmed"
    assert item.year == 2027


def test_explicit_summer_2027_wins_without_registry_provenance():
    item = classify(
        Posting(
            company="Example",
            title="2027 Summer Software Engineer Intern",
            apply_url="https://example.com/job",
            source="direct",
            source_id="1",
        )
    )
    assert item.target_match == "exact"


def test_employer_description_supplies_missing_summer_2027_evidence():
    item = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern",
            apply_url="https://example.com/job/1",
            source="greenhouse:example",
            source_id="1",
            source_mode="direct",
            description="Join our Summer 2027 internship cohort.",
        )
    )
    assert item.target_match == "exact"
    assert item.year == 2027


def test_index_description_cannot_promote_an_undated_title():
    item = classify(
        Posting(
            company="Example",
            title="Software Engineer Intern",
            apply_url="https://example.com/job/1",
            source="registry:test",
            source_id="1",
            source_mode="registry",
            description="README archive for Summer 2027 roles.",
        )
    )
    assert item.target_match == "unknown"


def test_quantitative_and_data_analyst_titles_are_technical():
    quant = role("Quantitative Research Intern - Summer 2027")
    data = role("Data Analyst Intern - Summer 2027")
    assert quant.category == "quant"
    assert data.category == "data"
