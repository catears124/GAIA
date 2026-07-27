from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

from .quality import canonical_company

load_dotenv()

TARGET_MATCHES = {"exact", "year_confirmed", "source_confirmed"}
TARGET_RANK = {
    "not_internship": -1,
    "wrong_year": -1,
    "wrong_season": -1,
    "unknown": 0,
    "source_confirmed": 1,
    "year_confirmed": 2,
    "exact": 3,
}
SOURCE_RANK = {
    "direct": 0,
    "verification": 1,
    "verification-lead": 2,
    "external-index": 2,
    "registry": 3,
    "universe-seed": 4,
}
EMPLOYER_DATE_MODES = {"direct", "verification"}
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CompatRow(dict[str, Any]):
    """Mapping row with SQLite-compatible positional access for existing tests/tools."""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def __iter__(self):
        return iter(self.values())


def _compat_row_factory(cursor):
    make_dict = dict_row(cursor)

    def make_row(values):
        return CompatRow(make_dict(values))

    return make_row


class ConnectionAdapter:
    """Thin psycopg wrapper that accepts legacy qmark placeholders during migration."""

    def __init__(self, connection: psycopg.Connection[CompatRow]) -> None:
        self._connection = connection

    @staticmethod
    def _query(query: str) -> str:
        if query.strip().lower() == "select locations_json from postings":
            return "SELECT array_to_json(locations)::text AS locations_json FROM postings"
        query = re.sub(r"\bactive\s*=\s*1\b", "active", query, flags=re.I)
        query = re.sub(r"\bactive\s*=\s*0\b", "NOT active", query, flags=re.I)
        return query.replace("?", "%s")

    def execute(self, query: str, params: Any = None):
        return self._connection.execute(self._query(query), params)

    def executemany(self, query: str, params_seq: Any):
        return self._connection.executemany(self._query(query), params_seq)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        value = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def application_identity(url: str, source: str, source_id: str) -> str:
    """Collapse copies from different feeds without merging distinct requisitions."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)

    if gh_jid := (query.get("gh_jid") or query.get("job_id")):
        return f"greenhouse:{gh_jid[0]}"
    if "greenhouse" in host:
        if match := re.search(r"/(?:jobs?|apply)/(\d+)(?:/|$)", path):
            return f"greenhouse:{match.group(1)}"
    if host == "jobs.lever.co":
        if match := UUID_RE.search(path):
            return f"lever:{match.group(0).lower()}"
    if host == "jobs.ashbyhq.com":
        if match := UUID_RE.search(path):
            return f"ashby:{match.group(0).lower()}"
    if "google.com" in host:
        if match := re.search(r"/jobs/results/(\d+)", path):
            return f"google:{match.group(1)}"
    if "smartrecruiters.com" in host:
        if match := re.search(r"/(\d{8,})(?:/|$)", path):
            return f"smartrecruiters:{match.group(1)}"
    if host.endswith("amazon.jobs"):
        if match := re.search(r"/jobs/(\d+)(?:/|$)", path):
            return f"amazon:{match.group(1)}"
    if "myworkdayjobs.com" in host:
        if match := re.search(r"_([A-Za-z]{0,6}\d[A-Za-z0-9-]*)$", path):
            return f"workday:{host}:{match.group(1).lower()}"

    if path.endswith("/apply"):
        path = path[: -len("/apply")]
    normalized = urlunsplit((parts.scheme.lower(), host, path or "/", parts.query, ""))
    return normalized or f"{source}:{source_id}"


def coverage_role_signature(company: str, title: str) -> str:
    """Match benchmark roles across employer URL and punctuation changes."""
    aliases = {
        "engineering": "engineer",
        "internship": "intern",
        "internships": "intern",
    }
    ignored = {"or", "summer", "2027"}
    tokens = [
        aliases.get(token, token)
        for token in re.findall(r"[a-z0-9+#]+", title.lower())
        if token not in ignored
    ]
    return f"{canonical_company(company).casefold()}:{' '.join(tokens)}"


def _source_sort(row: Mapping[str, Any]) -> tuple[int, bool, int, str]:
    return (
        SOURCE_RANK.get(str(row["source_mode"]), 99),
        row["posted_at"] is None,
        len(str(row["title"])),
        str(row["title"]),
    )


def _target_sort(row: Mapping[str, Any]) -> tuple[int, int]:
    return (
        TARGET_RANK.get(str(row["target_match"]), -2),
        -SOURCE_RANK.get(str(row["source_mode"]), 99),
    )


def _database_url(value: str | None) -> str:
    url = (
        value
        or os.getenv("GAIA_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
    )
    if not url:
        raise RuntimeError(
            "PostgreSQL is not configured. Set GAIA_DATABASE_URL (or DATABASE_URL) "
            "to the Supabase pooler connection string."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "supabase.com" in url and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


class BaseDatabase:
    def __init__(
        self,
        url: str | Path | None = None,
        *,
        schema: str | None = None,
        migrate: bool | None = None,
    ) -> None:
        legacy_path = isinstance(url, Path) or (
            isinstance(url, str) and "://" not in url and not url.startswith("postgres")
        )
        if legacy_path:
            test_url = os.getenv("GAIA_TEST_DATABASE_URL")
            if not test_url:
                raise RuntimeError(
                    "SQLite paths are no longer supported. Set GAIA_DATABASE_URL to PostgreSQL. "
                    "Tests may set GAIA_TEST_DATABASE_URL to map temporary paths to isolated schemas."
                )
            identity = str(Path(url))
            schema = schema or f"test_{hashlib.sha1(identity.encode()).hexdigest()[:16]}"
            url = test_url

        self.url = _database_url(str(url) if url is not None else None)
        self.path = self
        self.schema = schema or os.getenv("GAIA_SCHEMA", "public")
        if not SCHEMA_RE.fullmatch(self.schema):
            raise ValueError(f"invalid PostgreSQL schema name: {self.schema!r}")
        self.timeout = max(1, int(float(os.getenv("GAIA_DB_TIMEOUT", "60"))))
        if migrate is None:
            migrate = os.getenv("GAIA_AUTO_MIGRATE", "0" if os.getenv("VERCEL") else "1") == "1"
        if migrate:
            self.migrate()

    @contextmanager
    def connect(self) -> Iterator[ConnectionAdapter]:
        connection = psycopg.connect(
            self.url,
            row_factory=_compat_row_factory,
            connect_timeout=self.timeout,
            application_name="gaia",
            prepare_threshold=None,
        )
        try:
            connection.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema))
            )
            connection.execute(
                sql.SQL("SET statement_timeout TO {}").format(sql.Literal(f"{self.timeout}s"))
            )
            yield ConnectionAdapter(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with psycopg.connect(
            self.url,
            row_factory=dict_row,
            connect_timeout=self.timeout,
            application_name="gaia-migrate",
            prepare_threshold=None,
        ) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
            )
            connection.execute(
                sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema))
            )
            connection.execute(schema_sql)

    def drop_schema(self) -> None:
        if self.schema == "public":
            raise RuntimeError("refusing to drop the public schema")
        with psycopg.connect(
            self.url,
            connect_timeout=self.timeout,
            application_name="gaia-test-cleanup",
            prepare_threshold=None,
        ) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )

    def start_run(self) -> int:
        now = datetime.now(UTC)
        stale_after = max(300, int(os.getenv("GAIA_SYNC_LOCK_TIMEOUT", "7200")))
        cutoff = now - timedelta(seconds=stale_after)
        with self.connect() as db:
            db.execute(
                """
                UPDATE sync_runs
                SET finished_at=%s, status='cancelled'
                WHERE status='running' AND started_at<%s
                """,
                (now, cutoff),
            )
            row = db.execute(
                """
                INSERT INTO sync_runs(started_at, status)
                VALUES (%s, 'running')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (now,),
            ).fetchone()
            if row is None:
                running = db.execute(
                    "SELECT id FROM sync_runs WHERE status='running' LIMIT 1"
                ).fetchone()
                run_id = int(running["id"]) if running else "unknown"
                raise RuntimeError(f"sync run {run_id} is already running")
            return int(row["id"])

    def seed_benchmark_corpus(self, *, version: str = "v1", limit: int = 500) -> int:
        """Freeze a deterministic, production-derived classification regression corpus."""
        if limit <= 0:
            return 0
        captured_at = datetime.now(UTC)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT posting_key, company, title, employment_type, source_mode,
                       category, target_match
                FROM postings
                WHERE active
                ORDER BY
                    CASE source_mode WHEN 'direct' THEN 0 WHEN 'registry' THEN 1 ELSE 2 END,
                    target_match,
                    category,
                    lower(company),
                    lower(title),
                    posting_key
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            db.executemany(
                """
                INSERT INTO benchmark_cases(
                    version, posting_key, company, title, employment_type, source_mode,
                    expected_category, expected_target_match, captured_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                [
                    (
                        version,
                        row["posting_key"],
                        row["company"],
                        row["title"],
                        row["employment_type"],
                        row["source_mode"],
                        row["category"],
                        row["target_match"],
                        captured_at,
                    )
                    for row in rows
                ],
            )
            count = db.execute(
                "SELECT COUNT(*) AS count FROM benchmark_cases WHERE version=%s", (version,)
            ).fetchone()
            return int(count["count"])

    def finish_run(self, run_id: int, *, sources: int, postings: int, failed: int) -> None:
        status = "ok" if failed == 0 else "partial"
        with self.connect() as db:
            db.execute(
                """
                UPDATE sync_runs
                SET finished_at=%s, status=%s, sources=%s, postings=%s, failed=%s
                WHERE id=%s
                """,
                (datetime.now(UTC), status, sources, postings, failed, run_id),
            )
