"""status：空库 / 有数据 / provider 计数 / 未初始化。"""

from __future__ import annotations

import sqlite3

from effect_ledger import EffectLedger

from superops_cli.main import cli


def _seed_effects(db_path):
    """造三种状态各一条：EXECUTING / CONFIRMED / UNCERTAIN。"""
    led = EffectLedger(str(db_path))
    b1 = led.begin("task-a", "step-1", {"x": 1})  # EXECUTING
    b2 = led.begin("task-b", "step-1", {"x": 2})
    led.complete(b2["intent_id"], b2["fencing_token"], {"ok": True})  # CONFIRMED
    b3 = led.begin("task-c", "step-1", {"x": 3})
    led.mark_receipt_lost(b3["intent_id"], b3["fencing_token"])  # UNCERTAIN
    led.close()
    return b1


def test_status_empty_db(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    res = runner.invoke(cli, ["status"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "console mode: full" in res.output
    assert "TOTAL       0" in res.output
    assert "pending (UNCERTAIN): 0" in res.output
    assert "modelhub:     未配置" in res.output


def test_status_with_data(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    _seed_effects(home / "effects.db")
    res = runner.invoke(cli, ["status"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "CONFIRMED   1" in res.output
    assert "EXECUTING   1" in res.output
    assert "UNCERTAIN   1" in res.output
    assert "TOTAL       3" in res.output
    assert "pending (UNCERTAIN): 1" in res.output


def test_status_counts_modelhub_providers(runner, home, tmp_path):
    mh = tmp_path / "modelhub.db"
    conn = sqlite3.connect(mh)
    conn.execute("CREATE TABLE providers (name TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO providers VALUES (?)", [("p1",), ("p2",)])
    conn.commit()
    conn.close()
    runner.invoke(
        cli, ["init", "--yes", "--modelhub-db", str(mh)], catch_exceptions=False
    )
    res = runner.invoke(cli, ["status"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "modelhub providers: 2" in res.output


def test_status_without_init_fails(runner, home):
    res = runner.invoke(cli, ["status"])
    assert res.exit_code != 0
    assert "superops init" in res.output
