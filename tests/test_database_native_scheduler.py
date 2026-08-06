from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "gaia"


def test_supabase_owns_inventory_verification_and_discovery_clocks() -> None:
    source = (SRC / "database_scheduler.py").read_text(encoding="utf-8")
    assert 'SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname=%s)' in source
    assert 'connection.execute("CREATE EXTENSION pg_cron")' in source
    assert 'connection.execute("CREATE EXTENSION pg_net WITH SCHEMA extensions")' in source
    assert '"SELECT cron.schedule(%s, %s, %s) AS job_id"' in source
    assert "%L" not in source
    assert 'schedule="* * * * *"' in source
    assert 'schedule="*/2 * * * *"' in source
    assert 'schedule="3,18,33,48 * * * *"' in source
    assert "/api/maintenance/continuous-tick" in source
    assert "/api/maintenance/verify-fresh-leads" in source
    assert "/api/maintenance/discover" in source
    assert "GAIA-production-maintenance/supabase-cron" in source
    assert "all_scheduler_statuses" in source


def test_scheduler_self_installs_on_real_api_traffic() -> None:
    source = (SRC / "request_bootstrap.py").read_text(encoding="utf-8")
    assert "_ensure_database_scheduler" in source
    assert "await asyncio.to_thread(install_database_scheduler)" in source
    assert "GAIA_ENABLE_DATABASE_CRON" in source
    assert "await _ensure_runtime_database()" in source
    assert "await _ensure_database_scheduler()" in source


def test_database_tick_publishes_and_drains_after_inventory_commit() -> None:
    source = (SRC / "continuous_runtime_api.py").read_text(encoding="utf-8")
    runtime = source.split("async def run_continuous_runtime_tick", 1)[1].split(
        "\n\nasync def run_runtime_lead_verification", 1
    )[0]
    assert "inventory = await run_inventory_tick()" in runtime
    assert "published = await publish_committed_updates" in runtime
    assert runtime.index("inventory = await run_inventory_tick()") < runtime.index(
        "published = await publish_committed_updates"
    )
    assert "asyncio.to_thread(send_notifications, database)" in source
    assert '"/api/maintenance/continuous-tick"' in source
    assert "resolved_runtime_secret" in source
    assert "GAIA_RUNTIME_DISCORD_MAX_PER_CHANNEL" in source


def test_fresh_lead_verification_is_bounded_and_publishes_immediately() -> None:
    source = (SRC / "continuous_runtime_api.py").read_text(encoding="utf-8")
    verification = source.split("async def run_runtime_lead_verification", 1)[1].split(
        "\n\ndef install_continuous_runtime_api", 1
    )[0]
    assert "promote_leads(" in verification
    assert "asyncio.wait_for(" in verification
    assert "GAIA_RUNTIME_VERIFICATION_LIMIT" in verification
    assert "GAIA_RUNTIME_VERIFICATION_TIMEOUT_SECONDS" in verification
    assert "published = await publish_committed_updates" in verification
    assert '"/api/maintenance/verify-fresh-leads"' in source


def test_runtime_secret_store_is_not_exposed_through_postgrest() -> None:
    source = (SRC / "runtime_secrets.py").read_text(encoding="utf-8")
    assert "ALTER TABLE gaia_runtime_secrets ENABLE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE gaia_runtime_secrets FROM anon, authenticated" in source
    assert "ON CONFLICT(name) DO UPDATE" in source


def test_vercel_installs_bounded_continuous_runtime_components() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("GAIA_ENABLE_DATABASE_CRON", "1")' in source
    assert 'os.environ.setdefault("GAIA_ENABLE_RUNTIME_VERIFICATION", "1")' in source
    assert 'os.environ.setdefault("GAIA_RUNTIME_VERIFICATION_LIMIT", "4")' in source
    assert 'os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_INTERVAL_SECONDS", "900")' in source
    assert 'os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_PROBE_LIMIT", "4")' in source
    assert "install_continuous_runtime_api(app)" in source
    assert "install_runtime_discovery_api(app)" in source
