# GAIA production hosting

GAIA production uses exactly three services:

- **Vercel** serves the read-only FastAPI product.
- **GitHub Actions** applies migrations and runs horizontally scaled inventory batches.
- **Supabase PostgreSQL** stores the source queue, leases, job inventory, history, and public read model.

No laptop, Render worker, Docker service, or manually running terminal is part of production.

## GitHub secrets

Add these as repository secrets or secrets on the `production` environment:

| Secret | Purpose |
| --- | --- |
| `GAIA_DATABASE_URL` | Supabase transaction-pooler URL (`:6543`) used by scheduled crawler writes |
| `GAIA_MIGRATION_DATABASE_URL` | Supabase direct/non-pooling URL (`:5432`) used for production migrations |
| `VERCEL_TOKEN` | Vercel deployment token |
| `VERCEL_ORG_ID` | Vercel organization/team ID |
| `VERCEL_PROJECT_ID` | Vercel project ID |

The workflows also accept the integration-provided aliases `POSTGRES_URL` and `POSTGRES_URL_NON_POOLING`.

## Vercel environment

Production needs:

```text
GAIA_DATABASE_URL=<Supabase transaction-pooler URL>
GAIA_READ_ONLY=1
GAIA_AUTO_MIGRATE=0
```

The Vercel application never crawls or migrates. It reads the same Supabase database that GitHub Actions updates.

## Workflows

- `.github/workflows/deploy.yml` verifies the project, migrates Supabase through the direct connection, and deploys Vercel after every push to `main`.
- `.github/workflows/inventory.yml` runs every 15 minutes and fans inventory out across seven simultaneous GitHub-hosted runners.
- Three fast-ATS lanes each run 24 async workers.
- Two enterprise-ATS lanes each run 8 async workers.
- Workday has one isolated 2-worker lane; generic fallback pages have one 8-worker lane.
- A separate discovery job runs only after the validated employer-board lanes complete and the market queue is caught up.
- Scheduled lanes run for up to 11 minutes.
- A successful `main` production deployment launches 50-minute catch-up lanes after migration and deployment complete.
- A manual **Production inventory** run also defaults to 50 minutes per lane.

The PostgreSQL queue uses leases and `FOR UPDATE SKIP LOCKED`, so every async worker across every Actions runner claims a distinct source. Interrupted jobs are safely reclaimed, and manual, deployment-triggered, and scheduled workflow runs may overlap without crawling the same board twice.
