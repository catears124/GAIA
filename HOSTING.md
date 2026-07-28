# GAIA production hosting

GAIA production uses exactly three services:

- **Vercel** serves the read-only FastAPI product through its GitHub integration.
- **GitHub Actions** applies migrations and runs horizontally scaled inventory batches.
- **Supabase PostgreSQL** stores the source queue, leases, job inventory, history, and public read model.

No laptop, Render worker, Docker service, Vercel CLI token, or manually running terminal is part of production.

## GitHub Actions secrets

Add exactly these two values under **Repository settings → Secrets and variables → Actions**, or on the `production` environment:

| Secret | Purpose |
| --- | --- |
| `POSTGRES_URL` | Supabase transaction-pooler URL (`:6543`) used by crawler writes |
| `POSTGRES_URL_NON_POOLING` | Supabase direct/non-pooling URL (`:5432`) used by migrations |

GAIA does **not** need Supabase's anon key, publishable key, service-role key, JWT secret, raw database password, Vercel token, organization ID, or project ID in GitHub Actions.

## Vercel environment

Vercel needs the database URL and read-only flags already supplied by the Supabase integration:

```text
POSTGRES_URL=<Supabase transaction-pooler URL>
GAIA_READ_ONLY=1
GAIA_INITIAL_SYNC=0
GAIA_AUTO_MIGRATE=0
```

The Vercel application never crawls or migrates. Vercel deploys automatically from `main` through its GitHub integration and reads the same Supabase database that Actions updates.

## Workflows

- `.github/workflows/deploy.yml` only verifies tests, lint, and Vercel-entrypoint compatibility. Vercel's Git integration owns deployment.
- `.github/workflows/inventory.yml` runs on pushes to `main`, every 15 minutes, and by manual dispatch.
- Its prepare job migrates Supabase, then reconciles the validated source catalog into the crawl queue once.
- Three fast-ATS lanes each run 24 async workers.
- Two enterprise-ATS lanes each run 8 async workers.
- Workday has one isolated 2-worker lane; generic fallback pages have one 8-worker lane.
- A separate discovery job runs only after the validated employer-board lanes complete and the market queue is caught up.
- Scheduled lanes run for up to 11 minutes. Push and manual catch-up runs default to 50 minutes per lane.

The PostgreSQL queue uses leases and `FOR UPDATE SKIP LOCKED`, so every async worker across every Actions runner claims a distinct source. Interrupted and overlapping workflow runs are safely reclaimed without crawling the same board twice.
