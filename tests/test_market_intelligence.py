from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from bot.market_intelligence import MusashiInfraClient, compute_probability_change_24h


def _make_client(*, max_snapshot_age_minutes: int = 180) -> MusashiInfraClient:
    return MusashiInfraClient(
        supabase_url="https://example.supabase.co",
        api_key="test-key",
        timeout=5.0,
        enable_market_intelligence=True,
        enable_arbitrage_fallback=True,
        max_snapshot_age_minutes=max_snapshot_age_minutes,
    )


def test_compute_probability_change_uses_latest_snapshot_anchor():
    snapshots = [
        {"snapshot_time": "2024-01-01T00:00:00+00:00", "yes_price": 0.30},
        {"snapshot_time": "2024-01-02T00:00:00+00:00", "yes_price": 0.42},
        {"snapshot_time": "2024-01-03T00:00:00+00:00", "yes_price": 0.55},
    ]

    probability_change = compute_probability_change_24h(0.58, snapshots)

    assert probability_change == pytest.approx(0.16)


def test_compute_probability_change_requires_reference_within_tolerance():
    snapshots = [
        {"snapshot_time": "2024-01-01T00:00:00+00:00", "yes_price": 0.30},
        {"snapshot_time": "2024-01-03T00:00:00+00:00", "yes_price": 0.55},
    ]

    probability_change = compute_probability_change_24h(0.58, snapshots)

    assert probability_change is None


def test_list_cross_platform_markets_filters_stale_rows_and_sorts_locally(monkeypatch):
    client = _make_client(max_snapshot_age_minutes=120)
    now = datetime.now(timezone.utc)
    mock_select = Mock(
        return_value=[
            {
                "platform": "polymarket",
                "platform_id": "poly-older",
                "volume_24h": 8_000,
                "liquidity": 2_000,
                "last_snapshot_at": (now - timedelta(minutes=30)).isoformat(),
            },
            {
                "platform": "kalshi",
                "platform_id": "kalshi-top",
                "volume_24h": 10_000,
                "liquidity": 1_000,
                "last_snapshot_at": now.isoformat(),
            },
            {
                "platform": "polymarket",
                "platform_id": "poly-stale",
                "volume_24h": 50_000,
                "liquidity": 20_000,
                "last_snapshot_at": (now - timedelta(hours=5)).isoformat(),
            },
            {
                "platform": "kalshi",
                "platform_id": "kalshi-second",
                "volume_24h": 10_000,
                "liquidity": 900,
                "last_snapshot_at": (now - timedelta(minutes=10)).isoformat(),
            },
        ]
    )
    monkeypatch.setattr(client, "_select", mock_select)

    rows = client.list_cross_platform_markets(min_volume=500, limit=3)

    assert [row["platform_id"] for row in rows] == ["kalshi-top", "kalshi-second", "poly-older"]
    assert all(row["platform_id"] != "poly-stale" for row in rows)
    mock_select.assert_called_once()


def test_list_cross_platform_markets_uses_fresh_last_ingested_at_when_snapshot_is_stale(monkeypatch):
    client = _make_client(max_snapshot_age_minutes=120)
    now = datetime.now(timezone.utc)
    mock_select = Mock(
        return_value=[
            {
                "platform": "kalshi",
                "platform_id": "kalshi-refreshed",
                "volume_24h": 4_000,
                "liquidity": 500,
                "last_snapshot_at": (now - timedelta(hours=5)).isoformat(),
                "last_ingested_at": (now - timedelta(minutes=15)).isoformat(),
            },
            {
                "platform": "polymarket",
                "platform_id": "poly-stale",
                "volume_24h": 5_000,
                "liquidity": 800,
                "last_snapshot_at": (now - timedelta(hours=5)).isoformat(),
                "last_ingested_at": (now - timedelta(hours=4)).isoformat(),
            },
        ]
    )
    monkeypatch.setattr(client, "_select", mock_select)

    rows = client.list_cross_platform_markets(min_volume=0, limit=5)

    assert [row["platform_id"] for row in rows] == ["kalshi-refreshed"]
    mock_select.assert_called_once()


