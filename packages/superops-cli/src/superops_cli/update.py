"""`superops update` —— 版本检查与升级指引（只打印命令，绝不自动替换）。

设计（PLAN.md 决策点二）：
- 查 PyPI JSON API（镜像可配：SUPEROPS_PYPI_JSON_BASE，默认官方；清华镜像
  https://pypi.tuna.tsinghua.edu.cn/pypi 已实测同路径可用）。
- 「不自动替换」保用户主权与气隙场景：只输出可复制的升级命令。
- 网络失败降级为 WARN 提示，不影响退出码语义（0=已最新/有新版指引，2=网络不可达）。
"""

from __future__ import annotations

import json
import os
import urllib.request
from importlib import metadata

DEFAULT_JSON_BASE = "https://pypi.org/pypi"
PACKAGE = "superops-cli"
TIMEOUT_SECONDS = 10


def installed_version(package: str = PACKAGE) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def json_base(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get("SUPEROPS_PYPI_JSON_BASE") or DEFAULT_JSON_BASE).rstrip("/")


def _version_key(v: str) -> tuple:
    parts = []
    for chunk in v.split("+")[0].split("-")[0].split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def check_latest(package: str = PACKAGE, base: str | None = None) -> dict:
    """返回 {ok, current, latest, has_update, upgrade_commands}；网络失败 ok=False。"""
    url = f"{base or json_base()}/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 网络/解析失败统一降级
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}
    latest = (data.get("info") or {}).get("version") or ""
    current = installed_version(package)
    has_update = bool(latest) and _version_key(latest) > _version_key(current)
    return {
        "ok": True,
        "current": current,
        "latest": latest,
        "has_update": has_update,
        "upgrade_commands": (
            [
                f"uv tool upgrade {package}",
                f"pip install -U {package}",
                f"pip install -U {package} --index-url https://pypi.tuna.tsinghua.edu.cn/simple",
            ]
            if has_update
            else []
        ),
    }
