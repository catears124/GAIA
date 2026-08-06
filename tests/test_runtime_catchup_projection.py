from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "gaia"


def test_partial_runtime_pulses_repair_the_public_feed() -> None:
    source = (SRC / "continuous_runtime_api.py").read_text(encoding="utf-8")

    assert "gaia_feed_projection_state" in source
    assert "MAX(first_seen_at)" in source
    assert "MAX(finished_at)" in source
    assert "removed_rows>0" in source
    assert "database.rebuild_families()" in source
    assert "pg_try_advisory_lock" in source
    assert "projection, projection_error = await _repair_feed(database)" in source
    assert "inventory = await run_inventory_tick()" in source
    assert "force=changed" in source
    assert source.index("projection, projection_error") < source.index(
        "inventory = await run_inventory_tick()"
    )


def test_deadline_cancelled_sources_do_not_starve_the_queue() -> None:
    source = (SRC / "live_inventory.py").read_text(encoding="utf-8")

    abandon = source.split("def abandon_target", 1)[1].split("\n    def finish_target", 1)[0]
    assert "GAIA_CANCELLED_TARGET_RETRY_SECONDS" in abandon
    assert "next_run_at=GREATEST" in abandon
    assert "last_status='partial'" in abandon
    assert "lease_owner=NULL" in abandon
    assert "LEAST(next_run_at, now())" not in abandon


def test_runtime_rotates_across_prime_source_shards() -> None:
    worker = (SRC / "live_inventory.py").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "GAIA_RUNTIME_SHARD_COUNT" in worker
    assert "int(time.time() // 60) % self.shard_count" in worker
    assert "mod(abs(hashtext(source)::bigint), %s) = %s" in worker
    assert 'os.environ.setdefault("GAIA_RUNTIME_SHARD_COUNT", "7")' in app
    assert 'os.environ.setdefault("GAIA_CANCELLED_TARGET_RETRY_SECONDS", "300")' in app


def test_continuous_status_exposes_feed_projection_lag() -> None:
    source = (SRC / "continuous_runtime_api.py").read_text(encoding="utf-8")

    assert '"feed_projection": feed_projection' in source
    assert '"lagging": not projected_row or posting_lag or removal_lag' in source
    assert '"latest_posting_at": current_posting' in source
    assert '"projected_posting_at": projected_posting' in source
