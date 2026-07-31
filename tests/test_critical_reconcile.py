from __future__ import annotations

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

    result = cli.run_reconcile()

    assert result["families_rebuilt"] == 1
    assert result["employers"] == 7
    assert result["ecosystem_inserted"] == 1
    assert database.families_rebuilt == 1
    assert calls == ["postings", "ecosystem"]
