"""审计修复批次：serve --mode 覆盖、ledger list/pending、非回环警告。"""

from __future__ import annotations

import json

from effect_ledger import EffectLedger

from superops_cli.main import cli


def _seed(db_path):
    led = EffectLedger(str(db_path))
    b1 = led.begin("task-ok", "send-mail", {"to": "a@b.c"})
    led.complete(b1["intent_id"], b1["fencing_token"], {"msg_id": "m1"})
    b2 = led.begin("task-u", "charge", {"amt": 10})
    led.mark_receipt_lost(b2["intent_id"], b2["fencing_token"])
    led.close()
    return b1["intent_id"], b2["intent_id"]


def _all_output(res) -> str:
    """click 8.2+ stdout/stderr 分离，断言时合并。"""
    out = res.output
    try:
        out += res.stderr
    except (ValueError, AttributeError):
        pass
    return out


# ---------- P1-3：serve --mode 临时覆盖 config ----------

def test_serve_mode_flag_overrides_config(runner, home, monkeypatch):
    from superops_cli import main

    runner.invoke(cli, ["init", "--yes", "--mode", "full"], catch_exceptions=False)
    calls: dict = {}
    monkeypatch.setattr(
        main,
        "load_console_app",
        lambda db_path, token, mode, modelhub_db=None, wazuh_compose=None: calls.update(mode=mode)
        or object(),
    )
    monkeypatch.setattr(main, "run_server", lambda app, host, port: None)
    res = runner.invoke(cli, ["serve", "--mode", "read-only"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert calls["mode"] == "read-only"  # flag 覆盖 config 的 full
    assert "mode=read-only" in res.output


def test_serve_mode_flag_validated(runner, home):
    res = runner.invoke(cli, ["serve", "--mode", "yolo"])
    assert res.exit_code != 0
    assert "Invalid value" in res.output or "invalid" in res.output.lower()


# ---------- P2⑤：serve 非回环 host 醒目警告 ----------

def test_serve_warns_on_non_loopback_host(runner, home, monkeypatch):
    from superops_cli import main

    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.setattr(
        main,
        "load_console_app",
        lambda **kw: object(),
    )
    monkeypatch.setattr(main, "run_server", lambda app, host, port: None)
    res = runner.invoke(cli, ["serve", "--host", "0.0.0.0"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    out = _all_output(res)
    assert "警告" in out and "非回环地址" in out and "0.0.0.0" in out


def test_serve_no_warning_on_loopback(runner, home, monkeypatch):
    from superops_cli import main

    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.setattr(
        main,
        "load_console_app",
        lambda **kw: object(),
    )
    monkeypatch.setattr(main, "run_server", lambda app, host, port: None)
    res = runner.invoke(cli, ["serve"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "警告" not in _all_output(res)


# ---------- P1-4：ledger list / pending ----------

def test_ledger_list_text_and_filter(runner, home):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    confirmed, uncertain = _seed(home / "effects.db")
    res = runner.invoke(cli, ["ledger", "list"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert confirmed in res.output and uncertain in res.output
    assert "共 2 条" in res.output
    res2 = runner.invoke(cli, ["ledger", "list", "--state", "UNCERTAIN"],
                         catch_exceptions=False)
    assert uncertain in res2.output and confirmed not in res2.output


def test_ledger_list_json_limit(runner, home):
    import re

    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    _seed(home / "effects.db")
    res = runner.invoke(cli, ["ledger", "list", "--json", "--limit", "1"],
                        catch_exceptions=False)
    d = json.loads(re.sub(r"^Usage:.*$", "", res.output, flags=re.M).strip().splitlines()[-1])
    assert d["count"] == 1 and len(d["items"]) == 1
    assert {"intent_id", "state", "task_id", "step_id"} <= set(d["items"][0])


def test_ledger_pending_text_and_json(runner, home):
    import re

    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    _, uncertain = _seed(home / "effects.db")
    res = runner.invoke(cli, ["ledger", "pending"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "UNCERTAIN（待对账）: 1" in res.output
    assert "PAUSED（待人工裁决）: 0" in res.output
    assert uncertain in res.output
    res2 = runner.invoke(cli, ["ledger", "pending", "--json"], catch_exceptions=False)
    d = json.loads(res2.output.strip().splitlines()[-1])
    assert d["counts"] == {"uncertain": 1, "paused": 0}
    assert d["uncertain"][0]["intent_id"] == uncertain


def test_ledger_without_init_fails(runner, home):
    res = runner.invoke(cli, ["ledger", "list"])
    assert res.exit_code != 0
    assert "superops init" in res.output
