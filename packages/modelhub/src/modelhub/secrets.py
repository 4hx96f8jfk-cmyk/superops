"""密钥引用管理。

铁律（来自内部生产教训）：配置库中永不存明文密钥。
数据库里只允许两种引用形式：
  - ``env:VAR_NAME``            —— 从环境变量读取
  - ``keychain:service/account`` —— macOS Keychain 引用（security CLI 解析）

任何不带合法前缀的字符串（例如 ``sk-xxx``）在注册时直接拒绝，
从源头杜绝明文密钥落库。
"""

from __future__ import annotations

import os
import re
import subprocess

ENV_PREFIX = "env:"
KEYCHAIN_PREFIX = "keychain:"

# 明文密钥的常见形态（sk-... / gsk_... 等），即使没走注册入口也要拦截
_PLAINTEXT_HINT = re.compile(r"^(sk|gsk|pk|api)[-_][A-Za-z0-9]")


class SecretRefError(ValueError):
    """密钥引用非法（明文或缺少前缀）。"""


def validate_ref(ref: str) -> str:
    """校验引用格式，非法即抛错。返回原引用。"""
    if not isinstance(ref, str) or not ref:
        raise SecretRefError("密钥引用不能为空")
    if ref.startswith(ENV_PREFIX):
        var = ref[len(ENV_PREFIX) :]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise SecretRefError(f"非法环境变量名: {var!r}")
        return ref
    if ref.startswith(KEYCHAIN_PREFIX):
        body = ref[len(KEYCHAIN_PREFIX) :]
        if "/" not in body or not body.split("/", 1)[0] or not body.split("/", 1)[1]:
            raise SecretRefError(
                f"keychain 引用需为 keychain:service/account，收到 {ref!r}"
            )
        return ref
    if _PLAINTEXT_HINT.match(ref):
        raise SecretRefError(
            "疑似明文密钥，拒绝入库；请改用 env:VAR 或 keychain:service/account 引用"
        )
    raise SecretRefError(f"未知密钥引用前缀（需 env: 或 keychain:）: {ref!r}")


def resolve_secret(ref: str | None) -> str | None:
    """把引用解析成真实密钥值。ref 为 None 时返回 None（本地端点常无需鉴权）。"""
    if ref is None:
        return None
    validate_ref(ref)
    if ref.startswith(ENV_PREFIX):
        # 环境变量未设置不算错误：本地 llama.cpp/Ollama 通常无需 key，调用方自行决定是否要求
        return os.environ.get(ref[len(ENV_PREFIX) :])
    # keychain:service/account → macOS security CLI
    service, account = ref[len(KEYCHAIN_PREFIX) :].split("/", 1)
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SecretRefError(f"keychain 解析失败: {service}/{account}")
    return proc.stdout.strip()
