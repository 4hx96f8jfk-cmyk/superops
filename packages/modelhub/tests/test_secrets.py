"""密钥管理：引用格式校验 + 『DB 文件 grep 不到密钥值』硬证明。"""

import os

import pytest
from pydantic import ValidationError

from modelhub import (
    ModelHub,
    ProviderConfig,
    SecretRefError,
    resolve_secret,
    validate_ref,
)

# 测试用『密钥』值：故意不像真密钥（避免被脱敏钩子改写），但必须纯 ASCII（HTTP 头限制）
SECRET_VALUE = "unit-test-secret-9f3a2b7c"


def test_validate_ref_accepts_env_and_keychain():
    assert validate_ref("env:MY_KEY") == "env:MY_KEY"
    assert validate_ref("keychain:myapp/myaccount") == "keychain:myapp/myaccount"


def test_validate_ref_rejects_plaintext_and_garbage():
    for bad in [
        "sk-proj-abc123",
        "gsk_abc123",
        "",
        "plainword",
        "env:",
        "keychain:noslash",
    ]:
        with pytest.raises(SecretRefError):
            validate_ref(bad)


def test_resolve_secret_env(monkeypatch):
    monkeypatch.setenv("MH_TEST_KEY", SECRET_VALUE)
    assert resolve_secret("env:MH_TEST_KEY") == SECRET_VALUE
    assert resolve_secret(None) is None
    monkeypatch.delenv("MH_UNSET_KEY_X", raising=False)
    assert resolve_secret("env:MH_UNSET_KEY_X") is None


def test_register_rejects_plaintext_key(tmp_path):
    # pydantic 会把 field_validator 抛的 SecretRefError 包装成 ValidationError，两种都算拒收
    for bad in [SECRET_VALUE, "plaintext-key", "gsk_whatever"]:
        with pytest.raises((SecretRefError, ValidationError)):
            ProviderConfig(name="p", base_url="http://h:1/v1", api_key_ref=bad)


def test_db_file_never_contains_secret_value(tmp_path, monkeypatch, mock_server):
    """核心验收：注册带密钥引用的 provider + 真实调用后，DB 原始字节里 grep 不到密钥值。"""
    base_url, _ = mock_server
    monkeypatch.setenv("MH_SECRET_DB_TEST", SECRET_VALUE)
    db = tmp_path / "hub.db"

    hub = ModelHub(db_path=db)
    hub.registry.register(
        ProviderConfig(
            name="cloud",
            base_url=base_url,
            model="ok-model",
            kind="cloud",
            api_key_ref="env:MH_SECRET_DB_TEST",
        )
    )
    r = hub.chat([{"role": "user", "content": "hi"}], provider="cloud")
    assert r.content  # 调用确实发生（密钥被解析并用于请求头）
    hub.close()

    raw = db.read_bytes()
    assert SECRET_VALUE.encode() not in raw  # 密钥值不落库
    assert b"env:MH_SECRET_DB_TEST" in raw  # 只有引用落库


def test_secret_reaches_request_header_not_db(tmp_path, monkeypatch, mock_server):
    """密钥值只出现在运行时请求头，mock server 能收到 Bearer。"""
    base_url, state = mock_server
    monkeypatch.setenv("MH_SECRET_HDR_TEST", SECRET_VALUE)
    hub = ModelHub(db_path=tmp_path / "hub.db")
    hub.registry.register(
        ProviderConfig(
            name="c",
            base_url=base_url,
            model="ok-model",
            kind="cloud",
            api_key_ref="env:MH_SECRET_HDR_TEST",
        )
    )
    hub.chat([{"role": "user", "content": "hi"}], provider="c")
    assert state.requests[-1]["auth"] == f"Bearer {SECRET_VALUE}"
    hub.close()
