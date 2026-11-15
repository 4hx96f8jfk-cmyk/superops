"""用量统计：流水记录 + 按日聚合。"""

from datetime import datetime, timezone

import pytest

from modelhub import ModelHub, ProviderConfig
from modelhub.usage import UsageRecord


@pytest.fixture()
def hub_env(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db")
    yield h, tmp_path
    h.close()


def test_chat_records_usage(hub_env, mock_server):
    hub, _ = hub_env
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(name="local", base_url=base_url, model="ok-model")
    )
    hub.chat([{"role": "user", "content": "hi"}])
    rows = hub.usage.all_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "local" and row["status"] == "ok"
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 7
    assert row["latency_ms"] and row["latency_ms"] > 0


def test_failed_attempts_recorded(hub_env, mock_server):
    hub, _ = hub_env
    base_url, _ = mock_server
    hub.registry.register(ProviderConfig(name="bad", base_url=base_url, model="boom"))
    hub.registry.register(
        ProviderConfig(name="good", base_url=base_url, model="ok-model", kind="cloud")
    )
    hub.chat([{"role": "user", "content": "hi"}])
    rows = hub.usage.all_rows()
    statuses = {(r["provider"], r["status"]) for r in rows}
    assert ("bad", "error") in statuses and ("good", "ok") in statuses


def test_daily_aggregation(hub_env):
    hub, _ = hub_env
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    yesterday_ts = datetime.now(tz=timezone.utc).timestamp() - 86400
    hub.usage.record(UsageRecord("p1", "m1", 10, 5, 100.0, "ok"))
    hub.usage.record(UsageRecord("p1", "m1", 20, 5, 300.0, "ok"))
    hub.usage.record(UsageRecord("p1", "m1", 0, 0, None, "error", ts=yesterday_ts))
    stats = hub.daily_usage(day=today)
    assert len(stats) == 1
    s = stats[0]
    assert s["calls"] == 2 and s["ok_calls"] == 2 and s["error_calls"] == 0
    assert s["prompt_tokens"] == 30 and s["completion_tokens"] == 10
    assert abs(s["avg_latency_ms"] - 200.0) < 1e-6
    # 全量聚合应分出两天
    all_stats = hub.daily_usage()
    assert len(all_stats) == 2
    # 按 provider 过滤
    assert hub.daily_usage(provider="nope") == []
