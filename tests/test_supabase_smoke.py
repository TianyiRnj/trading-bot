import os
import sys

import pytest
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.market_intelligence import MusashiInfraClient

load_dotenv()


@pytest.fixture(scope="module")
def live_client() -> MusashiInfraClient:
    url = os.getenv("MUSASHI_INFRA_SUPABASE_URL", "").strip()
    key = os.getenv("MUSASHI_INFRA_SUPABASE_KEY", "").strip()
    timeout = float(os.getenv("MUSASHI_INFRA_TIMEOUT_SECONDS", "10"))

    if not url or not key:
        pytest.skip("MUSASHI_INFRA_SUPABASE_URL / MUSASHI_INFRA_SUPABASE_KEY are not configured")

    return MusashiInfraClient(
        supabase_url=url,
        api_key=key,
        timeout=timeout,
        enable_market_intelligence=True,
        enable_arbitrage_fallback=True,
        max_snapshot_age_minutes=14 * 24 * 60,
    )


def test_source_health_query_returns_known_sources(live_client):
    source_health = live_client.get_source_health()

    assert isinstance(source_health, dict)
    assert "kalshi" in source_health


def test_markets_table_is_readable(live_client):
    rows = live_client._select(
        "markets",
        {
            "select": "id,platform,platform_id,last_ingested_at",
            "limit": "5",
        },
    )

    assert isinstance(rows, list)
    assert len(rows) > 0
    first_row = rows[0]
    assert "id" in first_row
    assert "platform" in first_row
    assert "platform_id" in first_row
    assert first_row["platform"] in {"polymarket", "kalshi"}


def test_polymarket_context_lookup_returns_rows_for_live_data(live_client):
    rows = live_client._select(
        "markets",
        {
            "select": "platform_id",
            "platform": "eq.polymarket",
            "is_active": "eq.true",
            "limit": "3",
        },
    )
    if not rows:
        pytest.skip("No active Polymarket rows available in musashi-infra markets table")

    contexts = live_client.get_polymarket_contexts(
        [{"id": str(row["platform_id"])} for row in rows if row.get("platform_id")]
    )

    assert isinstance(contexts, dict)
    assert len(contexts) > 0


def test_kalshi_active_rows_are_available_for_live_data(live_client):
    rows = live_client._select(
        "markets",
        {
            "select": "platform,platform_id,is_active,last_ingested_at",
            "platform": "eq.kalshi",
            "is_active": "eq.true",
            "limit": "3",
        },
    )
    if not rows:
        pytest.skip("No active Kalshi rows available in musashi-infra markets table")

    assert isinstance(rows, list)
    assert len(rows) > 0
    for row in rows:
        assert row["platform"] == "kalshi"
        assert row["platform_id"]
        assert row["is_active"] is True
        assert row["last_ingested_at"]


def test_cross_platform_market_fallback_query_completes(live_client):
    rows = live_client.list_cross_platform_markets(min_volume=0, limit=20)

    assert isinstance(rows, list)
    assert len(rows) > 0
    assert "platform" in rows[0]
    assert "platform_id" in rows[0]
    assert rows[0]["platform"] in {"polymarket", "kalshi"}
