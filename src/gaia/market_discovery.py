from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from .discovery import _document_postings
from .models import CollectorResult, Posting

# These exact-role searches complement the broad configurable query inventory. Keep
# them distinct: repositories often mention a concrete role but never use the generic
# word "internships" in their name or description.
DEFAULT_QUERIES = (
    '"2027 software engineer intern" in:name,description,readme',
    '"2027 software developer intern" in:name,description,readme',
    '"2027 SWE intern" in:name,description,readme',
    '"2027 machine learning engineer intern" in:name,description,readme',
    '"2027 data scientist intern" in:name,description,readme',
    '"2027 data engineer intern" in:name,description,readme',
    '"2027 applied scientist intern" in:name,description,readme',
    '"2027 research scientist intern" in:name,description,readme',
    '"2027 quantitative researcher intern" in:name,description,readme',
    '"2027 quantitative trader intern" in:name,description,readme',
    '"2027 trading intern" in:name,description,readme',
    '"2027 security engineer intern" in:name,description,readme',
    '"2027 systems engineer intern" in:name,description,readme',
    '"2027 firmware engineer intern" in:name,description,readme',
    '"2027 hardware engineer intern" in:name,description,readme',
    '"2027 robotics engineer intern" in:name,description,readme',
    '"2027 product manager intern" in:name,description,readme',
    '"2027 university recruiting" jobs in:name,description,readme',
)
RETRYABLE_STATUSES = {403, 429, 500, 502, 503, 504}


def discovery_queries(config: dict[str, Any]) -> tuple[str, ...]:
    """Compose broad configured searches with exact role searches without duplicates."""
    configured = tuple(str(query).strip() for query in config.get("queries") or ())
    combined = (*configured, *DEFAULT_QUERIES)
    return tuple(dict.fromkeys(query for query in combined if query))


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token := os.getenv("GITHUB_TOKEN") or os.getenv("GAIA_GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(30.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return min(30.0, max(1.0, float(reset) - time.time() + 1.0))
        except ValueError:
            pass
    return min(30.0, 2.0 ** (attempt + 1))


async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 4,
) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in range(max(1, attempts)):
        response = await client.get(url, params=params, headers=headers)
        if response.status_code not in RETRYABLE_STATUSES:
            return response
        if attempt + 1 < attempts:
            await asyncio.sleep(_retry_delay(response, attempt))
    assert response is not None
    return response


async def discover_github_market(
    client: httpx.AsyncClient,
    settings: dict[str, Any],
) -> tuple[list[Posting], list[CollectorResult]]:
    """Discover active public internship feeds without naming employers in code."""
    config = settings.get("market_discovery", {}).get("github", {})
    if config.get("enabled", True) is False:
        return [], []
    queries = discovery_queries(config)
    per_query = max(1, min(30, int(config.get("repos_per_query", 10))))
    max_repos = max(1, int(config.get("max_repositories", 30)))
    cutoff_days = max(7, int(config.get("pushed_within_days", 180)))
    search_delay = max(0.0, float(config.get("search_delay_seconds", 2.25)))
    cutoff = datetime.now(UTC) - timedelta(days=cutoff_days)
    headers = _github_headers()

    repositories: dict[str, dict[str, Any]] = {}
    health: list[CollectorResult] = []
    for query_index, query in enumerate(queries):
        digest = hashlib.sha1(query.encode()).hexdigest()[:10]
        source = f"market-discovery:github:{digest}"
        try:
            response = await _get_with_retries(
                client,
                "https://api.github.com/search/repositories",
                params={
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": per_query,
                },
                headers=headers,
            )
            response.raise_for_status()
            items = response.json().get("items") or []
            for item in items:
                full_name = str(item.get("full_name") or "")
                pushed = str(item.get("pushed_at") or "")
                try:
                    pushed_at = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                except ValueError:
                    pushed_at = cutoff
                if full_name and pushed_at >= cutoff:
                    repositories[full_name] = item
            health.append(
                CollectorResult(
                    source=source,
                    postings=[],
                    complete=True,
                    mode="market-discovery",
                    rows_scanned=len(items),
                    expected_rows=len(items),
                    status="loaded",
                    note=f"GitHub repository search: {query}",
                )
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            blocked = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                403,
                429,
            }
            health.append(
                CollectorResult(
                    source=source,
                    postings=[],
                    complete=False,
                    mode="market-discovery",
                    rows_scanned=0,
                    error=None if blocked else repr(exc),
                    status="blocked" if blocked else "broken",
                    note=f"GitHub repository search failed after retries: {query}",
                )
            )
        if search_delay and query_index + 1 < len(queries):
            await asyncio.sleep(search_delay)

    ranked = sorted(
        repositories.values(),
        key=lambda item: (
            str(item.get("pushed_at") or ""),
            int(item.get("stargazers_count") or 0),
        ),
        reverse=True,
    )[:max_repos]
    postings: list[Posting] = []
    for repository in ranked:
        full_name = str(repository["full_name"])
        source = f"market-index:github:{full_name}"
        try:
            branch = quote(str(repository.get("default_branch") or "main"), safe="")
            response = await _get_with_retries(
                client,
                f"https://raw.githubusercontent.com/{full_name}/{branch}/README.md",
                attempts=3,
            )
            if response.status_code == 404:
                response = await _get_with_retries(
                    client,
                    f"https://api.github.com/repos/{full_name}/readme",
                    headers={**headers, "Accept": "application/vnd.github.raw+json"},
                    attempts=3,
                )
            response.raise_for_status()
            parsed = _document_postings(response.text, source=source, registry=False)
            for posting in parsed:
                posting.source_mode = "external-index"
            postings.extend(parsed)
            health.append(
                CollectorResult(
                    source=source,
                    postings=parsed,
                    complete=True,
                    mode="external-index",
                    rows_scanned=len(parsed),
                    expected_rows=len(parsed),
                    status="indexed",
                    note=f"discovered public feed {full_name}",
                )
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            blocked = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                403,
                429,
            }
            health.append(
                CollectorResult(
                    source=source,
                    postings=[],
                    complete=False,
                    mode="external-index",
                    rows_scanned=0,
                    error=None if blocked else repr(exc),
                    status="blocked" if blocked else "broken",
                    note=f"could not read {full_name} README after retries",
                )
            )

    deduped = {posting.posting_key: posting for posting in postings}
    return list(deduped.values()), health
