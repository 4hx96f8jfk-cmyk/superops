"""ops-console 离线测试（httpx TestClient，无网络）。

核心反例夹具：read-only 模式下调 POST /api/resolve 必须 403
——这是双模式语义的底线，破坏即回归失败。
"""

import json
import sqlite3
import time

import pytest
from effect_ledger import EffectLedger
from fastapi.testclient import TestClient

from superops_console import create_app
from superops_console.auth import load_or_create_token, token_equals

TOKEN = "a" * 32
HDR = {"X-SuperOps-Token": TOKEN}


def _seed_ledger(tmp_path):
    """造一条 CONFIRMED、一条 UNCERTAIN、一条 PAUSED（高风险不可探测）。"""
    db = str(tmp_path / "ledger.db")
    led = EffectLedger(db)
    b1 = led.begin("task-ok", "send-mail", {"to": "a@b.c"})
    led.complete(b1["intent_id"], b1["fencing_token"], {"msg_id": "m1"})
    b2 = led.begin("task-u", "charge", {"amt": 10})
    led.mark_receipt_lost(b2["intent_id"], b2["fencing_token"])
    b3 = led.begin("task-p", "wire-transfer", {"amt": 999}, risk="high")
    led.mark_receipt_lost(b3["intent_id"], b3["fencing_token"])
    led.reconcile(b3["intent_id"], lambda t, s: None)  # 高风险+探不到 → PAUSED
    ids = {"confirmed": b1["intent_id"], "uncertain": b2["intent_id"], "paused": b3["intent_id"]}
    led.close()
    return db, ids


@pytest.fixture
def seeded(tmp_path):
    return _seed_ledger(tmp_path)


def _client(db, mode="read-only", modelhub_db=None, probe=None):
    app = create_app(db, TOKEN, mode=mode, modelhub_db=modelhub_db, probe=probe)
    return TestClient(app)


# ---------- 反例夹具：read-only 写端点必须 403（双模式语义核心） ----------

class TestReadOnlyCounterExample:
    def test_resolve_403(self, seeded):
        db, ids = seeded
        c = _client(db, mode="read-only")
        r = c.post("/api/resolve", json={"intent_id": ids["paused"],
                                          "action": "approve", "note": "x"},
                   headers=HDR)
        assert r.status_code == 403
        assert r.json() == {"error": "mode=read-only"}
        # 反例的后半段：状态确实没被改动
        row = sqlite3.connect(db).execute(
            "SELECT state FROM effects WHERE intent_id=?", (ids["paused"],)).fetchone()
        assert row[0] == "PAUSED"

    def test_reconcile_403(self, seeded):
        db, ids = seeded
        c = _client(db, mode="read-only")
        r = c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        assert r.status_code == 403
        assert r.json() == {"error": "mode=read-only"}

    def test_probe_all_403(self, seeded):
        db, _ = seeded
        c = _client(db, mode="read-only")
        r = c.post("/api/providers/probe-all", headers=HDR)
        assert r.status_code == 403

    def test_reads_still_work_in_readonly(self, seeded):
        db, _ = seeded
        c = _client(db, mode="read-only")
        assert c.get("/api/effects", headers=HDR).status_code == 200
        assert c.get("/api/mode", headers=HDR).json() == {"mode": "read-only"}


# ---------- token 门：401 / 302 / cookie ----------

