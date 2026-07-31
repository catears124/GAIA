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


def test_reconcile_rebuilds_all_public_read_models(monkeypatch) -> None:
    database = _Database()
    calls: list[str] = []
    monkeypatch.setattr(cli, "Database", lambda migrate=False: database)
    monkeypatch.setattr(
        cli,
        "rebuild_employer_universe",
        lambda _database: calls.append("postings") or {"employers": 7, "evidence": 9, "frontier": 4},
    )
    monkeypatch.setattr(
        cli,
        "merge_observations_into_universe",
        lambda _database: calls.append("ecosystem") or {"observations": 3, "merged": 2, "inserted": 1},
    )

    assert cli.run_reconcile() == {
        "families_rebuilt": 1,
        "employers": 7,
        "evidence": 9,
        "frontier": 4,
        "ecosystem_observations": 3,
        "ecosystem_merged": 2,
        "ecosystem_inserted": 1,
    }
    assert database.families_rebuilt == 1
    assert calls == ["postings", "ecosystem"]


def test_production_reconcile_includes_employer_universe() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "reconcile.yml"
    ).read_text(encoding="utf-8")

    assert "GAIA_RECONCILE_EMPLOYER_UNIVERSE" not in workflow
    assert "Rebuild critical public role feed and employer universe" in workflow
    assert "Public role feed and employer universe are current" in workflow
