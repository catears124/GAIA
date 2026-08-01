from __future__ import annotations

from gaia.market_discovery import DEFAULT_QUERIES


def test_market_discovery_covers_distinct_technical_internship_tracks() -> None:
    joined = "\n".join(DEFAULT_QUERIES).lower()

    assert len(DEFAULT_QUERIES) >= 10
    assert "software engineer intern" in joined
    assert "machine learning intern" in joined
    assert "data science intern" in joined
    assert "quant" in joined
    assert "trading intern" in joined
    assert "research intern" in joined
    assert "university recruiting" in joined
    assert len(set(DEFAULT_QUERIES)) == len(DEFAULT_QUERIES)
