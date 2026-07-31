from __future__ import annotations

import json

from gaia.production_smoke import Probe, _valid_database_outage, evaluate


def _probe(status: int = 200, body: object | str = "") -> Probe:
    if not isinstance(body, str):
        body = json.dumps(body)
    return Probe(status=status, body=body)


def _base_probes() -> dict[str, Probe]:
    return {
        "index": _probe(body="emergency-outage.js api-resilience.js"),
        "emergency": _probe(body="MAX_EMERGENCY_AGE_MS"),
        "controller": _probe(body="liveHealthProbe XMLHttpRequest"),
        "snapshot": _probe(status=503),
        "health": _probe(),
        "stats": _probe(body={}),
        "families": _probe(body={"items": [], "total": 0}),
    }


def _outage_payload() -> dict[str, object]:
    return {
        "ok": False,
        "stale": True,
        "reason": "database_unavailable",
        "inventory": {"healthy": False, "total": 0},
        "progress": {"stage": "database-recovery"},
    }


def test_valid_database_outage_requires_truthful_fields() -> None:
    payload = _outage_payload()
    assert _valid_database_outage(payload)

    for key, value in (
        ("ok", True),
        ("stale", False),
        ("reason", "unknown"),
    ):
        broken = dict(payload)
        broken[key] = value
        assert not _valid_database_outage(broken)


def test_503_database_outage_is_not_treated_as_generic_server_failure() -> None:
    probes = _base_probes()
    probes["health"] = _probe(503, _outage_payload())

    result = evaluate(probes)

    assert result.state == "failure"
    assert result.description == "Database recovery active and first-visit inventory snapshot is unusable"


def test_invalid_503_contract_fails_closed() -> None:
    probes = _base_probes()
    probes["health"] = _probe(503, {"ok": False})

    result = evaluate(probes)

    assert result.state == "failure"
    assert result.description == "Health API returned an invalid database-outage contract"


def test_timeout_is_distinguished_from_http_server_failure() -> None:
    probes = _base_probes()
    probes["health"] = _probe(0)

    result = evaluate(probes)

    assert result.state == "failure"
    assert result.description == "Health API timed out or could not be reached"
