from __future__ import annotations

from gaia.market_discovery import DEFAULT_QUERIES, discovery_queries


def test_market_discovery_covers_distinct_technical_internship_tracks() -> None:
    joined = "\n".join(DEFAULT_QUERIES).lower()

    assert len(DEFAULT_QUERIES) >= 10
    assert "software engineer intern" in joined
    assert "machine learning engineer intern" in joined
    assert "data scientist intern" in joined
    assert "quantitative researcher intern" in joined
    assert "trading intern" in joined
    assert "research scientist intern" in joined
    assert "university recruiting" in joined
    assert len(set(DEFAULT_QUERIES)) == len(DEFAULT_QUERIES)


def test_configured_and_exact_queries_are_composed_without_duplicates() -> None:
    configured = [
        '"2027 internships" in:name,description,readme',
        DEFAULT_QUERIES[0],
    ]

    queries = discovery_queries({"queries": configured})

    assert queries[0] == configured[0]
    assert queries.count(DEFAULT_QUERIES[0]) == 1
    assert set(DEFAULT_QUERIES).issubset(queries)
    assert len(queries) == len(set(queries))
