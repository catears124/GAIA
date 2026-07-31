from __future__ import annotations

from collections.abc import Iterator

import pytest

from gaia import static_snapshot_http as snapshot


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[dict[str, object]], **_kwargs: object) -> None:
        self.responses: Iterator[dict[str, object]] = iter(responses)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _path: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse(next(self.responses))


def family(key: str) -> dict[str, object]:
    return {
        "family_key": key,
        "company": "Example",
        "title": "Software Intern",
        "locations": ["Remote"],
        "openings": [],
    }


def prefix() -> list[dict[str, object]]:
    return [
        {"ok": True, "stale": False, "inventory": {"healthy": True, "total": 2}},
        {},
        {},
        {},
        {},
    ]


def test_family_total_rejects_boolean_and_non_positive_values() -> None:
    with pytest.raises(snapshot.SnapshotExportError, match="boolean total"):
        snapshot._family_total({"total": True}, page=1)
    with pytest.raises(snapshot.SnapshotExportError, match="non-positive total"):
        snapshot._family_total({"total": 0}, page=1)


def test_build_snapshot_rejects_total_drift(monkeypatch) -> None:
    responses = prefix() + [
        {"total": 2, "items": [family("a")]},
        {"total": 3, "items": [family("b")]},
    ]
    monkeypatch.setattr(snapshot.httpx, "Client", lambda **kwargs: FakeClient(responses, **kwargs))

    with pytest.raises(snapshot.SnapshotExportError, match="total changed during export"):
        snapshot.build_snapshot("https://example.invalid")


def test_build_snapshot_rejects_duplicate_family_keys(monkeypatch) -> None:
    responses = prefix() + [
        {"total": 2, "items": [family("a\")]},
        {"total": 2, "items": [family("a")]},
    ]
    monkeypatch.setattr(snapshot.httpx, "Client", lambda **kwargs: FakeClient(responses, **kwargs))

    with pytest.raises(snapshot.SnapshotExportError, match="repeated family key"):
        snapshot.build_snapshot("https://example.invalid")


def test_build_snapshot_rejects_early_empty_page(monkeypatch) -> None:
    responses = prefix() + [
        {"total": 2, "items": [family("a")]},
        {"total": 2, "items": []},
    ]
    monkeypatch.setattr(snapshot.httpx, "Client", lambda **kwargs: FakeClient(responses, **kwargs))

    with pytest.raises(snapshot.SnapshotExportError, match="ended early"):
        snapshot.build_snapshot("https://example.invalid")


def test_build_snapshot_rejects_empty_health_inventory(monkeypatch) -> None:
    responses = [
        {"ok": True, "stale": False, "inventory": {"healthy": True, "total": 0}},
    ]
    monkeypatch.setattr(snapshot.httpx, "Client", lambda **kwargs: FakeClient(responses, **kwargs))

    with pytest.raises(snapshot.SnapshotExportError, match="empty inventory"):
        snapshot.build_snapshot("https://example.invalid")
