from __future__ import annotations

import asyncio
import os

import httpx

from .collectors import DatabricksIndexCollector, GoogleCareersCollector


async def audit() -> None:
    headers = {
        "User-Agent": os.getenv(
            "GAIA_USER_AGENT", "GAIA/1.0 recall-audit (+github.com/catears124/GAIA)"
        )
    }
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        google = await GoogleCareersCollector(pages=3).collect(client)
        databricks = await DatabricksIndexCollector().collect(client)

    google_titles = [item.title for item in google.postings if item.target_match == "exact"]
    databricks_titles = [item.title for item in databricks.postings if item.target_match == "exact"]
    if not any("Software Engineering Intern" in title for title in google_titles):
        raise SystemExit(f"Google recall canary failed. Recovered: {google_titles!r}")
    if not databricks_titles:
        raise SystemExit("Databricks recall canary failed: no Summer 2027 internship recovered")
    print({"google": google_titles, "databricks": databricks_titles})


def main() -> None:
    asyncio.run(audit())


if __name__ == "__main__":
    main()
