from pathlib import Path

ROOT = Path(__file__).parents[1]
INVENTORY = ROOT / ".github" / "workflows" / "inventory.yml"
PROMOTION = ROOT / ".github" / "workflows" / "lead-promotion.yml"


def test_inventory_uses_one_frequent_runner_instead_of_a_runner_burst() -> None:
    text = INVENTORY.read_text(encoding="utf-8")
    assert 'cron: "1,6,11,16,21,26,31,36,41,46,51,56 * * * *"' in text
    assert "matrix:" not in text
    assert "max-parallel:" not in text
    assert "group: production-inventory-continuous" in text
    assert "cancel-in-progress: false" in text
    assert "--budget-seconds \"$budget\" --concurrency 24" in text


def test_discord_pump_starts_before_inventory_and_drains_afterward() -> None:
    text = INVENTORY.read_text(encoding="utf-8")
    watcher = text.index("--watch --interval-seconds 2")
    crawler = text.index("gaia worker --once")
    final_drain = text.index("python -m gaia.discord_notify > evidence/discord-final.json")
    assert watcher < crawler < final_drain
    assert "VERIFIED_DHOOK: ${{ secrets.VERIFIED_DHOOK }}" in text
    assert "LEADS_DHOOK: ${{ secrets.LEADS_DHOOK }}" in text
    assert "Both VERIFIED_DHOOK and LEADS_DHOOK are required" in text


def test_changed_inventory_rebuilds_the_public_feed_inside_the_same_pulse() -> None:
    text = INVENTORY.read_text(encoding="utf-8")
    assert 'if [ "$changed" -gt 0 ]; then' in text
    assert "Database(migrate=False).rebuild_families()" in text


def test_lead_promotion_is_short_frequent_and_notifies_during_verification() -> None:
    text = PROMOTION.read_text(encoding="utf-8")
    assert 'cron: "3,8,13,18,23,28,33,38,43,48,53,58 * * * *"' in text
    assert "timeout-minutes: 6" in text
    assert "for batch in 1 2; do" in text
    assert "seq 1 16" not in text
    watcher = text.index("--watch --interval-seconds 2")
    promotion = text.index("/promote-leads?limit=24")
    final_drain = text.index("python -m gaia.discord_notify > evidence/discord-final.json")
    assert watcher < promotion < final_drain
