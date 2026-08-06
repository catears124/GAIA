from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "gaia"


def test_supabase_owns_a_minute_level_runtime_clock() -> None:
    source = (SRC / "database_scheduler.py").read_text(encoding="utf-8")
    assert 'SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname=%s)' in source
    assert 'connection.execute("CREATE EXTENSION pg_cron")' in source
    assert 'connection.execute("CREATE EXTENSION pg_net WITH SCHEMA extensions")' in source
    assert '"SELECT cron.schedule(%s, %s, %s) AS job_id"' in source
    assert '(_JOB_NAME, "* * * * *", _cron_command(base_url))' in source
    assert "%L" not in source
    assert "/api/maintenance/continuous-tick" in source
    assert "GAIA-production-maintenance/supabase-cron" in source
    assert "timeout_milliseconds := 45000" in source


def test_scheduler_self_installs_on_real_api_traffic() -> None:
    source = (SRC / "request_bootstrap.py").read_text(encoding="utf-8")
    assert "_ensure_database_scheduler" in source
    assert "await asyncio.to_thread(install_database_scheduler)" in source
    assert "GAIA_ENABLE_DATABASE_CRON" in source
    assert "await _ensure_runtime_database()" in source
    assert "await _ensure_database_scheduler()" in source


def test_database_tick_drains_discord_after_inventory_commit() -> None:
    source = (SRC / "continuous_runtime_api.py").read_text(encoding="utf-8")
    inventory = source.index("inventory = await run_inventory_tick()")
    delivery = source.index("asyncio.to_thread(send_notifications, database)")
    assert inventory < delivery
    assert '"/api/maintenance/continuous-tick"' in source
    assert "resolved_runtime_secret" in source
    assert "GAIA_RUNTIME_DISCORD_MAX_PER_CHANNEL" in source


def test_runtime_secret_store_is_not_exposed_through_postgrest() -> None:
    source = (SRC / "runtime_secrets.py").read_text(encoding="utf-8")
    assert "ALTER TABLE gaia_runtime_secrets ENABLE ROW LEVEL SECURITY" in source
    assert "REVOKE ALL ON TABLE gaia_runtime_secrets FROM anon, authenticated" in source
    assert "ON CONFLICT(name) DO UPDATE" in source


def test_vercel_installs_continuous_runtime_components() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'os.environ.setdefault("GAIA_ENABLE_DATABASE_CRON", "1")' in source
    assert "install_continuous_runtime_api(app)" in source
