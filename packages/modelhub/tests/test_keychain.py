"""keychain resolve 真实测试（M2-2.5）：macOS security CLI 端到端。

用唯一 service 名创建测试密钥 → resolve_secret 取回一致 → 清理自己创建的条目。
非 macOS 环境（如 CI 的 Linux runner）整体 skip。
"""

from __future__ import annotations

import subprocess
import sys
import uuid

import pytest

from modelhub import SecretRefError, resolve_secret

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="keychain 测试仅 macOS（CI 跳过）"
)

TEST_VALUE = "keychain-unit-test-7c41d8e2"  # 测试专用假密钥，非真实凭据


def _security(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["security", *args], capture_output=True, text=True, timeout=30
    )


@pytest.fixture()
def keychain_entry():
    """唯一 service 名的测试密钥条目，用完即删（只删自己创建的）。"""
    service = f"hongyun-superops-test-{uuid.uuid4().hex[:12]}"
    account = "test-key"
    add = _security(
        "add-generic-password", "-U", "-s", service, "-a", account, "-w", TEST_VALUE
    )
    assert add.returncode == 0, f"security add 失败: {add.stderr.strip()}"
    yield service, account
    # 清理：只删本测试创建的 service，绝不触碰其他条目
    _security("delete-generic-password", "-s", service, "-a", account)


def test_resolve_secret_keychain_roundtrip(keychain_entry):
    service, account = keychain_entry
    ref = f"keychain:{service}/{account}"
    assert resolve_secret(ref) == TEST_VALUE


def test_resolve_secret_keychain_missing_raises():
    service = f"hongyun-superops-test-{uuid.uuid4().hex[:12]}"
    with pytest.raises(SecretRefError):
        resolve_secret(f"keychain:{service}/no-such-account")
