from __future__ import annotations

from typing import Any

import httpx

from .classify import classify
from .collectors import Collector, locations_from, parse_date, text
from .models import CollectorResult, Posting


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flat_location(value: Any) -> list[str]:
    if isinstance(value, dict):
        joined = ", ".join(
            text(value.get(key))
            for key in ("city", "region", "state", "country")
            if text(value.get(key))
        )
        return [joined] if joined else locations_from(value)
    return locations_from(value)


class SmartRecruitersCollector(Collector):
    mode = "board"

    def __init__(self, company: str, identifier: str) -> None:
        self.company = company
        self.identifier = identifier
        self.name = f"smartrecruiters:{identifier}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        postings: list[Posting] = []
        offset = 0
        total: int | None = None
        while total is None or offset < total:
            response = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{self.identifier}/postings",
                params={"limit": 100, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            rows = list(payload.get("content") or payload.get("postings") or [])
            total = int(payload.get("totalFound") or payload.get("total") or len(rows))
            if not rows:
                break
            for job in rows:
                source_id = text(_first(job, "id", "uuid", "refNumber"))
                title = text(_first(job, "name", "title"))
                url = text(_first(job, "applyUrl", "referralUrl", "jobAdUrl"))
                if not url and source_id:
                    url = f"https://jobs.smartrecruiters.com/{self.identifier}/{source_id}"
                posted = parse_date(_first(job, "releasedDate", "createdOn", "createdAt"))
                postings.append(
                    classify(
                        Posting(
                            company=self.company,
                            title=title,
                            apply_url=url,
                            source=self.name,
                            source_id=source_id or url,
                            locations=_flat_location(job.get("location")),
                            employment_type=text(_first(job, "typeOfEmployment", "employmentType")),
                            posted_at=posted,
                            posted_raw=text(_first(job, "releasedDate", "createdOn", "createdAt")) or None,
                            posted_precision="timestamp" if posted else "unknown",
                            posted_confidence="official" if posted else "unknown",
                        )
                    )
                )
            offset += len(rows)
        complete = total is not None and offset >= total
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=complete,
            mode=self.mode,
            rows_scanned=offset,
            expected_rows=total,
            status="ok" if complete else "truncated",
        )


class RecruiteeCollector(Collector):
    mode = "board"

    def __init__(self, company: str, subdomain: str) -> None:
        self.company = company
        self.subdomain = subdomain
        self.name = f"recruitee:{subdomain}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(f"https://{self.subdomain}.recruitee.com/api/offers/")
        response.raise_for_status()
        payload = response.json()
        rows = list(payload.get("offers") or payload.get("content") or payload.get("results") or [])
        postings: list[Posting] = []
        for job in rows:
            source_id = text(_first(job, "id", "slug", "offer_id"))
            url = text(_first(job, "careers_url", "url", "apply_url"))
            if not url and source_id:
                url = f"https://{self.subdomain}.recruitee.com/o/{source_id}"
            posted_raw = text(_first(job, "published_at", "publishedAt", "created_at"))
            posted = parse_date(posted_raw)
            location = _first(job, "location", "locations")
            if not location:
                location = {
                    "city": job.get("city"),
                    "region": job.get("state"),
                    "country": job.get("country"),
                }
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(_first(job, "title", "name")),
                        apply_url=url,
                        source=self.name,
                        source_id=source_id or url,
                        locations=_flat_location(location),
                        description=text(_first(job, "description", "description_plain")),
                        employment_type=text(_first(job, "employment_type", "contract_type")),
                        posted_at=posted,
                        posted_raw=posted_raw or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(rows),
            expected_rows=len(rows),
            status="ok",
        )


class WorkableCollector(Collector):
    mode = "board"

    def __init__(self, company: str, subdomain: str) -> None:
        self.company = company
        self.subdomain = subdomain
        self.name = f"workable:{subdomain}"

    async def collect(self, client: httpx.AsyncClient) -> CollectorResult:
        response = await client.get(f"https://www.workable.com/api/accounts/{self.subdomain}")
        response.raise_for_status()
        payload = response.json()
        rows = list(
            payload.get("jobs")
            or payload.get("results")
            or payload.get("content")
            or (payload.get("data") or {}).get("jobs")
            or []
        )
        postings: list[Posting] = []
        for job in rows:
            source_id = text(_first(job, "shortcode", "code", "id"))
            url = text(_first(job, "url", "application_url", "shortlink"))
            if not url and source_id:
                url = f"https://apply.workable.com/{self.subdomain}/j/{source_id}/"
            posted_raw = text(_first(job, "published_at", "created_at", "createdAt"))
            posted = parse_date(posted_raw)
            location = job.get("location") or {
                "city": job.get("city"),
                "region": job.get("state"),
                "country": job.get("country"),
            }
            postings.append(
                classify(
                    Posting(
                        company=self.company,
                        title=text(_first(job, "title", "name")),
                        apply_url=url,
                        source=self.name,
                        source_id=source_id or url,
                        locations=_flat_location(location),
                        description=text(_first(job, "description", "description_plain")),
                        employment_type=text(_first(job, "employment_type", "type")),
                        posted_at=posted,
                        posted_raw=posted_raw or None,
                        posted_precision="timestamp" if posted else "unknown",
                        posted_confidence="official" if posted else "unknown",
                    )
                )
            )
        return CollectorResult(
            source=self.name,
            postings=postings,
            complete=True,
            mode=self.mode,
            rows_scanned=len(rows),
            expected_rows=len(rows),
            status="ok",
        )
