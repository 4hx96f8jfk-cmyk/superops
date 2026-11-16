"""update / feedback / evolution 三命令离线夹具（合成数据逐关验通）。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from superops_cli.evolution import (
    EvolutionAdapter,
    NoopEvolutionAdapter,
    evolution_enabled,
    get_adapter,
)
from superops_cli.feedback import sanitize
from superops_cli.main import cli
from superops_cli.update import check_latest


def _fake_response(payload: dict):
    class R:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R()


# ---------------- update ----------------
def test_update_has_update_prints_commands_only():
    with patch("superops_cli.update.urllib.request.urlopen") as m:
        m.return_value = _fake_response({"info": {"version": "99.0.0"}})
        r = check_latest(base="http://fake/pypi")
    assert r["ok"] and r["has_update"]
    assert all("pip" in c or "uv" in c for c in r["upgrade_commands"])
    # 铁律: 只打印不执行——返回值里没有任何执行痕迹，命令面同样只 echo


def test_update_network_failure_degrades():
    with patch(
        "superops_cli.update.urllib.request.urlopen", side_effect=OSError("boom")
    ):
        r = check_latest(base="http://fake/pypi")
    assert r["ok"] is False and "OSError" in r["error"]


def test_update_cli_json(runner, home):
    with patch("superops_cli.update.urllib.request.urlopen") as m:
        m.return_value = _fake_response({"info": {"version": "0.0.1"}})
        res = runner.invoke(cli, ["update", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.output)["ok"] is True


# ---------------- feedback ----------------
def test_sanitize_strips_home_user_token():
    raw = (
        f"/Users/luca/.superops/token 值 abcdef0123456789abcdef0123456789 "
        f"token=SECRET123 /home/mamhum/x /Users/other/leak"
    )
    out = sanitize(raw)
    assert "/Users/luca" not in out and "/Users/other" not in out
    assert "/Users/<user>/leak" in out  # 非本人主目录也按用户名脱敏
    assert "abcdef0123456789abcdef0123456789" not in out
    assert "SECRET123" not in out
    assert "/home/mamhum" not in out


def test_feedback_writes_bundle_offline(runner, home):
    res = runner.invoke(cli, ["feedback"])
    assert res.exit_code == 0, res.output
    files = list((home / "feedback").glob("superops-feedback-*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "github.com/4hx96f8jfk-cmyk/superops/issues/new" in res.output
    assert files[0].stat().st_mode & 0o777 == 0o600
    # 反例守卫: 包内不得出现真实主目录
    from pathlib import Path as P

    assert str(P.home()) not in text


# ---------------- evolution ----------------
def test_stub_is_protocol_conformant_and_disabled():
    a = NoopEvolutionAdapter()
    assert isinstance(a, EvolutionAdapter)
    for call in (
        a.genome_provider(),
        a.task_runner({}),
        a.oracle({}),
        a.promotion_sink({}),
        a.evidence_sink({}),
    ):
        assert call["status"] == "disabled"


def test_evolution_default_off(tmp_path):
    assert evolution_enabled(tmp_path / "missing.toml") is False
    cfg = tmp_path / "config.toml"
    cfg.write_text("[evolution]\nenabled = true\n")
    assert evolution_enabled(cfg) is True
    # 即便声明开启，产品树内工厂仍只给 stub（引擎不出内网铁律）
    assert isinstance(get_adapter(cfg), NoopEvolutionAdapter)


def test_evolution_status_cli(runner, home):
    res = runner.invoke(cli, ["evolution", "status"])
    assert res.exit_code == 0
    assert "NoopEvolutionAdapter" in res.output
    assert "enabled(声明): False" in res.output
