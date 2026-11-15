"""客户端封装：chat() 自动选路、provider 归属、参数透传、kwargs。"""

import pytest

from modelhub import ModelHub, ProviderConfig


@pytest.fixture()
def hub(tmp_path):
    h = ModelHub(db_path=tmp_path / "hub.db")
    yield h
    h.close()


def test_chat_returns_provider_attribution(hub, mock_server):
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(name="loc", base_url=base_url, model="ok-model")
    )
    r = hub.chat([{"role": "user", "content": "你好"}])
    assert r.provider == "loc"
    assert r.model == "ok-model"
    assert r.content == "hello from ok-model"
    assert r.finish_reason == "stop"
    assert r.prompt_tokens == 11 and r.completion_tokens == 7
    assert r.latency_ms > 0


def test_kwargs_passthrough(hub, mock_server):
    base_url, state = mock_server
    hub.registry.register(
        ProviderConfig(name="loc", base_url=base_url, model="ok-model")
    )
    hub.chat([{"role": "user", "content": "x"}], temperature=0.2, max_tokens=99)
    body = state.requests[-1]["body"]
    assert body["temperature"] == 0.2 and body["max_tokens"] == 99


def test_retries_per_provider(hub, mock_server):
    base_url, state = mock_server
    hub.registry.register(ProviderConfig(name="bad", base_url=base_url, model="boom"))
    with pytest.raises(Exception):
        hub.chat(
            [{"role": "user", "content": "x"}], provider="bad", retries_per_provider=3
        )
    assert len(state.requests) == 3  # 同一 provider 重试 3 次


def test_probe_facade(hub, mock_server, dead_base_url):
    base_url, _ = mock_server
    hub.registry.register(
        ProviderConfig(name="up", base_url=base_url, model="ok-model")
    )
    hub.registry.register(
        ProviderConfig(name="down", base_url=dead_base_url, model="m")
    )
    results = hub.probe()
    assert results["up"].status == "healthy"
    assert results["down"].status == "down"
    single = hub.probe("up")
    assert "up" in single


def test_context_manager_and_file_db(tmp_path):
    db = tmp_path / "hub.db"
    with ModelHub(db_path=db) as hub:
        hub.registry.register(ProviderConfig(name="p", base_url="http://h:1/v1"))
    assert db.exists()
