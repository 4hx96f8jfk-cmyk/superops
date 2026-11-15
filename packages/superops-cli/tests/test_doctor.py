"""doctor：全绿 exit 0 / token 权限异常 WARN exit 2 / 缺 config FAIL exit 1。"""

from __future__ import annotations

import os
import sqlite3

from superops_cli.main import cli


def test_doctor_all_green(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "全部通过" in res.output


def test_doctor_json_output(runner, home):
    import json as _json

    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    res = runner.invoke(cli, ["doctor", "--json"])
    assert res.exit_code == 0, res.output
    data = _json.loads(res.output)
    assert data["verdict"] == "OK"
    # vendor 为 SUP-GUI-20261113 P0 新增校验项（vendored 库 sha256 钉死）
    assert {c["name"] for c in data["checks"]} == {
        "config",
        "ledger schema",
        "token",
        "modelhub",
        "vendor",
    }
    # WARN 场景：--json 下退出码语义不变
    os.chmod(home / "console.token", 0o644)
    res = runner.invoke(cli, ["doctor", "--json"])
    assert res.exit_code == 2
    assert _json.loads(res.output)["verdict"] == "WARN"


def test_doctor_token_perm_warn(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    os.chmod(home / "console.token", 0o644)
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 2, res.output  # WARN 专用退出码
    assert "[WARN] token" in res.output
    assert "0600" in res.output


def test_doctor_missing_config_fails(runner, home):
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 1
    assert "[FAIL] config" in res.output


def test_doctor_missing_db_fails(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    os.rename(home / "effects.db", home / "effects.db.gone")  # 禁 rm：改名模拟丢失
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 1
    assert "[FAIL]" in res.output


def test_doctor_modelhub_missing_warns(runner, home, tmp_path):
    mh = tmp_path / "modelhub.db"  # 配置了但不存在
    runner.invoke(
        cli, ["init", "--yes", "--modelhub-db", str(mh)], catch_exceptions=False
    )
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 2, res.output
    assert "[WARN] modelhub" in res.output


def test_doctor_modelhub_reachable_ok(runner, home, tmp_path):
    mh = tmp_path / "modelhub.db"
    conn = sqlite3.connect(mh)
    conn.execute("CREATE TABLE providers (name TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    runner.invoke(
        cli, ["init", "--yes", "--modelhub-db", str(mh)], catch_exceptions=False
    )
    res = runner.invoke(cli, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "[OK] modelhub" in res.output
