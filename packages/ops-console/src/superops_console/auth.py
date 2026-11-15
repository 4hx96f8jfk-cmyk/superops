"""token 鉴权原语：本地会话 token 的生成/读取/常量时间比较。

机制沿用内部版已验证语义（32hex + 0600 + hmac.compare_digest 防时序侧信道），
仅路径参数化：env SUPEROPS_TOKEN_FILE，默认 ~/.superops/console.token
（与 superops-cli init 产出的文件名统一，杜绝两套默认值漂移）。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

DEFAULT_TOKEN_FILE = "~/.superops/console.token"
COOKIE_NAME = "superops_token"
HEADER_NAME = "x-superops-token"


def token_file_path() -> Path:
    """token 文件路径：env 优先，默认 ~/.superops/console.token。"""
    return Path(os.environ.get("SUPEROPS_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()


def load_or_create_token(path: str | Path | None = None) -> str:
    """读取 token 文件；不存在则生成随机 32hex 并以 0600 落盘。

    0600 是硬性要求：token 即单用户凭据，同机其他用户不可读。
    """
    p = Path(path).expanduser() if path else token_file_path()
    if p.exists():
        tok = p.read_text(encoding="ascii").strip()
        if tok:
            return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(16)  # 32 hex chars
    # 先建文件再写内容，确保权限位可控（避免 umask 竞态）
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(tok + "\n")
    os.chmod(str(p), 0o600)
    return tok


def token_equals(candidate: str | None, expected: str) -> bool:
    """常量时间比较，防时序侧信道。candidate 缺失直接 False，不抛异常。"""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8", "replace"),
                               expected.encode("utf-8", "replace"))
