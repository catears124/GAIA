from __future__ import annotations

from gaia.embed_discovery import greenhouse_embed_collectors
from gaia.models import Posting


def test_greenhouse_embed_for_parameter_becomes_board_collector():
    posting = Posting(
        company="Flipp",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://boards.greenhouse.io/embed/job_board?for=flipp",
        source="registry:test",
        source_id="1",
        source_mode="registry",
    )
    collectors = greenhouse_embed_collectors([posting])
    assert len(collectors) == 1
    assert collectors[0].name == "greenhouse:flipp"
    assert collectors[0].scope == "current"


def test_current_embed_reference_overrides_historical_scope():
    historical = Posting(
        company="Flipp",
        title="Software Engineer Intern, Summer 2026",
        apply_url="https://boards.greenhouse.io/embed/job_board?for=flipp",
        source="universe-seed:test",
        source_id="old",
        source_mode="universe-seed",
    )
    current = Posting(
        company="Flipp",
        title="Software Engineer Intern, Summer 2027",
        apply_url="https://boards.greenhouse.io/embed/job_board?for=flipp",
        source="registry:test",
        source_id="current",
        source_mode="registry",
    )
    collectors = greenhouse_embed_collectors([historical, current])
    assert collectors[0].scope == "current"
