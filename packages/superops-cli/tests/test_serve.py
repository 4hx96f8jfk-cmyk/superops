"""serve：console 缺失时人话报错；软依赖注入路径可测。"""

from __future__ import annotations

from superops_cli import main
from superops_cli.main import cli


def test_serve_console_missing_clear_error(runner, home, monkeypatch):
    runner.invoke(cli, ["init", "--yes"], catch_exceptions=False)
    # sys.modules 置 None 使 import superops_console 必抛 ImportError——
    # 不依赖 sibling 包是否恰好可导入，测试确定性优先
    monkeypatch.setitem(__import__("sys").modules, "superops_console", None)
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code != 0
    assert "console 未安装：pip install superops-console" in res.output


def test_serve_passes_config_to_console(runner, home, monkeypatch):
    """create_app 必须收到 config 里的 db/token/mode（未配 modelhub 时为 None）；uvicorn 用桩替掉。"""
    runner.invoke(cli, ["init", "--yes", "--mode", "full"], catch_exceptions=False)
    calls: dict = {}

    def fake_app(db_path, token, mode, modelhub_db):
        calls.update(db_path=db_path, token=token, mode=mode, modelhub_db=modelhub_db)
        return object()

    def fake_run(app, host, port):
        calls.update(app_is_sentinel=app is not None, host=host, port=port)

    monkeypatch.setattr(
        main,
        "load_console_app",
        lambda db_path, token, mode, modelhub_db=None, wazuh_compose=None: fake_app(
            db_path, token, mode, modelhub_db
        ),
    )
    monkeypatch.setattr(main, "run_server", fake_run)
    res = runner.invoke(cli, ["serve"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert calls["mode"] == "full"
    assert calls["db_path"] == str(home / "effects.db")
    assert calls["token"] == (home / "console.token").read_text().strip()
    assert calls["port"] == 8795
    assert calls["modelhub_db"] is None  # init --yes 未配 modelhub_db


def test_serve_passes_modelhub_db(runner, home, monkeypatch):
    """config 里配了 modelhub_db 时 serve 必须把绝对路径透传给 create_app。"""
    runner.invoke(
        cli,
        ["init", "--yes", "--modelhub-db", str(home / "modelhub.db")],
        catch_exceptions=False,
    )
    calls: dict = {}
    monkeypatch.setattr(
        main,
        "load_console_app",
        lambda db_path, token, mode, modelhub_db=None, wazuh_compose=None: calls.update(
            modelhub_db=modelhub_db
        )
        or object(),
    )
    monkeypatch.setattr(main, "run_server", lambda app, host, port: None)
    res = runner.invoke(cli, ["serve"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert calls["modelhub_db"] == str(home / "modelhub.db")


def test_serve_without_init_fails(runner, home):
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code != 0
    assert "superops init" in res.output
