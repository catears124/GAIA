from __future__ import annotations

from pathlib import Path

from gaia import cli


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        del query, params
        return self


class _Database:
    def __init__(self) -> None:
        self.families_rebuilt = 0

    def connect(self):
        return _Lock()

    def rebuild_families(self) -> None:
        self.families_rebuilt += 1


def test_critical_reconcile_skips_auxiliary_employer_census(monkeypatch) -> None:
    database = _Database()
    monkeypatch.setenv("GAIA_RECONCILE_EMPLOYER_UNIVERSE", "0")
    monkeypatch.setattr(cli, "Database", lambda migrate=False: database)
    monkeypatch.setattr(
        cli,
        "rebuild_employer_universe",
        lambda _database: (_ for _ in ()).throw(AssertionError("auxiliary census ran")),
    )
    monkeypatch.setattr(
        cli,
        "merge_observations_into_universe",
        lambda _database: (_ for _ in ()).throw(AssertionError("ecosystem merge ran")),
    )

    assert cli.run_reconcile() == {"families_rebuilt": 1}
    assert database.families_rebuilt == 1


def test_production_reconcile_prioritizes_public_feed() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "reconcile.yml"
    ).read_text(encoding="utf-8")

    assert 'GAIA_RECONCILE_EMPLOYER_UNIVERSE: "0"' in workflow
    assert "Public role-feed reconciliation is running" in workflow
    assert "Public role feed is current" in workflow
