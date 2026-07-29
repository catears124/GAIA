from pathlib import Path

source_path = Path("src/gaia/employer_census.py")
source = source_path.read_text(encoding="utf-8")
marker = "def merge_observations_into_universe(database: Database) -> dict[str, int]:\n"
if marker not in source:
    raise SystemExit("merge_observations_into_universe marker not found")
prefix = source.split(marker, 1)[0]
replacement = '''def merge_observations_into_universe(database: Database) -> dict[str, int]:
    """Bulk-merge ecosystem employers into the evidence-backed employer census."""

    ensure_ecosystem_schema(database)
    now = datetime.now(UTC)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT
                observation_key,
                canonical_name,
                aliases,
                evidence_type,
                source,
                profile_url,
                official_url,
                internship_signal,
                technical_signal,
                first_seen_at,
                last_seen_at,
                metadata
            FROM employer_observations
            ORDER BY lower(canonical_name), source, observation_key
            """
        ).fetchall()
        if not rows:
            return {"observations": 0, "merged": 0, "inserted": 0}

        existing_keys = {
            str(row["employer_key"])
            for row in connection.execute(
                "SELECT employer_key FROM employer_universe"
            ).fetchall()
        }
        employers: dict[str, dict[str, object]] = {}
        evidence: dict[str, dict[str, object]] = {}

        for row in rows:
            raw_name = str(row["canonical_name"]).strip()
            name = canonical_company(raw_name) or raw_name
            key = _employer_key(name)
            recency_days = max(
                0.0,
                (now - row["last_seen_at"]).total_seconds() / 86400,
            )
            recency = math.exp(-recency_days / 730)
            internship = float(row["internship_signal"] or 0)
            technical = float(row["technical_signal"] or 0)
            score = round(
                100 * (0.42 * internship + 0.40 * technical + 0.18 * recency),
                3,
            )

            item = employers.setdefault(
                key,
                {
                    "name": name,
                    "aliases": set(),
                    "evidence_types": set(),
                    "sources": set(),
                    "count": 0,
                    "located": False,
                    "internship": 0.0,
                    "technical": 0.0,
                    "score": 0.0,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                },
            )
            aliases = item["aliases"]
            evidence_types = item["evidence_types"]
            sources = item["sources"]
            assert isinstance(aliases, set)
            assert isinstance(evidence_types, set)
            assert isinstance(sources, set)
            aliases.update(str(alias) for alias in (row["aliases"] or [name]) if alias)
            evidence_types.add(str(row["evidence_type"]))
            sources.add(str(row["source"]))
            item["count"] = int(item["count"]) + 1
            item["located"] = bool(item["located"] or row["official_url"])
            item["internship"] = max(float(item["internship"]), internship)
            item["technical"] = max(float(item["technical"]), technical)
            item["score"] = max(float(item["score"]), score)
            item["first_seen"] = min(item["first_seen"], row["first_seen_at"])
            item["last_seen"] = max(item["last_seen"], row["last_seen_at"])

            evidence_type = str(row["evidence_type"])
            source_name = str(row["source"])
            evidence_key = hashlib.sha256(
                f"{key}|{evidence_type}|{source_name}|ecosystem".encode()
            ).hexdigest()[:28]
            evidence_item = evidence.setdefault(
                evidence_key,
                {
                    "employer_key": key,
                    "evidence_type": evidence_type,
                    "source": source_name,
                    "first_seen": row["first_seen_at"],
                    "last_seen": row["last_seen_at"],
                    "sample_url": row["official_url"] or row["profile_url"],
                    "metadata": {},
                },
            )
            evidence_item["first_seen"] = min(
                evidence_item["first_seen"], row["first_seen_at"]
            )
            if row["last_seen_at"] >= evidence_item["last_seen"]:
                evidence_item["last_seen"] = row["last_seen_at"]
                evidence_item["sample_url"] = row["official_url"] or row["profile_url"]
            metadata = evidence_item["metadata"]
            assert isinstance(metadata, dict)
            metadata.update(dict(row["metadata"] or {}))

        universe_rows = []
        for key, item in employers.items():
            aliases = item["aliases"]
            evidence_types = item["evidence_types"]
            sources = item["sources"]
            assert isinstance(aliases, set)
            assert isinstance(evidence_types, set)
            assert isinstance(sources, set)
            technical = float(item["technical"])
            universe_rows.append(
                (
                    key,
                    item["name"],
                    sorted(aliases),
                    "located" if item["located"] else "candidate",
                    int(item["count"]),
                    sorted(evidence_types),
                    sorted(sources),
                    float(item["internship"]),
                    technical,
                    float(item["score"]),
                    technical >= 0.5,
                    item["first_seen"],
                    item["last_seen"],
                    now,
                )
            )

        connection.executemany(
            """
            INSERT INTO employer_universe(
                employer_key, canonical_name, aliases, resolution_status,
                evidence_count, evidence_types, evidence_sources,
                historical_years, historical_internships,
                current_index_mentions, direct_sources, direct_openings,
                technical_roles, internship_probability,
                technical_probability, frontier_score, blind_spot,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,ARRAY[]::SMALLINT[],0,0,0,0,0,
                %s,%s,%s,%s,%s,%s,%s
            )
            ON CONFLICT(employer_key) DO UPDATE SET
                aliases=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(employer_universe.aliases || excluded.aliases) AS value
                    ORDER BY value
                ),
                resolution_status=CASE
                    WHEN excluded.resolution_status='located' THEN 'located'
                    ELSE employer_universe.resolution_status
                END,
                evidence_count=employer_universe.evidence_count + excluded.evidence_count,
                evidence_types=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(
                        employer_universe.evidence_types || excluded.evidence_types
                    ) AS value
                    ORDER BY value
                ),
                evidence_sources=ARRAY(
                    SELECT DISTINCT value
                    FROM unnest(
                        employer_universe.evidence_sources || excluded.evidence_sources
                    ) AS value
                    ORDER BY value
                ),
                internship_probability=GREATEST(
                    employer_universe.internship_probability,
                    excluded.internship_probability
                ),
                technical_probability=GREATEST(
                    employer_universe.technical_probability,
                    excluded.technical_probability
                ),
                frontier_score=GREATEST(
                    employer_universe.frontier_score,
                    excluded.frontier_score
                ),
                blind_spot=(
                    employer_universe.current_index_mentions=0
                    AND GREATEST(
                        employer_universe.technical_probability,
                        excluded.technical_probability
                    ) >= 0.5
                ),
                first_seen_at=LEAST(
                    employer_universe.first_seen_at,
                    excluded.first_seen_at
                ),
                last_seen_at=GREATEST(
                    employer_universe.last_seen_at,
                    excluded.last_seen_at
                ),
                updated_at=now()
            WHERE employer_universe.resolution_status!='enumerated'
            """,
            universe_rows,
        )

        evidence_rows = [
            (
                evidence_key,
                item["employer_key"],
                item["evidence_type"],
                item["source"],
                item["first_seen"],
                item["last_seen"],
                item["sample_url"],
                Jsonb(item["metadata"]),
            )
            for evidence_key, item in evidence.items()
        ]
        connection.executemany(
            """
            INSERT INTO employer_evidence(
                evidence_key, employer_key, evidence_type, source, event_year,
                role_count, active_roles, first_seen_at, last_seen_at,
                sample_url, metadata
            ) VALUES (%s,%s,%s,%s,NULL,0,0,%s,%s,%s,%s)
            ON CONFLICT(evidence_key) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                sample_url=excluded.sample_url,
                metadata=excluded.metadata
            """,
            evidence_rows,
        )

    inserted = sum(key not in existing_keys for key in employers)
    return {
        "observations": len(rows),
        "merged": len(rows) - inserted,
        "inserted": inserted,
    }
'''
source_path.write_text(prefix + replacement, encoding="utf-8")

