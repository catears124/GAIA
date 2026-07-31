import json

from gaia.production_smoke import Probe, evaluate, snapshot_is_usable


def probes() -> dict[str, Probe]:
    return {
        "index": Probe(200, '<script src="emergency-outage.js"></script><script src="api-resilience.js"></script>'),
        "emergency": Probe(200, "const MAX_EMERGENCY_AGE_MS = 1;"),
        "controller": Probe(200, "function liveHealthProbe(){ return new XMLHttpRequest(); }"),
        "snapshot": Probe(200, json.dumps({
            "generated_at": "2026-07-31T15:00:00Z",
            "family_index": [{"family_key": "a"}],
            "family_index_total": 1,
            "family_index_complete": True,
        })),
        "health": Probe(200, json.dumps({"ok": True, "stale": False, "inventory": {"healthy": True}})),
        "stats": Probe(200, "{}"),
        "families": Probe(200, json.dumps({"items": [], "total": 0})),
    }


def test_healthy_contract_succeeds() -> None:
    result = evaluate(probes())
    assert result.state == "success"


def test_outage_requires_a_complete_nonempty_snapshot() -> None:
    evidence = probes()
    evidence["health"] = Probe(500, "internal error")
    assert evaluate(evidence).state == "pending"

    payload = json.loads(evidence["snapshot"].body)
    payload["family_index_complete"] = False
    evidence["snapshot"] = Probe(200, json.dumps(payload))
    result = evaluate(evidence)
    assert result.state == "failure"
    assert "first-visit inventory snapshot is unusable" in result.description


def test_snapshot_rejects_duplicate_or_mismatched_family_indexes() -> None:
    payload = {
        "generated_at": "2026-07-31T15:00:00Z",
        "family_index": [{"family_key": "a"}, {"family_key": "a"}],
        "family_index_total": 2,
        "family_index_complete": True,
    }
    assert snapshot_is_usable(Probe(200, json.dumps(payload))) is False
    payload["family_index"] = [{"family_key": "a"}]
    assert snapshot_is_usable(Probe(200, json.dumps(payload))) is False


def test_live_health_cannot_be_stale_or_dishonest() -> None:
    evidence = probes()
    evidence["health"] = Probe(200, json.dumps({"ok": True, "stale": True, "inventory": {"healthy": True}}))
    assert evaluate(evidence).description == "Health API returned stale data as a live response"

    evidence["health"] = Probe(200, json.dumps({"ok": True, "inventory": {"healthy": False}}))
    assert "dishonestly" in evaluate(evidence).description


def test_families_total_cannot_be_smaller_than_returned_page() -> None:
    evidence = probes()
    evidence["families"] = Probe(200, json.dumps({"items": [{"family_key": "a"}], "total": 0}))
    assert evaluate(evidence).state == "failure"


def test_missing_or_malformed_evidence_fails_closed() -> None:
    evidence = probes()
    del evidence["controller"]
    assert evaluate(evidence).state == "failure"

    evidence = probes()
    evidence["health"] = Probe(200, "not-json")
    assert evaluate(evidence).description == "Health API returned an invalid contract"
