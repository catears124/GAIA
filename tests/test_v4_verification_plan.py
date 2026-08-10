from datetime import UTC, datetime, timedelta

from gaia.models import Posting
from gaia.v4_verification_plan import plan_verification_collectors


def _posting(company: str, title: str, url: str, *, minutes_ago: int | None = None) -> Posting:
    now = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)
    return Posting(
        company=company,
        title=title,
        apply_url=url,
        source="sensor:test",
        source_id=url,
        source_mode="market-sensor",
        sensor_reported_at=now - timedelta(minutes=minutes_ago) if minutes_ago is not None else None,
        observed_at=now,
        category="software",
        year=2027,
        season="summer",
        target_match="exact",
    )


def _settings() -> dict[str, object]:
    return {
        "workday": {"search_terms": ["intern"]},
        "native_sources": [],
    }


def test_plan_keeps_cheap_board_enumeration_and_hot_exact_pages(monkeypatch):
    monkeypatch.setenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "1")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_LIMIT", "20")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_BATCH", "10")
    postings = [
        _posting(
            "Acme GH",
            "Software Engineering Intern",
            "https://job-boards.greenhouse.io/acme/jobs/123",
            minutes_ago=5,
        ),
        _posting(
            "Acme WD",
            "Software Engineering Intern",
            "https://acme.wd1.myworkdayjobs.com/en-US/External/job/New-York/Software-Intern_R1",
            minutes_ago=10,
        ),
        _posting(
            "Custom",
            "Machine Learning Intern",
            "https://careers.custom.example/jobs/ml-intern",
            minutes_ago=1,
        ),
    ]
    collectors = plan_verification_collectors(
        postings,
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    names = [collector.name for collector in collectors]
    assert any(name.startswith("greenhouse:acme") for name in names)
    assert any(name.startswith("hot-page:acme.wd1.myworkdayjobs.com:") for name in names)
    assert any(name.startswith("hot-page:careers.custom.example:") for name in names)
    assert sum(name.startswith("workday:") for name in names) <= 1
    assert not any(name.startswith("schema:") for name in names)


def test_aggregator_detail_pages_can_never_grant_employer_verification(monkeypatch):
    monkeypatch.setenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "0")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_LIMIT", "100")
    postings = [
        _posting(
            "TikTok",
            "Machine Learning Engineer Intern",
            "https://simplify.jobs/p/abc/Machine-Learning-Engineer-Intern",
            minutes_ago=1,
        ),
        _posting(
            "Acme",
            "Software Engineering Intern",
            "https://www.linkedin.com/jobs/view/123",
            minutes_ago=2,
        ),
        _posting(
            "Real Employer",
            "Software Engineering Intern",
            "https://careers.real-employer.example/jobs/123",
            minutes_ago=3,
        ),
    ]
    collectors = plan_verification_collectors(
        postings,
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    names = [collector.name for collector in collectors]
    assert not any("simplify.jobs" in name for name in names)
    assert not any("linkedin.com" in name for name in names)
    assert any("careers.real-employer.example" in name for name in names)


def test_aggregator_subdomains_are_also_blocked(monkeypatch):
    monkeypatch.setenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "0")
    collectors = plan_verification_collectors(
        [
            _posting(
                "Acme",
                "Software Engineering Intern",
                "https://jobs.simplify.jobs/p/abc/Software-Engineering-Intern",
                minutes_ago=1,
            )
        ],
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    assert not any(collector.name.startswith("hot-page:") for collector in collectors)


def test_workday_board_lane_is_bounded_and_rotates(monkeypatch):
    monkeypatch.setenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "2")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_LIMIT", "50")
    postings = [
        _posting(
            f"Company {index}",
            "Software Engineering Intern",
            f"https://tenant{index}.wd1.myworkdayjobs.com/en-US/External/job/City/Intern_R{index}",
        )
        for index in range(8)
    ]
    first = plan_verification_collectors(
        postings,
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    second = plan_verification_collectors(
        postings,
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 15, tzinfo=UTC),
    )
    first_workday = {collector.name for collector in first if collector.name.startswith("workday:")}
    second_workday = {collector.name for collector in second if collector.name.startswith("workday:")}
    assert len(first_workday) == 2
    assert len(second_workday) == 2
    assert first_workday != second_workday


def test_explicitly_dated_hot_pages_win_before_undated_rotation(monkeypatch):
    monkeypatch.setenv("GAIA_V4_WORKDAY_BOARD_BUDGET", "0")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_LIMIT", "50")
    monkeypatch.setenv("GAIA_V4_HOT_PAGE_BATCH", "50")
    postings = [
        _posting("Dated", "Software Engineering Intern", "https://jobs.dated.example/1", minutes_ago=2),
        *[
            _posting(f"Undated {index}", "Software Engineering Intern", f"https://jobs.undated{index}.example/1")
            for index in range(80)
        ],
    ]
    collectors = plan_verification_collectors(
        postings,
        settings=_settings(),
        now=datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    )
    names = [collector.name for collector in collectors]
    assert any("jobs.dated.example" in name for name in names)
