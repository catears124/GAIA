from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from .collectors import Collector, GreenhouseCollector
from .models import Posting

GREENHOUSE_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}


def greenhouse_embed_collectors(postings: list[Posting]) -> list[Collector]:
    """Recover board tokens from Greenhouse embed URLs such as ?for=flipp."""
    boards: dict[str, tuple[str, str]] = {}
    for posting in postings:
        parts = urlsplit(posting.apply_url)
        if parts.netloc.lower() not in GREENHOUSE_HOSTS:
            continue
        token = (parse_qs(parts.query).get("for") or [None])[0]
        if not token:
            continue
        token = str(token).strip()
        if not token or token.lower() in {"jobs", "job", "embed", "apply"}:
            continue
        scope = "historical" if posting.source_mode == "universe-seed" else "current"
        existing = boards.get(token)
        if existing is None or (existing[1] == "historical" and scope == "current"):
            boards[token] = (posting.company, scope)

    collectors: list[Collector] = []
    for token, (company, scope) in boards.items():
        collector = GreenhouseCollector(company, token)
        collector.scope = scope
        collectors.append(collector)
    return collectors
