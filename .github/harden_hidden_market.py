from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


census = Path("src/gaia/employer_census.py")
text = census.read_text(encoding="utf-8")
marker = "\ndef _sbir_observations(\n"
helper = '''

def _normalize_web_url(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"https://{candidate.lstrip('/')}"
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return candidate
'''
if marker not in text:
    raise SystemExit("SBIR observation marker missing")
text = text.replace(marker, helper + marker, 1)
text = text.replace(
    '        official_url = str(record.get("company_url") or "").strip()\n',
    '        official_url = _normalize_web_url(record.get("company_url"))\n',
    1,
)
old = '''            page_records = _json_records(response.json())
            records.extend(page_records)
            if len(page_records) < rows:
                break
    return _sbir_observations(records, source_url=url)
'''
new = '''            page_records = _json_records(response.json())
            records.extend(page_records)
            if len(page_records) < rows:
                break
    deduplicated: dict[str, dict[str, object]] = {}
    for record in records:
        identity = str(record.get("award_link") or "").strip() or "|".join(
            (
                str(record.get("firm") or record.get("company_name") or "").casefold(),
                str(record.get("award_title") or "").casefold(),
                str(record.get("award_year") or record.get("solicitation_year") or ""),
                str(record.get("agency") or "").casefold(),
            )
        )
        deduplicated.setdefault(identity, record)
    return _sbir_observations(list(deduplicated.values()), source_url=url)
'''
if old not in text:
    raise SystemExit("SBIR pagination block missing")
text = text.replace(old, new, 1)
text = text.replace(
    '            score = round(100 * (0.42 * internship + 0.40 * technical + 0.18 * recency), 3)\n',
    '            score = round(100 * internship * technical * (0.75 + 0.25 * recency), 3)\n',
    1,
)
text = text.replace(
    '                            blind_spot=(current_index_mentions=0 AND %s >= 0.5),\n',
    '                            blind_spot=(current_index_mentions=0 AND %s >= 0.15),\n',
    1,
)
text = text.replace(
    '                            technical,\n                            row["first_seen_at"],\n',
    '                            internship * technical,\n                            row["first_seen_at"],\n',
    1,
)
text = text.replace(
    '                        technical >= 0.5,\n',
    '                        internship * technical >= 0.15,\n',
    1,
)
census.write_text(text, encoding="utf-8")

replace_once(
    "src/gaia/universe.py",
    '''            frontier = 0.0 if direct_count else 100 * (
                0.42 * internship
                + 0.30 * technical
                + 0.12 * recency
                + 0.10 * repeated_history
                + 0.06 * outside_registry
            )
''',
    '''            frontier = 0.0 if direct_count else 100 * internship * technical * (
                0.60
                + 0.20 * recency
                + 0.10 * repeated_history
                + 0.10 * outside_registry
            )
''',
)
replace_once(
    "src/gaia/universe.py",
    '''                and technical >= 0.5
''',
    '''                and internship * technical >= 0.15
''',
)

tests = Path("tests/test_employer_universe.py")
text = tests.read_text(encoding="utf-8")
text = text.replace(
    '''    _career_links,
    _sbir_observations,
''',
    '''    _career_links,
    _normalize_web_url,
    _sbir_observations,
''',
    1,
)
text = text.replace(
    '                "company_url": "https://quietphotonics.example",\n',
    '                "company_url": "quietphotonics.example",\n',
    1,
)
first_record = '''            {
                "firm": "Quiet Photonics LLC",
                "company_url": "quietphotonics.example",
                "city": "Huntsville",
                "state": "AL",
                "agency": "NASA",
                "award_year": 2025,
                "award_title": "Compact optical navigation",
                "research_area_keywords": "photonics; navigation; embedded systems",
                "uei": "EXAMPLE12345",
            },
'''
if first_record not in text:
    raise SystemExit("SBIR test record missing")
text = text.replace(first_record, first_record + first_record, 1)
if "test_hidden_market_score_requires_both_internship_and_technical_signal" not in text:
    text += '''

def test_normalize_web_url_requires_an_http_host() -> None:
    assert _normalize_web_url("quiet.example") == "https://quiet.example"
    assert _normalize_web_url("ftp://quiet.example") == ""
    assert _normalize_web_url("") == ""


def test_hidden_market_score_requires_both_internship_and_technical_signal(tmp_path) -> None:
    database = Database(tmp_path / "frontier-score.db")
    ensure_universe_schema(database)
    ensure_ecosystem_schema(database)
    _upsert_observations(
        database,
        source="sbir:recent",
        evidence_type="federal-rd-award",
        internship_signal=0.18,
        technical_signal=0.98,
        observations=[
            {
                "name": "Federal R&D Systems",
                "profile_url": "https://federal-rd.example",
                "official_url": "https://federal-rd.example",
                "location": "",
                "sectors": ["photonics"],
                "metadata": {},
            }
        ],
    )
    _upsert_observations(
        database,
        source="yc:hard-tech",
        evidence_type="startup-ecosystem",
        internship_signal=0.30,
        technical_signal=0.94,
        observations=[
            {
                "name": "Internship-Friendly Robotics",
                "profile_url": "https://robotics.example",
                "official_url": "https://robotics.example",
                "location": "",
                "sectors": ["robotics"],
                "metadata": {},
            }
        ],
    )

    rebuild_employer_universe(database)
    merge_observations_into_universe(database)
    scores = {
        item["canonical_name"]: item["frontier_score"]
        for item in universe_summary(database)["frontier"]
    }

    assert scores["Internship-Friendly Robotics"] > scores["Federal R&D Systems"]
'''
tests.write_text(text, encoding="utf-8")
