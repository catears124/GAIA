# GAIA

**Great, Another Internship Aggregator** is a local-first Summer 2027 internship radar built around measurable recall, source provenance, and conservative role-family grouping.

GAIA does **not** claim to index every internship on the public web. It makes a narrower, testable claim: every configured source has an explicit coverage mode, every enumerable board must finish pagination before it is marked complete, and known benchmark listings that GAIA cannot independently recover remain visible as a gap.

## Product contract

- **Employer posted** is supplied by an employer-controlled source. It is never replaced with crawler detection time or a registry-maintained timestamp.
- **First detected** and **last verified** are GAIA timestamps and remain separate.
- Exact copies of one application are deduplicated by ATS/job identity before opening counts are computed.
- Multi-location and multi-requisition variants are grouped into conservative role families without merging different specializations, seasons, years, or employment types.
- Explicit title evidence is authoritative. A registry cannot turn a stated 2026 role, Fall 2027 role, fellowship, or seasonless internship into Summer 2027.
- Target-specific registries are a recall benchmark and discovery feed, not the definition of direct coverage.
- Historical 2025–2026 internship archives seed the employer/ATS universe without entering the 2027 feed.
- Coverage reports application-level benchmark recall, registry-only gaps, direct-only discoveries, broken sources, truncated pagination, and suspicious zero-result enumerators.

## Interface

The default table shows **role families**, not raw source rows. Expanding a family reveals each distinct application, its locations, source variants, and direct apply link.

The UI provides:

- server-side search and pagination;
- exact versus registry-confirmed target filters;
- approximate date rendering for day-granularity Workday values;
- local saved/application tracking;
- application-level coverage diagnostics;
- nonblocking background synchronization.

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

Manual commands:

```bash
gaia sync
gaia serve
python -m gaia.audit
```

The local SQLite database defaults to `data/gaia.db`. Override it with `GAIA_DB`.

## Coverage modes

### Complete board enumeration

- Google Careers internship search
- Greenhouse
- Lever
- Ashby
- Workday CXS

A board is complete only after GAIA reaches the end of pagination. HTTP 200 alone is not sufficient.

### Verification only

Known employer pages exposing Schema.org `JobPosting` data are independently verified, but GAIA does not claim to discover unlinked sibling pages from that source.

### External index

Databricks is currently covered through an explicitly labeled external job index because its employer careers surface does not expose a stable enumerable public feed. This catches the current canary role without being counted as direct enumeration.

### Registry benchmark

Multiple Summer 2027 public registries provide a measurable known-listing floor and reveal employers GAIA has not independently recovered.

### Employer-universe seeds

Valid Simplify/Pitt historical archives from 2025–2026 contribute employer and ATS board identities only. Seed failures are reported in Coverage rather than silently reducing the monitored universe.

## Adding sources

Source configuration lives in `src/gaia/default_sources.yaml` and can be replaced with:

```bash
GAIA_SOURCES=path/to/sources.yaml
```

New adapters implement the `Collector` protocol and must declare:

- coverage mode;
- whether enumeration completed;
- rows scanned and expected rows when known;
- publication-date provenance and precision;
- regression fixtures for pagination and application identity.

## Validation

The repository contains unit tests for:

- Google and Databricks recall canaries;
- Workday full-board enumeration and public URLs;
- strict Summer 2027 classification;
- fellowship and conflicting-year exclusion;
- application deduplication across direct and registry copies;
- employer-date provenance;
- conservative role-family grouping;
- HTML and Markdown registry parsing;
- historical seed health reporting.

GitHub Actions is configured for Python 3.11, 3.13, and 3.14 plus scheduled live recall audits. The private repository currently requires Actions runner access to be enabled before that hosted matrix can execute.
