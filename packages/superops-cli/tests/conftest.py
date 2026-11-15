"""superops-cli 测试夹具：离线、tmp SUPEROPS_HOME、click CliRunner。"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from superops_cli.main import cli


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """把 SUPEROPS_HOME 钉到临时目录，全部测试互不污染真实 ~/.superops。"""
    h = tmp_path / "superops-home"
    monkeypatch.setenv("SUPEROPS_HOME", str(h))
    return h
