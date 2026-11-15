#!/usr/bin/env python3
"""发布泄漏扫描：检查仓库中是否残留内网 IP / 凭据 / 私钥。

用法: python tools/leak-scan.py [repo_root]
退出码: 0=干净, 1=发现疑似泄漏(打印明细)

CI 门禁用。规则刻意保守——宁可漏报不误杀，人工复核兜底。
"""

import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

PATTERNS = [
    (
        "私有内网IP",
        re.compile(
            r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    (
        "GitHub Token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})\b"),
    ),
    ("AWS Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("私钥块", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
]

# 允许出现内网 IP 的语境（文档示例、CI 配置）
ALLOW_HINTS = ("example.com", "127.0.0.1", "localhost")


def scan(root: Path):
    hits = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pat in PATTERNS:
                m = pat.search(line)
                if m and not (
                    name == "私有内网IP" and any(h in line for h in ALLOW_HINTS)
                ):
                    hits.append(
                        f"{path.relative_to(root)}:{lineno} [{name}] {line.strip()[:100]}"
                    )
    return hits


def main():
    root = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent
    )
    hits = scan(root)
    if hits:
        print(f"❌ 泄漏扫描发现 {len(hits)} 处疑似残留:")
        for h in hits:
            print("  " + h)
        sys.exit(1)
    print("✅ 泄漏扫描通过")


if __name__ == "__main__":
    main()
