from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from gaia.restore_snapshot_artifact import restore_latest_snapshot
from gaia.snapshot_validation import validate_snapshot_payload


def usable_payload(*, generated_at: datetime | None = None) -> dict[str, object]:
    generated = generated_at or datetime.now(UTC)
    role = {
        "family_key": "acme/software-intern",
        "title": "Software Intern",
        "company": "Acme",
        "openings": [{"apply_url": "https://example.com/apply"}],
    }
    return {
        "schema_version": 2,
        "generated_at": generated.isoformat(),
        "family_index": [role],
        "family_index_total": 1,
        "family_index_complete": True,
        "responses": {
            "/api/health": {"ok": True},
            "/api/stats": {"active_listings": 1},
            "/api/facets": {"companies": []},
            "/api/families": {"items": [role], "total": 1},
        },
    }


def archive(payload: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("last-known-inventory.json", json.dumps(payload))
    return output.getvalue()


def test_validator_rejects_duplicate_and_incomplete_roles() -> None:
    payload = usable_payload()
    payload["family_index"] = [payload["family_index"][0], payload["family_index"][0]]  # type: ignore[index]
    payload["family_index_total"] = 2
    with pytest.raises(ValueError, match="duplicate"):
        validate_snapshot_payload(payload)

    payload = usable_payload()
    payload["family_index_total"] = 2
    payload["family_index_complete"] = False
    with pytest.raises(ValueError, match="incomplete"):
        validate_snapshot_payload(payload)


def test_validator_rejects_old_and_future_snapshots() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="too old"):
        validate_snapshot_payload(
            usable_payload(generated_at=now - timedelta(hours=169)), now=now
        )
    with pytest.raises(ValueError, match="future"):
        validate_snapshot_payload(
            usable_payload(generated_at=now + timedelta(hours=2)), now=now
        )


def test_restore_skips_bad_artifact_and_writes_newest_usable(tmp_path: Path) -> None:
    listing = {
        "artifacts": [
            {
                "id": 20,
                "name": "gaia-inventory-snapshot-20",
                "expired": False,
                "created_at": "2026-07-31T02:00:00Z",
                "archive_download_url": "https://api.github.test/artifacts/20/zip",
            },
            {
                "id": 10,
                "name": "gaia-inventory-snapshot-10",
                "expired": False,
                "created_at": "2026-07-31T01:00:00Z",
                "archive_download_url": "https://api.github.test/artifacts/10/zip",
            },
        ]
    }
    downloads = {
        "https://api.github.test/artifacts/20/zip": archive({"broken": True}),
        "https://api.github.test/artifacts/10/zip": archive(usable_payload()),
    }
    destination = tmp_path / "last-known-inventory.json"

    with patch("gaia.restore_snapshot_artifact._json_request", return_value=listing), patch(
        "gaia.restore_snapshot_artifact._download", side_effect=lambda url, token: downloads[url]
    ):
        path, artifact_id = restore_latest_snapshot(
            repository="catears124/GAIA", token="token", output=destination
        )

    assert path == destination
    assert artifact_id == 10
    assert json.loads(destination.read_text())["family_index_total"] == 1


def test_restore_never_overwrites_output_without_usable_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "last-known-inventory.json"
    destination.write_text('{"sentinel":true}')
    listing = {
        "artifacts": [
            {
                "id": 1,
                "name": "gaia-inventory-snapshot-1",
                "expired": False,
                "created_at": "2026-07-31T01:00:00Z",
                "archive_download_url": "https://api.github.test/artifacts/1/zip",
            }
        ]
    }
    with patch("gaia.restore_snapshot_artifact._json_request", return_value=listing), patch(
        "gaia.restore_snapshot_artifact._download", return_value=archive({"broken": True})
    ), pytest.raises(RuntimeError, match="no usable snapshot"):
        restore_latest_snapshot(repository="catears124/GAIA", token="token", output=destination)

    assert destination.read_text() == '{"sentinel":true}'