class TestTokenGate:
    def test_bad_token_401(self, seeded):
        db, _ = seeded
        c = _client(db)
        r = c.get("/api/effects", headers={"X-SuperOps-Token": "wrong"})
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_no_token_api_401(self, seeded):
        db, _ = seeded
        c = _client(db)
        assert c.get("/api/effects").status_code == 401

    def test_no_token_page_302_gate(self, seeded):
        db, _ = seeded
        c = _client(db)
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/gate"

    def test_gate_page_open(self, seeded):
        db, _ = seeded
        c = _client(db)
        r = c.get("/gate")
        assert r.status_code == 200
        assert "SuperOps" in r.text

    def test_token_submit_sets_httponly_cookie(self, seeded):
        db, _ = seeded
        c = _client(db)
        r = c.post("/api/token", json={"token": TOKEN})
        assert r.status_code == 200
        sc = r.headers["set-cookie"]
        assert "superops_token=" in sc and "httponly" in sc.lower()
        # cookie 已入 jar，后续请求免 header 直通
        assert c.get("/api/mode").json()["mode"] == "read-only"

    def test_token_submit_wrong_401(self, seeded):
        db, _ = seeded
        c = _client(db)
        assert c.post("/api/token", json={"token": "nope"}).status_code == 401

    def test_token_file_0600_32hex(self, tmp_path):
        p = tmp_path / "sub" / "token.txt"
        tok = load_or_create_token(p)
        assert len(tok) == 32 and all(ch in "0123456789abcdef" for ch in tok)
        assert p.stat().st_mode & 0o777 == 0o600
        assert load_or_create_token(p) == tok  # 幂等：二次读取不重生成
        assert token_equals(tok, tok) and not token_equals(None, tok)


# ---------- 只读端点正确性 ----------

class TestReadEndpoints:
    def test_effects_list_and_filter(self, seeded):
        db, _ = seeded
        c = _client(db)
        d = c.get("/api/effects", headers=HDR).json()
        assert d["count"] == 3
        d2 = c.get("/api/effects?state=PAUSED", headers=HDR).json()
        assert d2["count"] == 1 and d2["items"][0]["state"] == "PAUSED"
        d3 = c.get("/api/effects?limit=2", headers=HDR).json()
        assert d3["count"] == 2

    def test_effect_detail_with_audit_chain(self, seeded):
        db, ids = seeded
        c = _client(db)
        d = c.get(f"/api/effects/{ids['confirmed']}", headers=HDR).json()
        assert d["effect"]["state"] == "CONFIRMED"
        assert d["effect"]["receipt"]["msg_id"] == "m1"
        events = [a["event"] for a in d["audit"]]
        assert "INTENT_PERSISTED" in events and "CONFIRMED" in events

    def test_effect_detail_404(self, seeded):
        db, _ = seeded
        c = _client(db)
        assert c.get("/api/effects/eff-nonexistent", headers=HDR).status_code == 404

    def test_pending_summary(self, seeded):
        db, ids = seeded
        c = _client(db)
        d = c.get("/api/pending", headers=HDR).json()
        assert d["counts"] == {"uncertain": 1, "paused": 1}
        assert d["paused"][0]["intent_id"] == ids["paused"]
        assert d["uncertain"][0]["intent_id"] == ids["uncertain"]


# ---------- full 模式：写放行 + 双审计 ----------

