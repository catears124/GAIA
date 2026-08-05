import json
from datetime import UTC, datetime, timedelta

from gaia.production_smoke import Probe, evaluate, snapshot_is_usable


def probes() -> dict[str, Probe]:
    return {
        "index": Probe(
            200,
            (
                '<script src="remote-snapshot.js?v=1.0.1"></script>'
                '<script src="api-resilience.js?v=2.0.0"></script>'
                '<script src="emergency-outage.js?v=2.0.0"></script>'
                '<script src="outage-controller.js?v=1.2.1"></script>'
            ),
        ),
        "remote": Probe(
            200,
            (
                "raw.githubusercontent.com/catears124/GAIA/snapshot-data "
                'cache: "no-store" mode: "cors"'
            ),
        ),
        "resilience": Probe(
            200,
            (
                "window.fetch = async function resilientFetch "
                "staticSnapshotResponse cachedResponse gaia:stale-data"
            ),
        ),
        "emergency": Probe(200, "MAX_EMERGENCY_AGE_MS = 0 retireLegacyState"),
        "controller": Probe(
            200, "function liveHealthProbe(){ return new XMLHttpRequest(); }"
        ),
        "snapshot": Probe(
            200,
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "max_stale_seconds": 86400,
                    "family_index": [{"family_key": "a"}],
                    "family_index_total": 1,
                    "family_index_complete": True,
                }
            ),
        ),
        "health": Probe(
            200,
            json.dumps(
                {
                    "ok": True,
                    "stale": False,
                    "inventory": {"healthy": True, "fresh": 6, "total": 6},
                }
            ),
        ),
        "stats": Probe(200, "{}"),
        "families": Probe(200, json.dumps({"items": [], "total": 0})),
    }


def test_healthy_contract_succeeds() -> None:
    result = evaluate(probes())
    assert result.state == "success"
    assert "resilience chain" in result.description


def test_live_degraded_inventory_is_pending_not_healthy() -> None:
    evidence = probes()
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": False,
                "stale": False,
                "inventory": {"healthy": False, "fresh": 5, "total": 6},
            }
        ),
    )

    result = evaluate(evidence)

    assert result.state == "pending"
    assert result.description == "Production reachable; inventory catch-up 5/6 fresh"


def test_truthful_database_outage_requires_a_complete_nonempty_snapshot() -> None:
    evidence = probes()
    evidence["health"] = Probe(
        503,
        json.dumps(
            {
                "ok": False,
                "stale": True,
                "reason": "database_unavailable",
                "inventory": {"healthy": False, "total": 0},
                "progress": {"stage": "database-recovery"},
            }
        ),
    )
    assert evaluate(evidence).state == "pending"
    payload = json.loads(evidence["snapshot"].body)
    payload["family_index_complete"] = False
    evidence["snapshot"] = Probe(200, json.dumps(payload))
    result = evaluate(evidence)
    assert result.state == "failure"
    assert "first-visit inventory snapshot is unusable" in result.description


def test_generic_server_failure_is_not_mislabeled_database_recovery() -> None:
    evidence = probes()
    evidence["health"] = Probe(500, "internal error")

    result = evaluate(evidence)

    assert result.state == "failure"
    assert "unclassified server failure" in result.description


def test_snapshot_rejects_duplicate_blank_or_mismatched_family_indexes() -> None:
    payload = json.loads(probes()["snapshot"].body)
    payload["family_index"] = [{"family_key": "a"}, {"family_key": "a"}]
    payload["family_index_total"] = 2
    assert snapshot_is_usable(Probe(200, json.dumps(payload))) is False
    payload["family_index"] = [{"family_key": ""}]
    payload["family_index_total"] = 1
    assert snapshot_is_usable(Probe(200, json.dumps(payload))) is False
    payload["family_index"] = [{"family_key": "a"}]
    payload["family_index_total"] = 2
    assert snapshot_is_usable(Probe(200, json.dumps(payload))) is False


def test_snapshot_rejects_stale_future_and_unbounded_expiry() -> None:
    payload = json.loads(probes()["snapshot"].body)
    now = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
    payload["generated_at"] = "2026-07-31T14:00:00Z"
    payload["max_stale_seconds"] = 3600
    assert snapshot_is_usable(Probe(200, json.dumps(payload)), now=now) is False
    payload["generated_at"] = "2026-07-31T17:00:00Z"
    assert snapshot_is_usable(Probe(200, json.dumps(payload)), now=now) is False
    payload["generated_at"] = "2026-07-31T16:00:00Z"
    payload["max_stale_seconds"] = 999999999
    assert snapshot_is_usable(Probe(200, json.dumps(payload)), now=now) is False


def test_public_snapshot_must_be_younger_than_45_minutes() -> None:
    payload = json.loads(probes()["snapshot"].body)
    now = datetime(2026, 8, 5, 21, 0, tzinfo=UTC)
    payload["generated_at"] = (now - timedelta(minutes=44)).isoformat()
    assert snapshot_is_usable(Probe(200, json.dumps(payload)), now=now) is True
    payload["generated_at"] = (now - timedelta(minutes=46)).isoformat()
    assert snapshot_is_usable(Probe(200, json.dumps(payload)), now=now) is False


def test_live_health_cannot_be_stale_dishonest_or_contradictory() -> None:
    evidence = probes()
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": True,
                "stale": True,
                "inventory": {"healthy": True, "fresh": 6, "total": 6},
            }
        ),
    )
    assert evaluate(evidence).description == "Health API returned stale data as a live response"
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": True,
                "stale": False,
                "inventory": {"healthy": False, "fresh": 5, "total": 6},
            }
        ),
    )
    assert "dishonestly" in evaluate(evidence).description
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": False,
                "stale": False,
                "inventory": {"healthy": True, "fresh": 6, "total": 6},
            }
        ),
    )
    assert "contradictory" in evaluate(evidence).description


def test_live_health_requires_nonempty_consistent_inventory_counts() -> None:
    evidence = probes()
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": False,
                "stale": False,
                "inventory": {"healthy": False, "fresh": 0, "total": 0},
            }
        ),
    )
    assert evaluate(evidence).state == "failure"
    evidence["health"] = Probe(
        200,
        json.dumps(
            {
                "ok": False,
                "stale": False,
                "inventory": {"healthy": False, "fresh": 7, "total": 6},
            }
        ),
    )
    assert evaluate(evidence).state == "failure"


def test_families_total_cannot_be_smaller_than_returned_page() -> None:
    evidence = probes()
    evidence["families"] = Probe(
        200, json.dumps({"items": [{"family_key": "a"}], "total": 0})
    )
    assert evaluate(evidence).state == "failure"


def test_missing_or_malformed_evidence_fails_closed() -> None:
    evidence = probes()
    del evidence["controller"]
    assert evaluate(evidence).state == "failure"
    evidence = probes()
    evidence["health"] = Probe(200, "not-json")
    assert evaluate(evidence).description == "Health API returned an invalid contract"


def test_deployed_asset_versions_and_legacy_runtime_fail_closed() -> None:
    evidence = probes()
    evidence["index"] = Probe(200, "api-resilience.js?v=2.0.0")
    assert "missing resilience assets" in evaluate(evidence).description

    evidence = probes()
    evidence["emergency"] = Probe(
        200,
        "MAX_EMERGENCY_AGE_MS = 0 retireLegacyState window.fetch = localStorage",
    )
    assert evaluate(evidence).description == "Legacy durable-cache runtime is active or invalid"
