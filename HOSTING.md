# GAIA production hosting

GAIA production uses exactly three services:

- **Vercel** serves the read-only FastAPI product.
- **GitHub Actions** applies migrations and runs bounded inventory batches.
- **Supabase PostgreSQL** stores the source queue, job inventory, history, and public read model.

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
- `.github/workflows/inventory.yml` runs every 15 minutes and drains due employer boards for up to 12 minutes.
- A push to `main` starts a 30-minute catch-up batch.
- A manual **Production inventory** workflow run defaults to a 50-minute catch-up batch.

The PostgreSQL queue uses leases and `FOR UPDATE SKIP LOCKED`, so interrupted jobs are safely reclaimed and overlapping workflow attempts do not crawl the same source concurrently.
