# GAIA

**Great, Another Internship Aggregator** is a local-first index for verified Summer 2027 computer-science internships.

## v3 product contract

> Every CS internship. Only when it is real.

GAIA discovers leads from public indexes, historical archives, employer career systems, robots/sitemaps and supported ATS providers. The default product feed is stricter: a listing appears as a normal internship only after GAIA recovers it from an employer-controlled source.

Public indexes are useful, but they are leads. They do not get to dominate the homepage merely because a README said “Summer 2027.”

## What appears in the default feed

The default feed includes only role families that satisfy all of the following:

- technical category: software, ML/AI, data, security, hardware, quant or product;
- confirmed 2027 evidence;
- at least one employer-controlled source variant, either a native ATS/API result or a verified structured employer page.

Index-only records move to the **Lead queue**. They remain searchable and visible, but they are no longer treated as ready-to-apply product rows.

## Two-plane crawler design

### Refresh jobs

The normal startup and **Refresh jobs** action poll only current sources already known to expose relevant internships.

- Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee and Workable boards are enumerated directly.
- Workday is traversed inside its public `intern` and `co-op` search surfaces; GAIA does not scan the whole employer board during interactive refresh.
- Google Careers uses its public internship search and extracts job identities from links and embedded page data.
- Current custom pages are independently verified.
- Progress is visible in the UI and source logs are concise by default.

### Discover companies

The explicit **Discover companies** action expands the monitored market.

- Current 2027 internship indexes seed application URLs and employers.
- Recently active internship repositories are discovered dynamically through GitHub repository search.
- Historical 2025–2026 internship archives seed ATS boards that may reopen for 2027.
- Known URLs promote automatically into provider-level boards.
- Custom employer domains expand through `robots.txt`, sitemap indexes and structured `JobPosting` pages.
- Discovered source specifications persist in SQLite, so future refreshes no longer depend on the original index that revealed them.

The heavy discovery sweep is intentionally separate from the fast interactive refresh.

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
- employer sitemaps and Schema.org `JobPosting` pages

Unsupported or access-limited domains remain explicit coverage work; they are never silently treated as complete.

## Date contract

GAIA exposes separate time concepts:

- **Employer posted** comes only from an employer-controlled ATS or structured employer page.
- **First detected** is when GAIA first observed the application.
- **Last verified** is when GAIA most recently confirmed it.
- Registry timestamps never become employer publication dates.
- Workday relative values such as `Posted 1 Day Ago` remain approximate and render as `~1 day ago`.
- When an employer exposes no defensible publication date, GAIA says so and still shows verification time. It does not invent precision.

## Canonicalization

v3 adds a product-quality layer:

- company aliases and flags are normalized for display and grouping;
- `D. E. Shaw`, `DE Shaw`, and related variants merge;
- `BAE Systems 🇺🇸` displays as `BAE Systems`;
- glued registry location strings such as `4 locations**Nashua, NHHudson, NH...` are repaired before display;
- persisted source identities are deduplicated without destroying case-sensitive provider IDs.

## Coverage contract

Coverage is an engineering diagnostic, not the product headline. GAIA reports:

- verified employer applications;
- unresolved lead applications;
- benchmark applications from independent 2027 indexes;
- benchmark applications still lead-only;
- employer applications found before or outside the benchmark;
- complete source traversals;
- genuine current crawler failures;
- access-limited sources;
- stale pages and dormant historical watches.

A source is not healthy merely because it returned HTTP 200. Pagination or the declared search surface must finish. Historical failures do not inflate the current failure count, and query-scoped Workday sources returning zero internships are not mislabeled as broken global boards.

## Run locally

Python 3.11 or newer:

```bash
python -m venv .venv

# Windows cmd
.venv\Scripts\activate.bat

# Windows PowerShell
.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
pytest -q
python scripts/run_local.py
```

Open `http://127.0.0.1:8501`.

Commands:

```bash
# Fast current-source refresh
gaia sync

# Heavy employer/feed/sitemap discovery sweep
gaia discover

# Web application
gaia serve
```

The SQLite database defaults to `data/gaia.db`. Override it with `GAIA_DB`.

Useful tuning variables:

```text
GAIA_CONCURRENCY=16
GAIA_WORKDAY_PAGE_CONCURRENCY=6
GAIA_DETAIL_CONCURRENCY=8
GAIA_WORKDAY_MAX_PER_TERM=4000
GAIA_DOMAIN_CONCURRENCY=12
GAIA_DOMAIN_MAX_URLS=500
GAIA_GITHUB_TOKEN=<optional token for higher discovery rate limits>
GAIA_DEBUG_COLLECTORS=1
```

## Adding a provider

A collector must declare:

- its coverage mode and current/historical scope;
- whether its declared surface was fully traversed;
- rows scanned and expected rows where available;
- publication-date provenance and precision;
- a stable application identity;
- regression fixtures for pagination, empty results and malformed responses.

Provider discovery should be URL-pattern based. Employer names belong in discovered data and the persisted source catalog, not in Python branches.

## Validation

The test suite covers:

- Workday query-scoped pagination and the prohibition on empty search text;
- concurrent page traversal and duplicate-term reconciliation;
- Google extraction from anchors and embedded page data;
- SmartRecruiters, Recruitee and Workable promotion and enumeration;
- dynamic GitHub market-feed discovery;
- sitemap expansion and structured publication dates;
- persisted source-catalog behavior;
- strict Summer 2027 classification;
- application deduplication and conservative role-family grouping;
- source status, latest-run scoping and benchmark recall accounting;
- v3 company/source/location canonicalization.
