from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_census() -> None:
    path = Path("src/gaia/employer_census.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from urllib.parse import urljoin, urlsplit\n",
        "from urllib.parse import urlencode, urljoin, urlsplit\n",
        1,
    )
    text = text.replace(
        '''CAREER_MARKERS = (
    "career",
    "careers",
    "job",
    "jobs",
    "join-us",
    "join_us",
    "work-with-us",
    "open-positions",
    "open-roles",
)
''',
        '''CAREER_MARKERS = (
    "career",
    "careers",
    "job",
    "jobs",
    "join-us",
    "join_us",
    "work-with-us",
    "open-positions",
    "open-roles",
    "openings",
    "opportunities",
)
ATS_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
    "oraclecloud.com",
    "successfactors.com",
)
''',
        1,
    )

    marker = "\ndef _upsert_observations(\n"
    helpers = '''

def _json_records(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "result", "data", "awards", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    for value in payload.values():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return [dict(item) for item in value]
    return []


def _split_keywords(value: object) -> list[str]:
    if isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        values = re.split(r"[,;|]", str(value or ""))
    return sorted({item for item in values if item})[:30]


def _sbir_observations(records: list[dict[str, object]], *, source_url: str) -> list[dict[str, object]]:
    companies: dict[str, dict[str, object]] = {}
    for record in records:
        raw_name = str(record.get("firm") or record.get("company_name") or "").strip()
        name = canonical_company(raw_name)
        if not name:
            continue
        official_url = str(record.get("company_url") or "").strip()
        key = f"{name.casefold()}|{urlsplit(official_url).netloc.casefold()}"
        item = companies.setdefault(
            key,
            {
                "name": name,
                "profile_url": official_url or (
                    "https://www.sbir.gov/awards?" + urlencode({"company_name": name})
                ),
                "official_url": official_url,
                "location": ", ".join(
                    value
                    for value in (
                        str(record.get("city") or "").strip(),
                        str(record.get("state") or "").strip(),
                    )
                    if value
                ),
                "sectors": [],
                "metadata": {
                    "source_url": source_url,
                    "award_count": 0,
                    "agencies": [],
                    "award_years": [],
                    "sample_awards": [],
                },
            },
        )
        sectors = set(item["sectors"])
        sectors.update(_split_keywords(record.get("research_area_keywords")))
        item["sectors"] = sorted(sectors)[:30]
        metadata = dict(item["metadata"])
        metadata["award_count"] = int(metadata.get("award_count") or 0) + 1
        metadata["agencies"] = sorted(
            {*(metadata.get("agencies") or []), str(record.get("agency") or "").strip()} - {""}
        )
        year = record.get("award_year") or record.get("solicitation_year")
        metadata["award_years"] = sorted(
            {*(metadata.get("award_years") or []), str(year or "").strip()} - {""}
        )
        samples = list(metadata.get("sample_awards") or [])
        title = str(record.get("award_title") or "").strip()
        if title and title not in samples and len(samples) < 5:
            samples.append(title)
        metadata["sample_awards"] = samples
        if record.get("uei"):
            metadata["uei"] = str(record["uei"])
        if record.get("number_employees"):
            metadata["number_employees"] = record["number_employees"]
        item["metadata"] = metadata
    return list(companies.values())


async def _sbir_feed(
    client: httpx.AsyncClient,
    item: Mapping[str, object],
) -> list[dict[str, object]]:
    url = str(item.get("url") or "https://api.www.sbir.gov/public/api/awards")
    rows = max(1, min(5000, int(item.get("rows") or 5000)))
    max_pages = max(1, min(10, int(item.get("max_pages") or 2)))
    years = [int(value) for value in item.get("years") or []]
    if not years:
        years = [datetime.now(UTC).year]
    records: list[dict[str, object]] = []
    for year in years:
        for page in range(max_pages):
            response = await client.get(
                url,
                params={"year": year, "rows": rows, "start": page * rows},
            )
            response.raise_for_status()
            page_records = _json_records(response.json())
            records.extend(page_records)
            if len(page_records) < rows:
                break
    return _sbir_observations(records, source_url=url)
'''
    if marker not in text:
        raise SystemExit("upsert marker missing")
    text = text.replace(marker, helpers + marker, 1)

    text = text.replace(
        '''                source,
                profile_url,
                str(item.get("location") or "") or None,
''',
        '''                source,
                profile_url,
                str(item.get("official_url") or "") or None,
                str(item.get("location") or "") or None,
''',
        1,
    )
    text = text.replace(
        '''                profile_url, location, sectors, internship_signal, technical_signal,
                metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
''',
        '''                profile_url, official_url, location, sectors,
                internship_signal, technical_signal, metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
''',
        1,
    )
    text = text.replace(
        '''                location=COALESCE(excluded.location, employer_observations.location),
''',
        '''                official_url=COALESCE(
                    excluded.official_url,
                    employer_observations.official_url
                ),
                location=COALESCE(excluded.location, employer_observations.location),
''',
        1,
    )

    text = text.replace(
        '''def _career_links(body: str, base_url: str, *, same_host_only: bool) -> list[str]:
    base_host = urlsplit(base_url).netloc.casefold()
    output: list[str] = []
    for score, url in _external_links(body, base_url):
        parts = urlsplit(url)
        if same_host_only and parts.netloc.casefold() != base_host:
            continue
        text = f"{parts.path} {parts.query}".casefold()
        if score >= 80 or any(marker in text for marker in CAREER_MARKERS):
            output.append(url)
    return list(dict.fromkeys(output))
''',
        '''def _is_ats_host(host: str) -> bool:
    normalized = host.casefold().split(":", 1)[0]
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in ATS_HOST_SUFFIXES)


def _career_links(body: str, base_url: str, *, same_host_only: bool) -> list[str]:
    base_host = urlsplit(base_url).netloc.casefold()
    output: list[str] = []
    for score, url in _external_links(body, base_url):
        parts = urlsplit(url)
        host = parts.netloc.casefold().split(":", 1)[0]
        if host in EXCLUDED_PROFILE_HOSTS:
            continue
        if same_host_only and parts.netloc.casefold() != base_host:
            continue
        text = f"{parts.path} {parts.query}".casefold()
        if _is_ats_host(host) or score >= 80 or any(marker in text for marker in CAREER_MARKERS):
            output.append(url)
    return list(dict.fromkeys(output))
''',
        1,
    )

    text = text.replace(
        '''        official_url, homepage = await _fetch_text(client, official_url)
        candidate_urls = [official_url]
        candidate_urls.extend(_career_links(homepage, official_url, same_host_only=True)[:6])
''',
        '''        official_url, homepage = await _fetch_text(client, official_url)
        candidate_urls = [official_url]
        career_urls = _career_links(homepage, official_url, same_host_only=False)[:12]
        candidate_urls.extend(career_urls)
        official_host = urlsplit(official_url).netloc.casefold()
        for career_url in career_urls[:6]:
            if urlsplit(career_url).netloc.casefold() != official_host:
                continue
            try:
                final_url, career_body = await _fetch_text(client, career_url)
            except httpx.HTTPError:
                continue
            candidate_urls.extend(
                _career_links(career_body, final_url, same_host_only=False)[:16]
            )
''',
        1,
    )

    old_loop = '''            if kind != "yc-directory" or not url:
                continue
            source = str(item.get("name") or url)
            try:
                response = await client.get(url)
                response.raise_for_status()
                observations = _yc_observations(
                    response.text,
                    url=url,
                    source=source,
                    sectors=[str(value) for value in item.get("sectors") or []],
                )
                observed += _upsert_observations(
                    database,
                    source=f"yc:{source}",
                    evidence_type="startup-ecosystem",
                    internship_signal=float(item.get("internship_signal") or 0.32),
                    technical_signal=float(item.get("technical_signal") or 0.86),
                    observations=observations,
                )
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                LOGGER.warning("employer ecosystem feed failed: %s: %r", source, exc)
'''
    new_loop = '''            source = str(item.get("name") or url or kind)
            try:
                if kind == "yc-directory" and url:
                    response = await client.get(url)
                    response.raise_for_status()
                    observations = _yc_observations(
                        response.text,
                        url=url,
                        source=source,
                        sectors=[str(value) for value in item.get("sectors") or []],
                    )
                    evidence_type = "startup-ecosystem"
                elif kind == "sbir-awards":
                    observations = await _sbir_feed(client, item)
                    evidence_type = "federal-rd-award"
                else:
                    continue
                observed += _upsert_observations(
                    database,
                    source=f"{kind}:{source}",
                    evidence_type=evidence_type,
                    internship_signal=float(item.get("internship_signal") or 0.25),
                    technical_signal=float(item.get("technical_signal") or 0.86),
                    observations=observations,
                )
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                LOGGER.warning("employer ecosystem feed failed: %s: %r", source, exc)
'''
    if old_loop not in text:
        raise SystemExit("refresh loop missing")
    text = text.replace(old_loop, new_loop, 1)
    path.write_text(text, encoding="utf-8")


