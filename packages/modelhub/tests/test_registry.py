"""ProviderRegistry：注册/删除/列出 + SQLite 持久化。"""

import pytest

from modelhub import ProviderConfig, ProviderRegistry


def test_register_list_get(tmp_path):
    reg = ProviderRegistry(tmp_path / "hub.db")
    reg.register(
        ProviderConfig(
            name="local1", base_url="http://127.0.0.1:8080/v1", model="qwen2.5-7b"
        )
    )
    reg.register(
        ProviderConfig(
            name="cloud1",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            kind="cloud",
            api_key_ref="env:OPENAI_API_KEY",
            weight=2.0,
        )
    )
    names = {c.name for c in reg.list()}
    assert names == {"local1", "cloud1"}
    assert reg.get("cloud1").kind == "cloud"
    assert reg.get("cloud1").weight == 2.0
    reg.close()


def test_persistence_across_reopen(tmp_path):
    db = tmp_path / "hub.db"
    reg = ProviderRegistry(db)
    reg.register(ProviderConfig(name="p", base_url="http://h:1/v1", model="m"))
    reg.close()
    reg2 = ProviderRegistry(db)
    assert reg2.get("p").model == "m"
    reg2.close()


def test_remove_and_update(tmp_path):
    reg = ProviderRegistry(tmp_path / "hub.db")
    reg.register(ProviderConfig(name="p", base_url="http://h:1/v1"))
    assert reg.remove("p") is True
    assert reg.remove("p") is False
    reg.register(ProviderConfig(name="p", base_url="http://h:1/v1", weight=1.0))
    reg.register(
        ProviderConfig(name="p", base_url="http://h:1/v1", weight=5.0)
    )  # 同名覆盖
    assert reg.get("p").weight == 5.0
    assert len(reg.list()) == 1
    reg.close()


def test_set_enabled_filters(tmp_path):
    reg = ProviderRegistry(tmp_path / "hub.db")
    reg.register(ProviderConfig(name="on", base_url="http://h:1/v1"))
    reg.register(ProviderConfig(name="off", base_url="http://h:2/v1"))
    reg.set_enabled("off", False)
    assert [c.name for c in reg.list(enabled_only=True)] == ["on"]
    reg.close()


def test_get_missing_raises():
    reg = ProviderRegistry(":memory:")
    with pytest.raises(KeyError):
        reg.get("nope")
    reg.close()


def test_base_url_normalization():
    cfg = ProviderConfig(name="p", base_url="http://h:1/v1/")
    assert cfg.base_url == "http://h:1/v1"
    with pytest.raises(ValueError):
        ProviderConfig(name="p", base_url="ftp://h:1")
