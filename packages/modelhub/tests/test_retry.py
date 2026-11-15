"""错误分类重试（M2-2.3）：分类表 + 指数退避序列 + 不可重试立即降级。"""

from __future__ import annotations

import random

import httpx
import pytest

from modelhub import (
    AllProvidersFailedError,
    ModelHub,
    ProviderConfig,
    ProviderHTTPError,
    backoff_delay,
    is_retryable,
)

# ---- 分类表逐类测试 ----------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_retryable_http_status(code):
    assert is_retryable(ProviderHTTPError(code, "x")) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_non_retryable_http_status(code):
    assert is_retryable(ProviderHTTPError(code, "x")) is False


def test_retryable_transport_errors():
    # timeout / 连接错都属 httpx.TransportError 家族 → 可重试
    assert is_retryable(httpx.ConnectTimeout("t")) is True
    assert is_retryable(httpx.ReadTimeout("t")) is True
    assert is_retryable(httpx.ConnectError("c")) is True
    # 未知异常按不可重试（宁可快速降级）
    assert is_retryable(ValueError("bad shape")) is False


# ---- 退避公式 base * 2^n + jitter --------------------------------------------


def test_backoff_delay_sequence():
    rng = random.Random(0)
    d = [backoff_delay(n, base=0.5, jitter_max=0.1, rng=rng) for n in range(4)]
    assert len(d) == 4
    for n, v in enumerate(d):
        base = 0.5 * (2**n)
        assert base <= v <= base + 0.1  # 0.5, 1.0, 2.0, 4.0 + jitter
    assert d[0] != 0.5 or d[1] != 1.0  # jitter 生效（非确定值）


# ---- 端到端：退避序列断言（monkeypatch sleep） --------------------------------


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db", failure_cooldown=0.0)
    yield h
    h.close()


def test_retry_backoff_sleep_sequence(hub, mock_server, monkeypatch):
    """boom(500) 重试 3 次：sleep 序列必须落在 [0.5,0.6]、[1.0,1.1]。"""
    base_url, state = mock_server
    hub.registry.register(ProviderConfig(name="bad", base_url=base_url, model="boom"))
    sleeps: list[float] = []
    monkeypatch.setattr(hub.router, "_sleep", sleeps.append)
    with pytest.raises(AllProvidersFailedError):
        hub.chat(
            [{"role": "user", "content": "x"}], provider="bad", retries_per_provider=3
        )
    assert len(state.requests) == 3
    assert len(sleeps) == 2  # 最后一次失败后不再退避
    assert 0.5 <= sleeps[0] <= 0.6
    assert 1.0 <= sleeps[1] <= 1.1


def test_flaky_429_succeeds_after_retries(hub, mock_server, monkeypatch):
    """mock 429 两次后成功：共 3 次请求，退避 2 次。"""
    base_url, state = mock_server
    hub.registry.register(
        ProviderConfig(name="flaky", base_url=base_url, model="flaky-429")
    )
    sleeps: list[float] = []
    monkeypatch.setattr(hub.router, "_sleep", sleeps.append)
    r = hub.chat(
        [{"role": "user", "content": "x"}], provider="flaky", retries_per_provider=3
    )
    assert r.content == "flaky recovered"
    assert len(state.requests) == 3
    assert len(sleeps) == 2


def test_non_retryable_no_retry_and_falls_back(hub, mock_server, monkeypatch):
    """401 不重试、不退避，直接降级到下一家。"""
    base_url, state = mock_server
    hub.registry.register(
        ProviderConfig(name="deny", base_url=base_url, model="deny-401", kind="local")
    )
    hub.registry.register(
        ProviderConfig(name="good", base_url=base_url, model="ok-model", kind="cloud")
    )
    sleeps: list[float] = []
    monkeypatch.setattr(hub.router, "_sleep", sleeps.append)
    r = hub.chat([{"role": "user", "content": "x"}], retries_per_provider=3)
    assert r.provider == "good"
    models = [req["body"]["model"] for req in state.requests]
    assert models == ["deny-401", "ok-model"]  # 401 只打了一次
    assert sleeps == []
