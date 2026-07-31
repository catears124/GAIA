from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .snapshot_validation import validate_snapshot_payload

ARTIFACT_PREFIX = "gaia-inventory-snapshot-"
SNAPSHOT_NAME = "last-known-inventory.json"


def _json_request(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gaia-snapshot-recovery",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub API returned a non-object response")
    return payload


def _download(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gaia-snapshot-recovery",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def restore_latest_snapshot(
    *, repository: str, token: str, output: Path, max_artifacts: int = 50
) -> tuple[Path, int]:
    if not repository or "/" not in repository:
        raise ValueError("repository must be owner/name")
    if not token:
        raise ValueError("GitHub token is required")

    endpoint = (
        f"https://api.github.com/repos/{repository}/actions/artifacts"
        f"?per_page={min(100, max(1, max_artifacts))}"
    )
    payload = _json_request(endpoint, token)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("GitHub artifacts response is missing artifacts")

    failures: list[str] = []
    candidates = sorted(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
            and str(artifact.get("name") or "").startswith(ARTIFACT_PREFIX)
            and not artifact.get("expired")
        ),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )

    for artifact in candidates[:max_artifacts]:
        artifact_id = int(artifact.get("id") or 0)
        archive_url = str(artifact.get("archive_download_url") or "")
        if not artifact_id or not archive_url:
            continue
        try:
            archive = _download(archive_url, token)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = [name for name in bundle.namelist() if name.endswith(SNAPSHOT_NAME)]
                if not names:
                    raise ValueError("artifact does not contain a snapshot")
                raw = bundle.read(names[0])
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("snapshot root must be an object")
            validate_snapshot_payload(decoded, max_age_hours=720, size_bytes=len(raw))
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_bytes(raw)
            temporary.replace(output)
            return output, artifact_id
        except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, urllib.error.URLError) as exc:
            failures.append(f"{artifact_id}: {exc}")

    detail = "; ".join(failures[:5]) or "no matching non-expired artifacts"
    raise RuntimeError(f"no usable snapshot artifact found ({detail})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore GAIA's newest usable inventory artifact")
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.getenv("GH_TOKEN", ""))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("frontend") / SNAPSHOT_NAME,
    )
    parser.add_argument("--max-artifacts", type=int, default=50)
    args = parser.parse_args()
    path, artifact_id = restore_latest_snapshot(
        repository=args.repository,
        token=args.token,
        output=args.output,
        max_artifacts=max(1, args.max_artifacts),
    )
    print(f"restored {path} from artifact {artifact_id}")


if __name__ == "__main__":
    main()