def test_build_market_context_includes_momentum_and_source_health(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_fetch_recent_snapshots",
        lambda canonical_market_id: [
            {"snapshot_time": "2024-01-01T00:00:00+00:00", "yes_price": 0.20},
            {"snapshot_time": "2024-01-02T00:00:00+00:00", "yes_price": 0.42},
            {"snapshot_time": "2024-01-03T00:00:00+00:00", "yes_price": 0.55},
        ],
    )
    monkeypatch.setattr(
        client,
        "_fetch_related_markets",
        lambda row: [
            {"platform": "kalshi", "platform_id": "kalshi-1"},
            {"platform": "polymarket", "platform_id": "poly-2"},
        ],
    )
    row = {
        "id": "canonical-1",
        "platform_id": "poly-1",
        "event_id": "evt-1",
        "series_id": "series-1",
        "title": "Will something happen?",
        "yes_price": 0.60,
        "no_price": 0.40,
        "volume_24h": 2_500,
        "open_interest": 8_000,
        "liquidity": 20_000,
        "status": "open",
        "is_active": True,
        "last_snapshot_at": datetime.now(timezone.utc).isoformat(),
        "last_ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_missing_at": None,
    }

    context = client._build_market_context(row, {"is_available": True})

    assert context["confidence_label"] == "high"
    assert context["probability_change_24h"] == pytest.approx(0.18)
    assert context["related_market_count"] == 2
    assert context["source_health"] == {"is_available": True}


def test_build_market_context_snapshot_age_uses_recent_ingest_when_snapshot_is_stale(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "_fetch_recent_snapshots", lambda canonical_market_id: [])
    monkeypatch.setattr(client, "_fetch_related_markets", lambda row: [])
    now = datetime.now(timezone.utc)
    row = {
        "id": "canonical-1",
        "platform_id": "poly-1",
        "event_id": "evt-1",
        "series_id": "series-1",
        "title": "Will something happen?",
        "yes_price": 0.60,
        "no_price": 0.40,
        "volume_24h": 2_500,
        "open_interest": 8_000,
        "liquidity": 20_000,
        "status": "open",
        "is_active": True,
        "last_snapshot_at": (now - timedelta(days=3)).isoformat(),
        "last_ingested_at": (now - timedelta(minutes=12)).isoformat(),
        "source_missing_at": None,
    }

    context = client._build_market_context(row, {"is_available": True})

    assert context["snapshot_age_minutes"] is not None
    assert context["snapshot_age_minutes"] < 60


def test_get_polymarket_contexts_uses_cache_on_repeat_lookup(monkeypatch):
    client = _make_client()
    row = {
        "id": "canonical-1",
        "platform_id": "poly-1",
        "event_id": "evt-1",
        "series_id": None,
        "title": "Will something happen?",
        "yes_price": 0.55,
        "no_price": 0.45,
        "volume_24h": 2_500,
        "open_interest": 8_000,
        "liquidity": 20_000,
        "status": "open",
        "is_active": True,
        "last_snapshot_at": datetime.now(timezone.utc).isoformat(),
        "last_ingested_at": datetime.now(timezone.utc).isoformat(),
        "source_missing_at": None,
    }
    mock_select = Mock(return_value=[row])
    monkeypatch.setattr(client, "_select", mock_select)
    monkeypatch.setattr(client, "get_source_health", lambda: {"polymarket": {"is_available": True}})
    monkeypatch.setattr(
        client,
        "_fetch_recent_snapshots",
        lambda canonical_market_id: [
            {"snapshot_time": "2024-01-02T00:00:00+00:00", "yes_price": 0.42},
            {"snapshot_time": "2024-01-03T00:00:00+00:00", "yes_price": 0.55},
        ],
    )
    monkeypatch.setattr(client, "_fetch_related_markets", lambda row: [])

    first = client.get_polymarket_contexts([{"id": "poly-1"}])
    second = client.get_polymarket_contexts([{"id": "poly-1"}])

    assert first["poly-1"]["platform_id"] == "poly-1"
    assert second["poly-1"] == first["poly-1"]
    mock_select.assert_called_once()
