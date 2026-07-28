from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected block not found in {path}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


def harden_ecosystem_feeds() -> None:
    path = Path("src/gaia/employer_census.py")
    text = path.read_text(encoding="utf-8")
    if "import logging\n" not in text:
        text = text.replace("import hashlib\n", "import hashlib\nimport logging\n", 1)
    logger_anchor = "from .universe import _employer_key\n"
    if "LOGGER = logging.getLogger" not in text:
        text = text.replace(
            logger_anchor,
            logger_anchor + '\nLOGGER = logging.getLogger("gaia.employer-census")\n',
            1,
        )
    old = '''            response = await client.get(url)
            response.raise_for_status()
            source = str(item.get("name") or url)
            observations = _yc_observations(
                response.text,
                url=url,
                source=source,
                sectors=[str(value) for value in item.get("sectors") or []],
            )
            observed += _upsert_observations(
                database,
                source=f"yc:{source}",
                evidence_type="startup-ecosystem",
                internship_signal=float(item.get("internship_signal") or 0.32),
                technical_signal=float(item.get("technical_signal") or 0.86),
                observations=observations,
            )'''
    new = '''            source = str(item.get("name") or url)
            try:
                response = await client.get(url)
                response.raise_for_status()
                observations = _yc_observations(
                    response.text,
                    url=url,
                    source=source,
                    sectors=[str(value) for value in item.get("sectors") or []],
                )
                observed += _upsert_observations(
                    database,
                    source=f"yc:{source}",
                    evidence_type="startup-ecosystem",
                    internship_signal=float(item.get("internship_signal") or 0.32),
                    technical_signal=float(item.get("technical_signal") or 0.86),
                    observations=observations,
                )
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                LOGGER.warning("employer ecosystem feed failed: %s: %r", source, exc)'''
    if old not in text:
        raise SystemExit("expected ecosystem refresh block not found")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def harden_reconciliation_timeout() -> None:
    replace_once(
        ".github/workflows/inventory.yml",
        '''    env:
      GAIA_DATABASE_URL: ${{ secrets.POSTGRES_URL }}
      GAIA_AUTO_MIGRATE: "0"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip
      - name: Install GAIA
        run: python -m pip install --disable-pip-version-check -e .
      - name: Serialize global reconciliation
        run: gaia reconcile''',
        '''    env:
      GAIA_DATABASE_URL: ${{ secrets.POSTGRES_URL }}
      GAIA_AUTO_MIGRATE: "0"
      GAIA_DB_TIMEOUT: "900"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip
      - name: Install GAIA
        run: python -m pip install --disable-pip-version-check -e .
      - name: Serialize global reconciliation
        run: gaia reconcile''',
    )


def add_lane_regression_test() -> None:
    path = Path("tests/test_hosted_inventory.py")
    text = path.read_text(encoding="utf-8")
    if "import pytest\n" not in text:
        text = text.replace(
            "from datetime import UTC, datetime, timedelta\n",
            "from datetime import UTC, datetime, timedelta\n\nimport pytest\n",
            1,
        )
    old_import = "from gaia.live_inventory import LiveDatabase, LiveInventoryStore\n"
    if old_import in text:
        text = text.replace(
            old_import,
            "from gaia.inventory import WorkerSummary\n"
            "from gaia.inventory_runtime import InventoryWorker as RuntimeInventoryWorker\n"
            "from gaia.live_inventory import (\n"
            "    InventoryWorker as LiveInventoryWorker,\n"
            "    LiveDatabase,\n"
            "    LiveInventoryStore,\n"
            ")\n",
            1,
        )
    if "test_live_lane_defers_global_family_rebuild" not in text:
        text += '''

@pytest.mark.asyncio
async def test_live_lane_defers_global_family_rebuild(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "deferred-rebuild.db")
    rebuilds = 0

    def rebuild() -> None:
        nonlocal rebuilds
        rebuilds += 1

    async def fake_runtime_run(
        worker: RuntimeInventoryWorker,
        *,
        once: bool = False,
        budget_seconds: float | None = None,
    ) -> WorkerSummary:
        del once, budget_seconds
        worker.database.rebuild_families()
        return WorkerSummary()

    monkeypatch.setattr(database, "rebuild_families", rebuild)
    monkeypatch.setattr(RuntimeInventoryWorker, "run", fake_runtime_run)
    monkeypatch.setenv("GAIA_DEFER_FAMILY_REBUILD", "1")

    worker = LiveInventoryWorker(database, concurrency=1)
    await worker.run(once=True, budget_seconds=1)

    assert rebuilds == 0
    database.rebuild_families()
    assert rebuilds == 1
'''
    path.write_text(text, encoding="utf-8")


def add_checker_regression_test() -> None:
    path = Path("tests/test_employer_universe.py")
    text = path.read_text(encoding="utf-8")
    if "from gaia.health import production_report\n" not in text:
        text = text.replace(
            "from gaia.models import CollectorResult, Posting\n",
            "from gaia.health import production_report\n"
            "from gaia.models import CollectorResult, Posting\n",
            1,
        )
    if "test_production_checker_can_require_employer_universe" not in text:
        text += '''

def test_production_checker_can_require_employer_universe(tmp_path) -> None:
    database = Database(tmp_path / "missing-universe.db")

    report = production_report(
        database,
        min_sources=1,
        min_active_listings=1,
        require_universe=True,
    )

    assert report["ok"] is False
    assert "employer universe read model is missing" in report["errors"]
'''
    path.write_text(text, encoding="utf-8")


def main() -> None:
    harden_ecosystem_feeds()
    harden_reconciliation_timeout()
    add_lane_regression_test()
    add_checker_regression_test()


if __name__ == "__main__":
    main()
