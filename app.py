"""Vercel entrypoint for the GAIA FastAPI application.

The build imports only lightweight application code. Schema initialization and
empty-database recovery begin on the first real API request. Ongoing inventory ticks
also run inside Vercel, so a recreated Supabase project does not depend on stale
GitHub database credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

if os.getenv("VERCEL"):
    os.environ.setdefault("GAIA_INITIAL_SYNC", "0")
    os.environ.setdefault("GAIA_READ_ONLY", "1")
    os.environ.setdefault("GAIA_AUTO_MIGRATE", "0")
    os.environ.setdefault("GAIA_BOOTSTRAP_EMPTY_DATABASE", "1")
    os.environ.setdefault("GAIA_BOOTSTRAP_BUDGET_SECONDS", "38")
    os.environ.setdefault("GAIA_CANDIDATE_PROBE_LIMIT", "24")
    os.environ.setdefault("GAIA_ENABLE_RUNTIME_TICK", "1")
    os.environ.setdefault("GAIA_RUNTIME_TICK_INTERVAL_SECONDS", "120")
    os.environ.setdefault("GAIA_RUNTIME_TICK_BUDGET_SECONDS", "48")
    os.environ.setdefault("GAIA_RUNTIME_TICK_CONCURRENCY", "12")
    os.environ.setdefault("GAIA_ENABLE_RUNTIME_DYNAMIC_SOURCES", "1")
    os.environ.setdefault("GAIA_RUNTIME_DYNAMIC_SOURCE_INTERVAL_SECONDS", "120")
    os.environ.setdefault("GAIA_RUNTIME_DYNAMIC_SOURCE_LEASE_SECONDS", "180")
    os.environ.setdefault("GAIA_RUNTIME_DYNAMIC_SOURCE_PROBE_LIMIT", "24")
    os.environ.setdefault("GAIA_RUNTIME_DYNAMIC_SOURCE_CONCURRENCY", "12")
    os.environ.setdefault("GAIA_ENABLE_RUNTIME_MARKET_DISCOVERY", "1")
    os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_INTERVAL_SECONDS", "300")
    os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_LEASE_SECONDS", "240")
    os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_PROBE_LIMIT", "24")
    os.environ.setdefault("GAIA_RUNTIME_MARKET_DISCOVERY_CONCURRENCY", "10")
    os.environ.setdefault("GAIA_ENABLE_CONVERSION_DIAGNOSTICS", "1")
    os.environ.setdefault("GAIA_DIAGNOSTIC_CANDIDATE_LEASE_SECONDS", "120")
    os.environ.setdefault("GAIA_DB_TIMEOUT", "8")

from gaia.coverage_extensions import install_coverage_extensions  # noqa: E402
from gaia.domain_graph_coverage_extension import (  # noqa: E402
    install_domain_graph_coverage_extension,
)
from gaia.freshness_coverage_extension import (  # noqa: E402
    install_freshness_coverage_extension,
)
from gaia.json_feed_coverage_extension import (  # noqa: E402
    install_json_feed_coverage_extension,
)
from gaia.provider_expansion import install_provider_expansion  # noqa: E402
from gaia.runtime_coverage_extensions import (  # noqa: E402
    install_runtime_coverage_extensions,
)
from gaia.xml_feed_coverage_extension import (  # noqa: E402
    install_xml_feed_coverage_extension,
)

install_coverage_extensions()
install_provider_expansion()
install_runtime_coverage_extensions()
install_freshness_coverage_extension()
install_json_feed_coverage_extension()
install_xml_feed_coverage_extension()
install_domain_graph_coverage_extension()

from gaia.activity_api import install_activity_api  # noqa: E402
from gaia.api_resilience import install_database_outage_guard  # noqa: E402
from gaia.continuous_pulse_api import install_continuous_pulse_api  # noqa: E402
from gaia.conversion_diagnostics_api import (  # noqa: E402
    install_conversion_diagnostics_api,
)
from gaia.coverage_api import install_coverage_api  # noqa: E402
from gaia.maintenance_api import install_maintenance_api  # noqa: E402
from gaia.product_api import app  # noqa: E402,F401
from gaia.request_bootstrap import install_request_bootstrap  # noqa: E402
from gaia.runtime_discovery_api import install_runtime_discovery_api  # noqa: E402

install_request_bootstrap(app)
install_activity_api(app)
install_coverage_api(app)
install_maintenance_api(app)
install_continuous_pulse_api(app)
install_runtime_discovery_api(app)
install_conversion_diagnostics_api(app)
install_database_outage_guard(app)
