# GAIA

**Great, Another Internship Aggregator** — a local-first Summer 2027 internship radar built around measurable recall, source provenance, and role-family grouping.

GAIA does not claim to index every internship on the internet. It does guarantee that every configured source reports one of: completely enumerated, externally indexed, verification-only, broken, or unresolved. Missing sources are visible failures rather than silent omissions.

## Product contract

- Employer publication time is never replaced with crawler detection time.
- Exact applications are deduplicated by source identity.
- Multi-location requisitions are grouped into conservative role families.
- Public internship registries are a recall benchmark and discovery feed, not the definition of the employer universe.
- Google and Databricks are release canaries: CI fails if their current Summer 2027 roles cannot be recovered from fixtures.
- Coverage reports enumeration completeness, held-out recall, unresolved domains, and zero-result anomalies separately.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
pytest -q
python -m gaia.cli sync
python -m gaia.cli serve
```

Open `http://127.0.0.1:8501`.

## Sources in the first shippable release

Direct or employer-controlled:

- Google Careers
- Greenhouse
- Lever
- Ashby
- Workday CXS
- Schema.org `JobPosting` pages and sitemaps

Independent detection/backstop:

- public target-specific registries
- explicit company-job index pages for canary employers when the employer site is non-enumerable

Backstop records are labeled and never counted as direct enumeration.

## Repository status

The initial release is intentionally compact. New adapters implement the `Collector` protocol and must ship with pagination, publication-date provenance, and recall fixtures.