def patch_sources() -> None:
    path = Path("src/gaia/default_sources.yaml")
    text = path.read_text(encoding="utf-8")
    marker = "employer_ecosystems:\n"
    if marker not in text:
        raise SystemExit("ecosystem config missing")
    addition = '''employer_ecosystems:
  # Recent federal R&D awardees expose obscure technical small businesses that rarely
  # appear in prestige-oriented internship repositories. The official SBIR API is
  # evidence only; company websites and hiring surfaces still require validation.
  - kind: sbir-awards
    name: recent-federal-rd-awardees
    url: https://api.www.sbir.gov/public/api/awards
    years: [2024, 2025, 2026]
    rows: 5000
    max_pages: 2
    internship_signal: 0.18
    technical_signal: 0.98
'''
    text = text.replace(marker, addition, 1)
    path.write_text(text, encoding="utf-8")


def patch_frontend() -> None:
    path = Path("src/gaia/frontend/app-v2.js")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '''    "historical-direct": "past direct application",
''',
        '''    "historical-direct": "past direct application",
    "federal-rd-award": "recent federal R&D award",
    "startup-ecosystem": "technical startup ecosystem",
''',
        1,
    )
    path.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    path = Path("tests/test_employer_universe.py")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '''    _upsert_observations,
    _yc_observations,
''',
        '''    _career_links,
    _sbir_observations,
    _upsert_observations,
    _yc_observations,
''',
        1,
    )
    if "test_sbir_awards_aggregate_obscure_technical_employers" not in text:
        text += '''

def test_sbir_awards_aggregate_obscure_technical_employers() -> None:
    rows = _sbir_observations(
        [
            {
                "firm": "Quiet Photonics LLC",
                "company_url": "https://quietphotonics.example",
                "city": "Huntsville",
                "state": "AL",
                "agency": "NASA",
                "award_year": 2025,
                "award_title": "Compact optical navigation",
                "research_area_keywords": "photonics; navigation; embedded systems",
                "uei": "EXAMPLE12345",
            },
            {
                "firm": "Quiet Photonics LLC",
                "company_url": "https://quietphotonics.example",
                "city": "Huntsville",
                "state": "AL",
                "agency": "DOE",
                "award_year": 2026,
                "award_title": "Radiation-tolerant sensing",
                "research_area_keywords": "sensors, photonics",
            },
        ],
        source_url="https://api.www.sbir.gov/public/api/awards",
    )

    assert len(rows) == 1
    assert rows[0]["name"] == "Quiet Photonics LLC"
    assert rows[0]["official_url"] == "https://quietphotonics.example"
    assert rows[0]["metadata"]["award_count"] == 2
    assert rows[0]["metadata"]["agencies"] == ["DOE", "NASA"]
    assert rows[0]["sectors"] == [
        "embedded systems",
        "navigation",
        "photonics",
        "sensors",
    ]


def test_career_links_follow_external_ats_but_not_social_profiles() -> None:
    body = """
    <a href="https://jobs.ashbyhq.com/quiet">Open positions</a>
    <a href="/careers">Careers</a>
    <a href="https://www.linkedin.com/company/quiet">Jobs on LinkedIn</a>
    """

    links = _career_links(body, "https://quiet.example", same_host_only=False)

    assert "https://jobs.ashbyhq.com/quiet" in links
    assert "https://quiet.example/careers" in links
    assert not any("linkedin.com" in link for link in links)


def test_observation_keeps_official_company_url(tmp_path) -> None:
    database = Database(tmp_path / "official-url.db")
    ensure_ecosystem_schema(database)
    _upsert_observations(
        database,
        source="sbir:recent",
        evidence_type="federal-rd-award",
        internship_signal=0.18,
        technical_signal=0.98,
        observations=[
            {
                "name": "Quiet Photonics LLC",
                "profile_url": "https://www.sbir.gov/awards?company_name=Quiet+Photonics+LLC",
                "official_url": "https://quietphotonics.example",
                "location": "Huntsville, AL",
                "sectors": ["photonics"],
                "metadata": {},
            }
        ],
    )

    with database.connect() as connection:
        row = connection.execute(
            "SELECT official_url FROM employer_observations"
        ).fetchone()

    assert row["official_url"] == "https://quietphotonics.example"
'''
    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_census()
    patch_sources()
    patch_frontend()
    patch_tests()


if __name__ == "__main__":
    main()