class TestFullMode:
    def test_resolve_approve_moves_state_and_audits(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")
        r = c.post("/api/resolve", json={"intent_id": ids["paused"],
                                         "action": "approve", "note": "人工核实可重试"},
                   headers=HDR)
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "EXECUTE_ALLOWED"
        assert body["fencing_token"] == 2  # fencing token 递增，旧回执失效
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM effects WHERE intent_id=?",
                           (ids["paused"],)).fetchone()
        assert row["state"] == "EXECUTING" and row["fencing_token"] == 2
        # 双留痕：ledger audit_log + console_audit 都有记录
        ev = [x["event"] for x in conn.execute(
            "SELECT event FROM audit_log WHERE intent_id=?", (ids["paused"],))]
        assert "CONSOLE_APPROVED_RETRY" in ev
        ca = conn.execute("SELECT * FROM console_audit WHERE intent_id=?",
                          (ids["paused"],)).fetchall()
        assert len(ca) == 1
        rec = dict(ca[0])
        assert rec["who"] == "console" and rec["what"] == "resolve.approve"
        assert rec["at"] > 0 and "人工核实可重试" in rec["params"]

    def test_resolve_reject_stays_paused_but_audits(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")
        r = c.post("/api/resolve", json={"intent_id": ids["paused"],
                                         "action": "reject", "note": "风险未明"},
                   headers=HDR)
        assert r.status_code == 200 and r.json()["action"] == "STAY_PAUSED"
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT state FROM effects WHERE intent_id=?",
                            (ids["paused"],)).fetchone()[0] == "PAUSED"
        assert conn.execute("SELECT COUNT(*) FROM console_audit WHERE what=?",
                            ("resolve.reject",)).fetchone()[0] == 1

    def test_resolve_bad_action_400(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")
        r = c.post("/api/resolve", json={"intent_id": ids["paused"], "action": "yolo"},
                   headers=HDR)
        assert r.status_code == 400

    def test_resolve_non_paused_409(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")
        r = c.post("/api/resolve", json={"intent_id": ids["confirmed"], "action": "approve"},
                   headers=HDR)
        assert r.status_code == 409

    def test_reconcile_injected_probe_confirms(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full", probe=lambda t, s: True)
        r = c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        assert r.status_code == 200 and r.json()["action"] == "CONFIRMED"
        # reconcile 也是写操作 → console_audit 有记录
        ca = c.get("/api/audit", headers=HDR).json()["items"]
        assert any(x["what"] == "reconcile" for x in ca)

    def test_reconcile_default_probe_conservative(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")  # 默认 probe 返回 None，低风险 → 保持 UNCERTAIN
        r = c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        assert r.json()["action"] == "RECONCILE_LATER"

    def test_reconcile_wrong_state_409(self, seeded):
        db, ids = seeded
        c = _client(db, mode="full")
        r = c.post("/api/reconcile", json={"intent_id": ids["confirmed"]}, headers=HDR)
        assert r.status_code == 409

    def test_reconcile_unknown_404(self, seeded):
        db, _ = seeded
        c = _client(db, mode="full")
        assert c.post("/api/reconcile", json={"intent_id": "eff-x"},
                      headers=HDR).status_code == 404


# ---------- modelhub 集成 ----------

def _seed_modelhub(tmp_path):
    from modelhub import UsageRecord, UsageStore
    db = str(tmp_path / "modelhub.db")
    conn = sqlite3.connect(db)
    store = UsageStore(conn)
    now = time.time()
    store.record(UsageRecord(provider="local", model="q-test", prompt_tokens=100,
                             completion_tokens=20, ts=now))
    store.record(UsageRecord(provider="local", model="q-test", prompt_tokens=50,
                             completion_tokens=5, status="error", ts=now))
    conn.commit()
    conn.close()
    return db


class TestModelhub:
    def test_usage_aggregation(self, seeded, tmp_path):
        db, _ = seeded
        mh = _seed_modelhub(tmp_path)
        c = _client(db, mode="read-only", modelhub_db=mh)
        d = c.get("/api/usage", headers=HDR).json()
        assert d["available"] is True and len(d["items"]) == 1
        row = d["items"][0]
        assert row["calls"] == 2 and row["error_calls"] == 1
        assert row["prompt_tokens"] == 150

    def test_usage_without_modelhub(self, seeded):
        db, _ = seeded
        c = _client(db)
        assert c.get("/api/usage", headers=HDR).json() == {"available": False, "items": []}

    def test_probe_all_full_mode(self, seeded, tmp_path):
        db, _ = seeded
        mh = _seed_modelhub(tmp_path)
        c = _client(db, mode="full", modelhub_db=mh)
        r = c.post("/api/providers/probe-all", headers=HDR)
        assert r.status_code == 200  # 无注册 provider → 空结果，但链路通
        assert r.json()["items"] == {}

    def test_probe_all_without_db_404(self, seeded):
        db, _ = seeded
        c = _client(db, mode="full")
        assert c.post("/api/providers/probe-all", headers=HDR).status_code == 404


class TestFactoryContract:
    def test_bad_mode_rejected(self, seeded):
        db, _ = seeded
        with pytest.raises(ValueError):
            create_app(db, TOKEN, mode="yolo")

    def test_positional_contract(self, seeded):
        # CLI 子代理契约：前四参位置传参可用
        db, _ = seeded
        app = create_app(db, TOKEN, "full", None)
        assert app.state.mode == "full"
