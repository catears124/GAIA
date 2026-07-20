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
