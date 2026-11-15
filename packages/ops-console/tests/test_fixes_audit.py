"""审计修复批次的回归测试（P1×5 + P2 批次，ops-console 侧）。

覆盖：P1-1 XSS（前端源码断言）、P1-2 probe-all 审计、P2① safe_json_body、
P2② minimap destroy、P2③ 限流器键回收、P2④ token 文件名统一+gate 动态注入、
P2⑤ __main__ 默认端口、P2⑥⑦⑧ 前端文案/计数卡/Delete 键（源码断言）。
"""

import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from effect_ledger import EffectLedger
from fastapi.testclient import TestClient

from superops_console import create_app
from superops_console.app import _SlidingWindowLimiter
from superops_console.auth import DEFAULT_TOKEN_FILE

TOKEN = "a" * 32
HDR = {"X-SuperOps-Token": TOKEN}
STATIC = Path(__file__).resolve().parents[1] / "src" / "superops_console" / "static"


def _seed_ledger(tmp_path):
    db = str(tmp_path / "ledger.db")
    led = EffectLedger(db)
    b1 = led.begin("task-ok", "send-mail", {"to": "a@b.c"})
    led.complete(b1["intent_id"], b1["fencing_token"], {"msg_id": "m1"})
    b2 = led.begin("task-u", "charge", {"amt": 10})
    led.mark_receipt_lost(b2["intent_id"], b2["fencing_token"])
    ids = {"confirmed": b1["intent_id"], "uncertain": b2["intent_id"]}
    led.close()
    return db, ids


@pytest.fixture
def seeded(tmp_path):
    return _seed_ledger(tmp_path)


def _client(db, mode="read-only", modelhub_db=None):
    return TestClient(create_app(db, TOKEN, mode=mode, modelhub_db=modelhub_db))


def _seed_modelhub(tmp_path):
    from modelhub import UsageRecord, UsageStore

    db = str(tmp_path / "modelhub.db")
    conn = sqlite3.connect(db)
    UsageStore(conn).record(
        UsageRecord(provider="local", model="q", prompt_tokens=1,
                    completion_tokens=1, ts=time.time())
    )
    conn.commit()
    conn.close()
    return db


# ---------- P1-2：probe-all 必须落 console_audit ----------

class TestProbeAllAudit:
    def test_probe_all_writes_audit_event(self, seeded, tmp_path):
        db, _ = seeded
        mh = _seed_modelhub(tmp_path)
        c = _client(db, mode="full", modelhub_db=mh)
        assert c.post("/api/providers/probe-all", headers=HDR).status_code == 200
        items = c.get("/api/audit", headers=HDR).json()["items"]
        ev = [x for x in items if x["what"] == "providers.probe_all"]
        assert len(ev) == 1
        rec = ev[0]
        assert rec["at"] > 0  # 触发时间
        assert '"probed"' in (rec["params"] or "")  # 结果计数入 params
        # 审计事件本身必须仍在哈希链上（全链校验通过）
        assert c.get("/api/audit/verify", headers=HDR).json()["valid"] is True

    def test_probe_all_not_audited_when_unconfigured(self, seeded):
        # modelhub 未配置 → 404，不产生审计事件（没做的事不留痕）
        db, _ = seeded
        c = _client(db, mode="full")
        assert c.post("/api/providers/probe-all", headers=HDR).status_code == 404
        items = c.get("/api/audit", headers=HDR).json()["items"]
        assert not [x for x in items if x["what"] == "providers.probe_all"]


# ---------- P2①：非法 JSON body 一律 400，不冒 500 ----------

class TestSafeJsonBody:
    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/token", b"{not json"),
            ("/api/reconcile", b"garbage"),
            ("/api/resolve", b"[1,2,3]"),  # 合法 JSON 但非对象
        ],
    )
    def test_bad_body_400(self, seeded, path, body):
        db, _ = seeded
        c = _client(db, mode="full")
        r = c.request("POST", path, content=body,
                      headers={**HDR, "Content-Type": "application/json"})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_empty_body_400(self, seeded):
        db, _ = seeded
        c = _client(db, mode="full")
        r = c.request("POST", "/api/reconcile", content=b"",
                      headers={**HDR, "Content-Type": "application/json"})
        assert r.status_code == 400


