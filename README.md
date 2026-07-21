# GAIA

**Great, Another Internship Aggregator** is a local-first index for Summer 2027 computer-science internships.

## Headline

> Every CS internship. One live index.

That is the product goal, not a claim that the public web can be proven globally complete. GAIA makes the strongest claim it can actually audit: it continuously generates an employer/source universe, fully traverses supported internship surfaces, measures recovery against independent public indexes, and leaves every unresolved application or source visible.

## Why GAIA 2 exists

GAIA 1 treated company discovery and job refresh as one giant crawl. A large Workday employer could make a normal refresh walk tens of thousands of unrelated jobs before it found a few internships. It was slow, noisy, and circular because the company universe depended too heavily on a few manually configured repositories.

GAIA 2 separates the system into two planes:

### Refresh jobs

The normal startup and **Refresh jobs** action poll only current sources already known to expose Summer 2027 opportunities.

- Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee and Workable boards are enumerated directly.
- Workday is traversed completely inside its public `intern` and `co-op` search surfaces. GAIA never scans the employer's entire general-purpose board during an interactive refresh.
- Google Careers uses its public internship search and extracts job identities from both links and embedded page data.
- Current custom pages are independently verified.
- Progress is visible in the UI and source logs are concise by default.

### Discover companies

The explicit **Discover companies** action expands the monitored market.

- Current 2027 internship indexes seed application URLs and employers.
- Recently active internship repositories are discovered dynamically through GitHub repository search; no company names are embedded in this process.
- Historical 2025–2026 internship archives seed ATS boards that may reopen for 2027.
- Known URLs are promoted automatically into provider-level boards.
- Custom employer domains are expanded through `robots.txt`, sitemap indexes and structured `JobPosting` pages.
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
- When an employer exposes no defensible publication date, GAIA says so and still shows detection time. It does not invent precision.

## Job identity and grouping

- Exact copies from an employer board and one or more indexes collapse by ATS/job identity before counts are computed.
- Separate location requisitions can group into one conservative role family.
- Different specializations, seasons, years and employment types remain separate.
- The default feed includes technical categories only: software, ML/AI, data, security, hardware, quant and product.
- Nontechnical internships remain queryable through the API by using `track=all`.

## Coverage contract

GAIA reports:

- active applications known across all sources;
- benchmark applications from independent 2027 indexes;
- applications independently recovered from employer sources;
- benchmark applications still index-only;
- employer applications found before or outside the benchmark;
- complete source traversals;
- genuine current crawler failures;
- access-limited sources;
- stale pages and dormant historical watches.

A source is not healthy merely because it returned HTTP 200. Pagination or the declared search surface must finish. Historical failures do not inflate the current failure count, and a query-scoped Workday source returning zero internships is not mislabeled as a broken global board.

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
- source status, latest-run scoping and benchmark recall accounting.
