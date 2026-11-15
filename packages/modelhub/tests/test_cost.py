"""用量成本估算（M2-2.6）：价格表 config + 按 provider 聚合 + 手算核对。"""

from __future__ import annotations

import pytest

from modelhub import ModelHub, ProviderConfig
from modelhub.usage import UsageRecord


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db")
    yield h
    h.close()


def test_price_defaults_zero_local_free():
    cfg = ProviderConfig(name="p", base_url="http://h:1/v1")
    assert cfg.price_input_per_1k == 0.0 and cfg.price_output_per_1k == 0.0


def test_cost_summary_matches_hand_computation(hub):
    """灌流水后手算：p-cloud = 1500/1000*2.0 + 500/1000*4.0 = 5.0。"""
    hub.registry.register(
        ProviderConfig(
            name="p-cloud",
            base_url="http://h:1/v1",
            kind="cloud",
            price_input_per_1k=2.0,
            price_output_per_1k=4.0,
        )
    )
    hub.registry.register(ProviderConfig(name="p-local", base_url="http://h:2/v1"))
    hub.usage.record(UsageRecord("p-cloud", "gpt", 1000, 300, 10.0, "ok"))
    hub.usage.record(UsageRecord("p-cloud", "gpt", 500, 200, 20.0, "ok"))
    hub.usage.record(UsageRecord("p-local", "qwen", 800, 100, 30.0, "ok"))
    hub.usage.record(UsageRecord("p-cloud", "gpt", 0, 0, None, "error"))

    rows = {r["provider"]: r for r in hub.usage_summary()}
    assert set(rows) == {"p-cloud", "p-local"}
    cloud = rows["p-cloud"]
    assert cloud["calls"] == 3 and cloud["ok_calls"] == 2 and cloud["error_calls"] == 1
    assert cloud["prompt_tokens"] == 1500 and cloud["completion_tokens"] == 500
    assert abs(cloud["cost"] - (1500 / 1000 * 2.0 + 500 / 1000 * 4.0)) < 1e-9
    # 本地默认价 0 → 免费
    assert rows["p-local"]["cost"] == 0.0


def test_cost_summary_provider_filter(hub):
    hub.usage.record(UsageRecord("a", "m", 100, 50, 1.0, "ok"))
    hub.usage.record(UsageRecord("b", "m", 200, 60, 1.0, "ok"))
    rows = hub.usage_summary(provider="b")
    assert len(rows) == 1 and rows[0]["provider"] == "b"
    assert rows[0]["cost"] == 0.0  # 未配价格 = 免费


def test_cost_summary_after_real_chat_uses_registry_prices(hub, mock_server):
    """真实调用链路：注册价 → chat → summary 成本与流水一致。"""
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(
            name="c",
            base_url=base_url,
            model="ok-model",
            kind="cloud",
            price_input_per_1k=1.0,
            price_output_per_1k=2.0,
        )
    )
    hub.chat([{"role": "user", "content": "hi"}], provider="c")
    rows = {r["provider"]: r for r in hub.usage_summary()}
    # mock server usage: prompt=11, completion=7
    assert abs(rows["c"]["cost"] - (11 / 1000 * 1.0 + 7 / 1000 * 2.0)) < 1e-9
