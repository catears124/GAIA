# GAIA

**Great, Another Internship Aggregator** is a verified index of Summer 2027 computer-science internships.

> Every CS internship. Only when it is real.

GAIA discovers leads from public indexes, historical archives, employer career systems, robots/sitemaps, and supported ATS providers. The public feed is stricter: an application appears as verified only after GAIA recovers it from an employer-controlled source.

## Product contract

The default feed requires all of the following:

- a technical role family: software, ML/AI, data, security, hardware, quant, or product;
- defensible Summer 2027 evidence;
- at least one employer-controlled source, such as a native ATS/API result or structured employer page.

Index-only records remain visible as leads. Registry timestamps never become employer publication dates, and an HTTP 200 response does not count as complete coverage unless GAIA actually traversed the declared search surface.

## Architecture

```text
Employer ATSs, sitemaps, indexes, archives
                    |
                    v
          GAIA crawler / discovery worker
                    |
                    v
          Supabase PostgreSQL database
                    |
                    v
             FastAPI on Vercel
```

PostgreSQL is the only persistence layer. The crawler and public API use the same schema, while the Vercel deployment is read-only. GAIA no longer packages a SQLite snapshot into the serverless function.

The database model uses:

- `TIMESTAMPTZ` for observed, posted, and verification times;
- `TEXT[]` for normalized locations;
- `JSONB` for the nested family-opening read model and source specifications;
- a unique partial index to enforce one active sync run;
- trigram and GIN indexes for company, title, location, and JSON search paths;
- PostgreSQL constraints for lifecycle, scope, counts, and derived-family invariants.

Psycopg prepared statements are disabled deliberately so Supabase's PgBouncer transaction pooler can safely serve Vercel functions.

## Supported source families

Native enumeration currently covers:

- Google Careers
- Greenhouse
- Lever
- Ashby
- Workday CXS internship/co-op search
- SmartRecruiters Posting API
- Recruitee Careers Site API
- Workable public account jobs
- Jobvite, iCIMS, Oracle Cloud, and SuccessFactors
- employer sitemaps and Schema.org `JobPosting` pages

Unsupported or access-limited domains remain explicit coverage work; they are never silently treated as complete.

## Local setup

Python 3.11 or newer:

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
copy .env.example .env
```

Create a Supabase project, then copy the **transaction pooler** connection string from:

```text
Supabase Dashboard -> Project Settings -> Database -> Connection string
```

Put it in `.env`:

```text
GAIA_DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require
```

Do not use the browser-facing Supabase URL or anon key. GAIA connects directly to PostgreSQL, and the database URL must remain server-side.

Initialize the schema:

```bash
gaia migrate
```

Run GAIA:

```bash
# Fast current-source refresh
gaia sync

# Heavy employer/feed/sitemap discovery sweep
gaia discover

# Local web application
gaia serve
```

Open `http://127.0.0.1:8501`.

## Import the existing SQLite database

The migration script preserves crawler history, source health, catalog state, benchmark cases, and postings, then rebuilds role families under the PostgreSQL model.

```bash
python scripts/migrate_sqlite_to_postgres.py --source data/gaia.db
```

The importer truncates the target GAIA tables by default. To upsert without clearing them:

```bash
python scripts/migrate_sqlite_to_postgres.py --source data/gaia.db --keep-target
```

After import, verify:

```bash
gaia serve
```

Then inspect:

```text
/api/health
/api/stats
/api/families
/api/coverage
```

Keep the SQLite file until the imported row counts and public UI are correct. It is no longer read by GAIA after migration.

## Deploy to Vercel

Add this environment variable to the Vercel project for Production, Preview, and Development:

```text
GAIA_DATABASE_URL=<Supabase transaction-pooler connection string>
```

Recommended deployment values are already enforced by `app.py` on Vercel:

```text
GAIA_READ_ONLY=1
GAIA_INITIAL_SYNC=0
GAIA_AUTO_MIGRATE=0
```

Run the schema migration and SQLite import locally before the first production deployment. Vercel should query the database; it should not create schemas or run crawlers.

Deploy:

```bash
vercel --prod
```

The repository workflow also verifies against a real PostgreSQL 16 service before production deployment. Configure these GitHub repository secrets:

```text
VERCEL_TOKEN
VERCEL_ORG_ID
VERCEL_PROJECT_ID
```

## Refresh and discovery planes

### Refresh jobs

The normal sync polls current sources already known to expose relevant internships.

- Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, and Workable boards are enumerated directly.
- Workday is traversed inside public `intern` and `co-op` search surfaces.
- Google Careers uses its public internship search and extracts stable job identities.
- Current custom pages are independently verified.
- Link validation closes applications that are actually gone without treating protected pages as dead.

### Discover companies

The heavier discovery path expands the monitored market.

- Current internship indexes seed application URLs and employers.
- Recently active internship repositories are discovered dynamically through GitHub search.
- Historical internship archives seed ATS boards that may reopen for 2027.
- Known URLs promote into provider-level boards.
- Custom employer domains expand through `robots.txt`, sitemap indexes, and structured `JobPosting` pages.
- Productive sources persist in PostgreSQL and are promoted into the hot refresh set.

## Date contract

GAIA exposes separate time concepts:

- **Employer posted** comes only from an employer-controlled ATS or structured employer page.
- **First detected** is when GAIA first observed the application.
- **Last verified** is when GAIA most recently confirmed it.
- Registry timestamps never become employer publication dates.
- Approximate source values remain approximate.
- When an employer exposes no defensible publication date, GAIA says so instead of inventing precision.

## Useful tuning variables

```text
GAIA_CONCURRENCY=16
GAIA_WORKDAY_PAGE_CONCURRENCY=6
GAIA_DETAIL_CONCURRENCY=8
GAIA_WORKDAY_MAX_PER_TERM=4000
GAIA_DOMAIN_CONCURRENCY=12
GAIA_DOMAIN_MAX_URLS=500
GAIA_LINK_CHECK_LIMIT=500
GAIA_LINK_CHECK_CONCURRENCY=20
GAIA_GITHUB_TOKEN=<optional token for higher discovery rate limits>
GAIA_DEBUG_COLLECTORS=1
```

## Validation

```bash
ruff check .
pytest -q
```

The suite covers provider pagination, classification, canonicalization, application reconciliation, family materialization, link validation, source lifecycle, coverage accounting, and PostgreSQL concurrency behavior.
