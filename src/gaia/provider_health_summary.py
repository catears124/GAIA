from __future__ import annotations

import json
import sys


def summarize(providers):
    keys = ("total", "running", "due", "fresh", "unhealthy", "degraded")
    totals = {key: sum(int(row.get(key, 0) or 0) for row in providers) for key in keys}
    total = totals["total"]
    return {
        "ok": totals["unhealthy"] == 0,
        **totals,
        "provider_count": len(providers),
        "fresh_percent": round(100 * totals["fresh"] / total, 1) if total else 0.0,
        "due_percent": round(100 * totals["due"] / total, 1) if total else 0.0,
    }


def main():
    payload = json.load(sys.stdin)
    if not isinstance(payload, list):
        raise SystemExit("provider health input must be a JSON list")
    print(json.dumps(summarize(payload), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