# ---------- P2③：限流器空 deque 键回收 ----------

class TestLimiterKeyHygiene:
    def test_is_blocked_does_not_create_keys(self):
        lim = _SlidingWindowLimiter(5, 60.0)
        for i in range(100):
            assert lim.is_blocked(f"10.0.0.{i}") is False
        assert len(lim._events) == 0  # 只读判定不得凭空建键

    def test_record_failure_prunes_empty_deques(self):
        lim = _SlidingWindowLimiter(5, 0.001)  # 1ms 窗口，历史键必然过期
        for i in range(70):
            lim.record_failure(f"10.1.0.{i}")
            time.sleep(0.002)
        lim.record_failure("last")
        # 过期清空的旧键在 record_failure 顺带回收，不随 IP 数无界增长
        assert len(lim._events) < 70


# ---------- P2④：token 默认文件名统一 + gate 动态注入 ----------

class TestTokenFileName:
    def test_default_is_console_token(self):
        assert DEFAULT_TOKEN_FILE.endswith("console.token")

    def test_gate_page_injects_actual_path(self, seeded, monkeypatch):
        db, _ = seeded
        monkeypatch.setenv("SUPEROPS_TOKEN_FILE", "/custom/my.token")
        c = _client(db)
        html = c.get("/gate").text
        assert "/custom/my.token" in html
        assert "__TOKEN_PATH__" not in html
        assert "token.txt" not in html

    def test_gate_page_default_path_hint(self, seeded, monkeypatch):
        db, _ = seeded
        monkeypatch.delenv("SUPEROPS_TOKEN_FILE", raising=False)
        html = _client(db).get("/gate").text
        assert "console.token" in html


# ---------- P2⑤：__main__ 默认端口 8795 ----------

class TestMainDefaultPort:
    def test_default_port_8795(self, seeded, tmp_path, monkeypatch):
        db, _ = seeded
        monkeypatch.setenv("SUPEROPS_TOKEN_FILE", str(tmp_path / "t"))
        calls = {}

        class FakeUvicorn:
            @staticmethod
            def run(app, host, port):
                calls.update(host=host, port=port)

        monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
        monkeypatch.setattr(sys, "argv", ["superops-console", "--db", db])
        from superops_console.__main__ import main

        main()
        assert calls["port"] == 8795
        assert calls["host"] == "127.0.0.1"


# ---------- P1-1 / P2②⑥⑦⑧：前端源码断言（无 JS 测试链，源码级回归） ----------

class TestFrontendSource:
    def test_index_no_inline_js_string_injection(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # 操作按钮不得再把 id 拼进 onclick JS 字符串
        assert 'onclick="resolve(' not in html
        assert 'onclick="reconcile(' not in html
        assert "onclick=\"showDetail('" not in html
        # 必须走 data-iid 属性 + 事件委托
        assert "data-iid=" in html
        assert "data-act=" in html
        assert "closest('button[data-act]')" in html

    def test_esc_escapes_quote_and_backslash(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "&#39;" in html and "&#92;" in html
        m = re.search(r"function esc\(s\)\{.*\}", html)
        assert m and re.search(r"\[&<>\"'\\\\\]", m.group(0))

    def test_stat_cards_present(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert 'id="statCards"' in html
        assert "待处理" in html and "UNCERTAIN" in html and "PAUSED" in html
        assert "d.counts" in html  # 消费 /api/pending 已有 counts

    def test_usage_headers_chinese(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "输入tok" in html and "输出tok" in html
        assert "prompt tok" not in html and "compl tok" not in html

    def test_layout_minimap_destroy(self):
        js = (STATIC / "js" / "layout.js").read_text(encoding="utf-8")
        m = re.search(r"export function attachMiniMap.*?\n\}", js, re.S)
        assert m and "destroy()" in m.group(0)

    def test_layout_delete_key_closes_panel(self):
        js = (STATIC / "js" / "layout.js").read_text(encoding="utf-8")
        assert 'e.key === "Delete"' in js
