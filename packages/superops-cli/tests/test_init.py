"""init 向导：幂等、--force 备份、token 权限、非交互 flag 全覆盖。"""

from __future__ import annotations

import os
import re
import sqlite3
import stat

from superops_cli.main import cli


def _init(runner, home, *extra):
    return runner.invoke(cli, ["init", "--yes", *extra], catch_exceptions=False)


def test_init_creates_everything(runner, home):
    res = _init(runner, home)
    assert res.exit_code == 0, res.output
    assert (home / "effects.db").is_file()
    token_file = home / "console.token"
    assert token_file.is_file()
    assert (home / "config.toml").is_file()
    # token: 32 hex
    token = token_file.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    # 权限 0600
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    # 账本表已建（effect-ledger SCHEMA）
    conn = sqlite3.connect(home / "effects.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"effects", "audit_log"} <= tables
    # 下一步提示
    assert "superops status" in res.output


def test_init_idempotent_no_overwrite(runner, home):
    _init(runner, home)
    token_before = (home / "console.token").read_text()
    cfg_before = (home / "config.toml").read_text()
    res = _init(runner, home)
    assert res.exit_code == 0, res.output
    # 幂等：token 与 config 一字不变，也不产生 .bak
    assert (home / "console.token").read_text() == token_before
    assert (home / "config.toml").read_text() == cfg_before
    assert not (home / "console.token.bak").exists()


def test_init_force_backups_not_deletes(runner, home):
    _init(runner, home)
    token_before = (home / "console.token").read_text()
    res = _init(runner, home, "--force")
    assert res.exit_code == 0, res.output
    bak = home / "console.token.bak"
    assert bak.is_file(), "--force 必须把旧 token 改名 .bak 保留（禁删除）"
    assert bak.read_text() == token_before
    assert (home / "console.token").read_text() != token_before  # 新 token
    assert (home / "config.toml.bak").is_file()
    assert stat.S_IMODE(os.stat(home / "console.token").st_mode) == 0o600


def test_init_force_twice_keeps_both_baks(runner, home):
    _init(runner, home)
    _init(runner, home, "--force")
    _init(runner, home, "--force")
    # 第二个 .bak 带时间戳，绝不覆盖第一个 .bak
    assert (home / "console.token.bak").is_file()
    stamped = list(home.glob("console.token.bak.*"))
    assert stamped, "第二次 --force 应生成带时间戳的 .bak"


def test_init_flags_noninteractive(runner, home, tmp_path):
    mh = tmp_path / "modelhub.db"
    res = _init(
        runner, home, "--mode", "full", "--modelhub-db", str(mh), "--port", "9000"
    )
    assert res.exit_code == 0, res.output
    text = (home / "config.toml").read_text()
    assert 'mode = "full"' in text
    assert f'modelhub_db = "{mh}"' in text
    assert "port = 9000" in text


def test_init_interactive_accepts_defaults(runner, home):
    # 不带 --yes：走交互 prompt，全部回车取默认
    res = runner.invoke(cli, ["init"], input="\n\n\n\n\n", catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert (home / "config.toml").is_file()
    assert 'mode = "full"' in (home / "config.toml").read_text()
