"""路由：本地优先排序、权重、失败降级冷却、并发闸、回退链。"""

import threading

import pytest

from modelhub import AllProvidersFailedError, ModelHub, ProviderConfig


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db", max_concurrency=4, failure_cooldown=0.5)
    yield h
    h.close()


def _register_two(hub, mock_server, local_model="ok-model", cloud_model="ok-model"):
    base_url, state = mock_server
    # 故意先注册云端：排序必须靠 kind 而不是注册顺序
    hub.registry.register(
        ProviderConfig(name="cloud", base_url=base_url, model=cloud_model, kind="cloud")
    )
    hub.registry.register(
        ProviderConfig(name="local", base_url=base_url, model=local_model, kind="local")
    )
    return state


def test_local_first_ordering(hub, mock_server):
    state = _register_two(hub, mock_server)
    r = hub.chat([{"role": "user", "content": "hi"}])
    assert r.provider == "local"  # 本地优先，与注册顺序无关
    assert state.requests[-1]["body"]["model"] == "ok-model"


def test_weight_orders_within_kind(hub, mock_server):
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(
            name="c-low", base_url=base_url, model="ok-model", kind="cloud", weight=1.0
        )
    )
    hub.registry.register(
        ProviderConfig(
            name="c-high", base_url=base_url, model="ok-model", kind="cloud", weight=9.0
        )
    )
    names = [c.name for c in hub.router.candidates()]
    assert names == ["c-high", "c-low"]


def test_fallback_local_fails_to_cloud(hub, mock_server):
    state = _register_two(hub, mock_server, local_model="boom", cloud_model="ok-model")
    r = hub.chat([{"role": "user", "content": "hi"}])
    assert r.provider == "cloud"  # 自动降级到云端
    assert len(r.attempts) == 1  # 本地失败记录在案
    assert "500" in r.attempts[0]["error"]
    assert state.requests[-1]["body"]["model"] == "ok-model"


def test_failure_cooldown_skips_provider(hub, mock_server):
    state = _register_two(hub, mock_server, local_model="boom", cloud_model="ok-model")
    hub.chat([{"role": "user", "content": "hi"}])  # local 失败 → 进冷却
    assert "local" not in [c.name for c in hub.router.candidates()]
    # 第二次直接走 cloud，不再撞 local
    n_before = len(state.requests)
    r = hub.chat([{"role": "user", "content": "hi again"}])
    assert r.provider == "cloud"
    assert len(state.requests) == n_before + 1
    # 冷却期（0.5s）过后恢复
    import time

    time.sleep(0.6)
    assert "local" in [c.name for c in hub.router.candidates()]


def test_all_fail_raises_with_attempts(hub, mock_server):
    base_url, _ = mock_server
    hub.registry.register(ProviderConfig(name="only", base_url=base_url, model="boom"))
    with pytest.raises(AllProvidersFailedError) as ei:
        hub.chat([{"role": "user", "content": "hi"}])
    assert ei.value.attempts[0]["provider"] == "only"


def test_concurrency_gate_limits_inflight(tmp_path, mock_server):
    """max_concurrency=2，5 个并发线程打 slow-model，峰值在途必须 ≤2。"""
    base_url, state = mock_server
    hub = ModelHub(db_path=tmp_path / "gate.db", max_concurrency=2)
    hub.registry.register(
        ProviderConfig(name="slow", base_url=base_url, model="slow-model")
    )
    results, errors = [], []

    def worker():
        try:
            results.append(hub.chat([{"role": "user", "content": "x"}]))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 5
    assert state.max_inflight <= 2
    hub.close()


def test_kind_and_provider_filters(hub, mock_server):
    _register_two(hub, mock_server)
    assert [c.name for c in hub.router.candidates(kind="cloud")] == ["cloud"]
    assert [c.name for c in hub.router.candidates(provider="cloud")] == ["cloud"]
    assert hub.router.candidates(model="no-such-model") == []