workflow_path = Path(".github/workflows/reconcile.yml")
workflow = workflow_path.read_text(encoding="utf-8")
old_status = '''          state=success
          description="Production read models are current"
          if [ "${{ job.status }}" != "success" ]; then
            state=failure
            description="Production read-model reconciliation failed"
          fi
'''
new_status = '''          case "${{ job.status }}" in
            success)
              state=success
              description="Production read models are current"
              ;;
            cancelled)
              state=pending
              description="Reconciliation superseded by a newer pulse"
              ;;
            *)
              state=failure
              description="Production read-model reconciliation failed"
              ;;
          esac
'''
if old_status not in workflow:
    raise SystemExit("expected reconcile status block not found")
workflow_path.write_text(workflow.replace(old_status, new_status, 1), encoding="utf-8")

test_path = Path("tests/test_bulk_employer_reconcile.py")
test_path.write_text(
    '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.types.json import Jsonb

from gaia.db import Database
from gaia.employer_census import merge_observations_into_universe
from gaia.universe import _employer_key


def test_ecosystem_observations_merge_in_bulk(tmp_path) -> None:
    database = Database(tmp_path / "bulk-employer-reconcile.db")
    now = datetime.now(UTC)
    with database.connect() as connection:
        connection.executemany(
            """
            INSERT INTO employer_observations(
                observation_key, canonical_name, aliases, evidence_type, source,
                profile_url, official_url, internship_signal, technical_signal,
                first_seen_at, last_seen_at, metadata
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (
                    "obs-1",
                    "Example Labs",
                    ["Example Labs"],
                    "startup-ecosystem",
                    "yc:test",
                    "https://example.test/profile",
                    None,
                    0.4,
                    0.9,
                    now - timedelta(days=2),
                    now - timedelta(days=1),
                    Jsonb({"batch": "W26"}),
                ),
                (
                    "obs-2",
                    "Example Labs",
                    ["Example"],
                    "employer-page",
                    "directory:test",
                    "https://example.test/directory",
                    "https://example.test",
                    0.8,
                    0.7,
                    now - timedelta(days=3),
                    now,
                    Jsonb({"verified": True}),
                ),
            ],
        )

    result = merge_observations_into_universe(database)

    with database.connect() as connection:
        employer = connection.execute(
            "SELECT * FROM employer_universe WHERE employer_key=%s",
            (_employer_key("Example Labs"),),
        ).fetchone()
        evidence = connection.execute(
            "SELECT COUNT(*) AS count FROM employer_evidence WHERE employer_key=%s",
            (_employer_key("Example Labs"),),
        ).fetchone()

    assert result == {"observations": 2, "merged": 1, "inserted": 1}
    assert employer is not None
    assert employer["resolution_status"] == "located"
    assert employer["evidence_count"] == 2
    assert set(employer["aliases"]) == {"Example", "Example Labs"}
    assert employer["internship_probability"] == 0.8
    assert employer["technical_probability"] == 0.9
    assert evidence["count"] == 2


def test_reconcile_has_no_per_observation_database_loop() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "gaia" / "employer_census.py"
    ).read_text(encoding="utf-8")
    function = source.split(
        "def merge_observations_into_universe(database: Database)", 1
    )[1]

    assert 'SELECT * FROM employer_universe WHERE employer_key=%s' not in function
    assert function.count("connection.executemany(") == 2


def test_superseded_reconcile_status_is_not_failure() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "reconcile.yml"
    ).read_text(encoding="utf-8")

    assert "Reconciliation superseded by a newer pulse" in workflow
    assert "state=pending" in workflow
''',
    encoding="utf-8",
)
