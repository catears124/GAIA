# GAIA

**Great, Another Internship Aggregator** is a continuously maintained inventory of technical internship applications recovered from employer job systems.

GAIA does not treat a periodically generated list as the market. It keeps a census of employer career systems, crawls each source on its own schedule, records complete source snapshots, and derives the internship feed from the resulting job inventory.

## Product contract

The public feed distinguishes two things:

- **Verified applications** came from an employer-controlled ATS, jobs API, sitemap, or structured employer page.
- **Leads** came from public internship indexes and are retained as discovery evidence until GAIA independently recovers the employer application.

A source is never considered complete merely because it returned HTTP 200. Removals are recorded only after a complete source snapshot. Partial, blocked, truncated, and failed crawls preserve the previous active inventory.

Absolute coverage of every private or access-controlled job is impossible. GAIA therefore reports the state of its monitored source universe explicitly: sources completed, overdue, blocked, degraded, and never successfully enumerated.

## Architecture

```text
Employer ATSs, APIs, sitemaps, career domains, public gap detectors
                              |
                              v
                 continuous inventory workers
                 - PostgreSQL source queue
                 - per-source leases
                 - complete snapshots
                 - failure backoff
                 - safe new/removal reconciliation
                              |
                              v
                    Supabase PostgreSQL
                              |
                              v
                     FastAPI on Vercel
```

There are no operator-facing `sync` or `discover` modes. Discovery is a scheduled maintenance task inside the same worker, and newly discovered sources enter the persistent crawl queue automatically.

## Inventory model

Each cataloged source receives its own:

- crawl interval and priority;
- `next_run_at` schedule;
- lease owner and expiration;
- last attempt and last complete snapshot;
- observed and expected row counts;
- failure streak and retry backoff;
- source snapshot history.

The worker claims due sources with PostgreSQL row locks using `FOR UPDATE SKIP LOCKED`, so multiple workers can run without crawling the same source simultaneously.

Current defaults:

| Source family | Typical cadence |
| --- | ---: |
| Greenhouse, Lever, Ashby | 15 minutes |
| SmartRecruiters, Recruitee, Workable | 20 minutes |
| Workday, Google | 20–30 minutes |
| Jobvite, iCIMS, Oracle, SuccessFactors | 30 minutes |
| Employer verification pages | 1 hour |
| Employer domains and sitemaps | 6 hours |
| Historical/dormant employer watches | 24 hours |
| Public market discovery | 15 minutes |
| Historical employer-universe expansion | 24 hours |

Intervals are scheduling targets rather than promises. The API reports overdue and degraded sources instead of presenting an arbitrary partial batch as a fresh market snapshot.

## Snapshot and removal semantics

For a complete source result:

```text
previous active IDs - current IDs = removed jobs
current IDs - every previously seen ID = newly discovered jobs
```

For a partial, blocked, rate-limited, or failed result:

```text
no jobs are removed
previous active state is preserved
source is retried with backoff
```

The `postings` table records `first_seen_at`, `last_seen_at`, and `removed_at`. The API exposes first-seen-today, removed-today, and net-today movement independently of employer publication dates.

## Collection strategy

Native enumeration currently covers:

- Greenhouse
- Lever
- Ashby
- Workday CXS
- Google Careers
- SmartRecruiters
- Recruitee
- Workable
- Jobvite
- iCIMS
- Oracle Cloud
- SuccessFactors
- employer sitemaps and Schema.org `JobPosting` pages

For board APIs that expose the full employer inventory, GAIA stores every returned job and classifies internships afterward. Workday runs in full-inventory mode by default rather than sampling eight internship searches.

Public internship repositories are gap detectors and employer-discovery surfaces. They are not treated as proof that the independently enumerated market is complete.

## Classification

Collection and inventory identity come before product filtering. Direct employer descriptions, employment types, and program-style titles are considered alongside explicit `intern` and `co-op` wording. This includes titles such as:

- Summer Technology Analyst
- Engineering Summer Associate
- Student Researcher
- Product Management Intern
- Quantitative Trading Intern

The public feed still requires technical-category and 2027 evidence, but uncertain records remain in the underlying inventory instead of disappearing at collection time.

## Database setup

Python 3.11 or newer:

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
copy .env.example .env
```

Configure a server-side PostgreSQL connection. GAIA accepts `GAIA_DATABASE_URL`, `POSTGRES_URL`, `DATABASE_URL`, or `SUPABASE_DB_URL`.

```text
GAIA_DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require
```

Apply schema upgrades:

```bash
gaia migrate
```

## Run locally

Web application:

```bash
gaia serve
```

Continuous worker:

```bash
gaia worker
```

A bounded worker invocation for CI or a cron fallback:

```bash
gaia worker --once --budget-seconds 720
```

`--once` is an infrastructure primitive, not a data-refresh workflow. It drains due queue entries and exits; the queue itself controls what is crawled.

## Production

### Web

Vercel remains a read-only FastAPI deployment. Configure the PostgreSQL URL and deploy normally.

```text
GAIA_READ_ONLY=1
GAIA_AUTO_MIGRATE=0
```

### Always-on worker

`render.yaml`, `Dockerfile`, and `Procfile` define an independent worker process. Configure `GAIA_DATABASE_URL`; optionally configure `GAIA_GITHUB_TOKEN` for higher public discovery rate limits.

The process command is:

```bash
gaia worker
```

### Scheduled fallback

`.github/workflows/inventory.yml` drains due work every 15 minutes. Configure either `GAIA_DATABASE_URL` or `POSTGRES_URL` as a GitHub Actions secret. An always-on worker is preferred; the workflow is a fallback and recovery path.

## API health contract

`/api/health` now reports source-level inventory state:

```json
{
  "inventory": {
    "total": 2400,
    "fresh": 2368,
    "fresh_percent": 98.7,
    "never_completed": 4,
    "overdue": 15,
    "degraded": 13,
    "coverage_watermark": "..."
  }
}
```

The compatibility `last_run` timestamp is populated only after every current source has completed at least once. It represents the oldest completed current-source watermark, not the finish time of an arbitrary partial global run.

`/api/stats` includes:

- `active_listings`
- `new_today`
- `removed_today`
- `net_today`
- verified family and application counts

## Validation

```bash
ruff check .
pytest -q
```

The test suite covers provider pagination, classification, canonicalization, reconciliation, family materialization, link validation, source lifecycle, coverage accounting, and PostgreSQL concurrency behavior.
