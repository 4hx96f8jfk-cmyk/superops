"""per-provider timeout（M2-2.4）：慢端点超时后降级到下一家。"""

from __future__ import annotations

import time

import pytest

from modelhub import ModelHub, ProviderConfig


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db", failure_cooldown=0.0)
    yield h
    h.close()


def test_provider_timeout_default_30s():
    assert ProviderConfig(name="p", base_url="http://h:1/v1").timeout == 30.0


def test_slow_provider_times_out_and_falls_back(hub, mock_server):
    """crawl-model 睡 2s，timeout=0.3 → 超时降级到 ok-model，总耗时远小于 2s。"""
    base_url, state = mock_server
    hub.registry.register(
        ProviderConfig(
            name="slow",
            base_url=base_url,
            model="crawl-model",
            kind="local",
            timeout=0.3,
        )
    )
    hub.registry.register(
        ProviderConfig(
            name="fast", base_url=base_url, model="ok-model", kind="cloud", timeout=5.0
        )
    )
    t0 = time.monotonic()
    r = hub.chat([{"role": "user", "content": "hi"}])
    elapsed = time.monotonic() - t0
    assert r.provider == "fast"
    assert elapsed < 1.5  # 没等慢端点睡满 2s
    models = [req["body"]["model"] for req in state.requests]
    assert models == ["crawl-model", "ok-model"]


def test_timeout_error_recorded_in_attempts(hub, mock_server):
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(name="slow", base_url=base_url, model="crawl-model", timeout=0.2)
    )
    from modelhub import AllProvidersFailedError

    with pytest.raises(AllProvidersFailedError) as ei:
        hub.chat([{"role": "user", "content": "hi"}], provider="slow")
    assert "timed out" in ei.value.attempts[0]["error"].lower()
