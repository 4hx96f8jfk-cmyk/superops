"""健康探针：TCP / 真实推理 / 假绿识别 / 缓存 TTL。"""

import time

from modelhub import (
    STATUS_DOWN,
    STATUS_HEALTHY,
    STATUS_REACHABLE,
    HealthProber,
    ProviderConfig,
    ProviderRegistry,
)


def _reg(*cfgs):
    reg = ProviderRegistry(":memory:")
    for c in cfgs:
        reg.register(c)
    return reg


def test_healthy_endpoint(mock_server):
    base_url, _ = mock_server
    reg = _reg(ProviderConfig(name="ok", base_url=base_url, model="ok-model"))
    res = HealthProber(reg).probe("ok")
    assert res.status == STATUS_HEALTHY
    assert res.tcp_ok and res.inference_ok
    assert res.checked_at <= time.time()
    reg.close()


def test_down_endpoint(dead_base_url):
    reg = _reg(ProviderConfig(name="dead", base_url=dead_base_url, model="m"))
    res = HealthProber(reg).probe("dead")
    assert res.status == STATUS_DOWN
    assert not res.tcp_ok and not res.inference_ok
    reg.close()


def test_reachable_but_not_inference_is_not_green(mock_server):
    """假绿陷阱：HTTP 200 形状正确但 content 为空 → 只能算 reachable，不能报 healthy。"""
    base_url, _ = mock_server
    reg = _reg(ProviderConfig(name="fake", base_url=base_url, model="empty-content"))
    res = HealthProber(reg).probe("fake")
    assert res.status == STATUS_REACHABLE
    assert res.tcp_ok and not res.inference_ok
    assert "max_tokens" in res.detail  # 提示 reasoning 模型加大预算
    reg.close()


def test_reasoning_model_counts_healthy(mock_server):
    """reasoning 模型 content 为空但 reasoning_content 有产出 → 真能推理。"""
    base_url, _ = mock_server
    reg = _reg(ProviderConfig(name="rsn", base_url=base_url, model="reasoning-model"))
    res = HealthProber(reg).probe("rsn")
    assert res.status == STATUS_HEALTHY
    assert "reasoning" in res.detail
    reg.close()


def test_probe_sends_adequate_max_tokens(mock_server):
    base_url, state = mock_server
    reg = _reg(ProviderConfig(name="ok", base_url=base_url, model="ok-model"))
    HealthProber(reg).probe("ok")
    assert state.requests[-1]["body"]["max_tokens"] >= 64  # 16 token 会被思考吃光
    reg.close()


def test_probe_cache_ttl(mock_server):
    base_url, state = mock_server
    reg = _reg(ProviderConfig(name="ok", base_url=base_url, model="ok-model"))
    prober = HealthProber(reg, cache_ttl=10.0)
    prober.probe("ok")
    prober.probe("ok")  # 命中缓存，不再打请求
    assert len(state.requests) == 1
    prober.probe("ok", force=True)  # 强制刷新
    assert len(state.requests) == 2
    # TTL=0 → 永不命中缓存
    prober0 = HealthProber(reg, cache_ttl=0.0)
    prober0.probe("ok")
    prober0.probe("ok")
    assert len(state.requests) == 4
    reg.close()


def test_probe_cache_persists_across_process_restart(tmp_path, mock_server):
    """M2-2.2 验收：缓存落 SQLite，重建 prober（模拟重启进程）仍命中。"""
    base_url, state = mock_server
    db = tmp_path / "hub.db"
    reg = ProviderRegistry(db)
    reg.register(ProviderConfig(name="ok", base_url=base_url, model="ok-model"))
    HealthProber(reg, cache_ttl=60.0).probe("ok")
    assert len(state.requests) == 1
    reg.close()
    # 『重启』：新开连接、新 prober —— 缓存必须来自 SQLite 而非内存
    reg2 = ProviderRegistry(db)
    prober2 = HealthProber(reg2, cache_ttl=60.0)
    res = prober2.probe("ok")
    assert res.status == STATUS_HEALTHY
    assert len(state.requests) == 1  # 未重复探活
    # force 无视持久缓存
    prober2.probe("ok", force=True)
    assert len(state.requests) == 2
    reg2.close()


def test_probe_cache_ttl_expiry_persisted(tmp_path, mock_server):
    """TTL 过期语义不变：expires_at 到点必须重新探活。"""
    base_url, state = mock_server
    reg = ProviderRegistry(tmp_path / "hub.db")
    reg.register(ProviderConfig(name="ok", base_url=base_url, model="ok-model"))
    prober = HealthProber(reg, cache_ttl=0.3)
    prober.probe("ok")
    prober.probe("ok")  # TTL 内命中持久缓存
    assert len(state.requests) == 1
    time.sleep(0.4)
    prober.probe("ok")  # 过期 → 重新探活
    assert len(state.requests) == 2
    reg.close()
