"""`superops feedback` —— 零后端 bug 提交（PLAN.md 决策点三）。

doctor --json 诊断包 → 脱敏 → 落本地文件 → 打印预填 issue 链接。
脱敏铁律（公开名门禁先例）：诊断包禁含 token / 用户主目录 / 用户名 / 密钥样式串。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

DEFAULT_REPO = "4hx96f8jfk-cmyk/superops"
MAX_BODY_CHARS = 500  # issue 预填正文上限，完整包靠附件

# 脱敏规则（顺序敏感：先路径后通用 token 样式）
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(re.escape(str(Path.home()))), "~"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "/Users/<user>"),
    (re.compile(r"/home/[A-Za-z0-9._-]+"), "/home/<user>"),
    (re.compile(r"\b[0-9a-f]{32}\b", re.I), "<REDACTED-TOKEN>"),
    (
        re.compile(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)\S+"),
        r"\1<REDACTED>",
    ),
]


def sanitize(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def collect_bundle() -> dict:
    """复用 doctor 检查面；config 缺失也照常出包（config 检查本身即诊断）。"""
    from .main import _doctor_checks
    from .config import ConfigError, load_config

    bundle: dict = {"tool": "superops-cli", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        cfg = load_config()
        bundle["mode"] = cfg.mode
        bundle["checks"] = [
            {"level": lv, "name": n, "detail": d} for lv, n, d in _doctor_checks(cfg)
        ]
    except ConfigError as exc:
        bundle["checks"] = [{"level": "FAIL", "name": "config", "detail": str(exc)}]
    bundle["env"] = {k: "<set>" for k in os.environ if k.startswith("SUPEROPS_")}
    return bundle


def write_bundle(home: Path, bundle: dict) -> Path:
    fb_dir = home / "feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    clean = sanitize(json.dumps(bundle, ensure_ascii=False, indent=2))
    path = fb_dir / f"superops-feedback-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(clean)
    path.chmod(0o600)
    return path


def issue_url(bundle: dict, repo: str = DEFAULT_REPO) -> str:
    fails = [c for c in bundle.get("checks", []) if c["level"] != "OK"]
    title = f"[feedback] {fails[0]['name']}" if fails else "[feedback] 反馈"
    body_lines = [f"- 判定: {bundle.get('verdict', 'n/a')}"]
    for c in fails[:5]:
        body_lines.append(
            f"- [{c['level']}] {c['name']} — {sanitize(c['detail'])[:120]}"
        )
    body = "\n".join(body_lines)[:MAX_BODY_CHARS]
    return (
        f"https://github.com/{repo}/issues/new?title={quote(title)}&body={quote(body)}"
    )
