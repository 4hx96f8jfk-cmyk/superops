"""M3 安全与体验补强测试：审计防篡改链、gate 限流、cookie 安全位、
CLI --lan 绑定、端到端冒烟（gate→effects→resolve→audit/verify）。

反例是核心：直接 UPDATE 篡改审计行必须被 verify 抓到；token 失败灌
6 次第 6 次必须 429。
"""

import sqlite3

import pytest
from effect_ledger import EffectLedger
from fastapi.testclient import TestClient

from superops_console import create_app

TOKEN = "a" * 32
HDR = {"X-SuperOps-Token": TOKEN}

# 旧版（无 hash 列）console_audit 建表语句：模拟迁移前存量库
OLD_AUDIT_SCHEMA = """
CREATE TABLE console_audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  who       TEXT NOT NULL,
  what      TEXT NOT NULL,
  intent_id TEXT,
  params    TEXT,
  at        REAL NOT NULL
);
"""


def _seed_ledger(tmp_path):
    """CONFIRMED / UNCERTAIN / PAUSED 各一条（与主测试文件同款夹具）。"""
    db = str(tmp_path / "ledger.db")
    led = EffectLedger(db)
    b1 = led.begin("task-ok", "send-mail", {"to": "a@b.c"})
    led.complete(b1["intent_id"], b1["fencing_token"], {"msg_id": "m1"})
    b2 = led.begin("task-u", "charge", {"amt": 10})
    led.mark_receipt_lost(b2["intent_id"], b2["fencing_token"])
    b3 = led.begin("task-p", "wire-transfer", {"amt": 999}, risk="high")
    led.mark_receipt_lost(b3["intent_id"], b3["fencing_token"])
    led.reconcile(b3["intent_id"], lambda t, s: None)
    ids = {"confirmed": b1["intent_id"], "uncertain": b2["intent_id"],
           "paused": b3["intent_id"]}
    led.close()
    return db, ids


@pytest.fixture
def seeded(tmp_path):
    return _seed_ledger(tmp_path)


def _client(db, mode="full"):
    return TestClient(create_app(db, TOKEN, mode=mode))


# ---------- 审计防篡改哈希链 ----------

class TestAuditChain:
    def test_empty_chain_valid(self, seeded):
        db, _ = seeded
        c = _client(db)
        d = c.get("/api/audit/verify", headers=HDR).json()
        assert d["valid"] is True and d["broken_at"] is None

    def test_writes_chain_and_verify_ok(self, seeded):
        db, ids = seeded
        c = _client(db)
        c.post("/api/resolve", json={"intent_id": ids["paused"],
                                     "action": "approve", "note": "n1"}, headers=HDR)
        c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        d = c.get("/api/audit/verify", headers=HDR).json()
        assert d["valid"] is True and d["checked"] >= 2
        # 每行都带上了链字段
        items = c.get("/api/audit", headers=HDR).json()["items"]
        assert all(x["row_hash"] and x["prev_hash"] for x in items)

    def test_tamper_row_detected(self, seeded):
        """反例：直接 UPDATE 篡改一行审计 → valid=false 且 broken_at 指向该行。"""
        db, ids = seeded
        c = _client(db)
        c.post("/api/resolve", json={"intent_id": ids["paused"],
                                     "action": "approve", "note": "n1"}, headers=HDR)
        c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        assert c.get("/api/audit/verify", headers=HDR).json()["valid"] is True
        # 绕过应用直接改库：篡改中间行的 params
        conn = sqlite3.connect(db)
        target = conn.execute(
            "SELECT id FROM console_audit ORDER BY id LIMIT 1 OFFSET 1").fetchone()[0]
        conn.execute("UPDATE console_audit SET params='{\"evil\":1}' WHERE id=?",
                     (target,))
        conn.commit()
        conn.close()
        d = c.get("/api/audit/verify", headers=HDR).json()
        assert d["valid"] is False
        assert d["broken_at"]["id"] == target
        assert d["broken_at"]["reason"] == "row_hash mismatch"

    def test_row_deletion_detected(self, seeded):
        """反例加强：删掉一行后续接断裂（prev_hash mismatch）也会被抓到。"""
        db, ids = seeded
        c = _client(db)
        c.post("/api/resolve", json={"intent_id": ids["paused"],
                                     "action": "approve", "note": "n1"}, headers=HDR)
        c.post("/api/reconcile", json={"intent_id": ids["uncertain"]}, headers=HDR)
        conn = sqlite3.connect(db)
        first = conn.execute(
            "SELECT id FROM console_audit ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM console_audit WHERE id=?", (first,))
        conn.commit()
        conn.close()
        d = c.get("/api/audit/verify", headers=HDR).json()
        assert d["valid"] is False
        assert d["broken_at"]["reason"] == "prev_hash mismatch"

    def test_legacy_db_migration_genesis(self, tmp_path):
        """迁移兼容：旧行无 hash → 以 schema_version 起点重新起链，
        记录 audit.chain.genesis 创世事件，全链 verify 通过。"""
        db = str(tmp_path / "legacy.db")
        led = EffectLedger(db)
        led.begin("task-old", "step", {"x": 1})
        led.close()
        # 手工造旧版 console_audit（无 prev_hash/row_hash 列）+ 两条存量行
        conn = sqlite3.connect(db)
        conn.executescript(OLD_AUDIT_SCHEMA)
        conn.execute("INSERT INTO console_audit(who,what,intent_id,params,at) "
                     "VALUES('console','legacy.op','eff-old','{}',100.0)")
        conn.execute("INSERT INTO console_audit(who,what,intent_id,params,at) "
                     "VALUES('console','legacy.op2',NULL,'{}',200.0)")
        conn.commit()
        conn.close()

        c = _client(db)  # create_app 触发迁移起链
        d = c.get("/api/audit/verify", headers=HDR).json()
        assert d["valid"] is True and d["checked"] == 3  # 2 旧行 + 1 创世事件
        items = c.get("/api/audit", headers=HDR).json()["items"]
        genesis = [x for x in items if x["what"] == "audit.chain.genesis"]
        assert len(genesis) == 1 and genesis[0]["who"] == "system"
        assert "schema_version" in genesis[0]["params"]
        # 幂等：再次 create_app 不重复起链、不重复创世
        c2 = _client(db)
        d2 = c2.get("/api/audit/verify", headers=HDR).json()
        assert d2["valid"] is True and d2["checked"] == 3


# ---------- gate 限流（token 失败 5 次/分，第 6 次 429） ----------

class TestRateLimit:
    def test_gate_sixth_failure_429(self, seeded):
        db, _ = seeded
        c = _client(db)
        for i in range(5):
            r = c.post("/api/token", json={"token": f"wrong-{i}"})
            assert r.status_code == 401, f"第 {i+1} 次应 401"
        r = c.post("/api/token", json={"token": "wrong-6"})
        assert r.status_code == 429
        assert r.json() == {"error": "rate-limited"}
        # 超限后连正确 token 也先被 429 挡住（不再做 token 比较）
        assert c.post("/api/token", json={"token": TOKEN}).status_code == 429

    def test_success_resets_counter(self, seeded):
        db, _ = seeded
        c = _client(db)
        for _ in range(4):
            assert c.post("/api/token", json={"token": "nope"}).status_code == 401
        assert c.post("/api/token", json={"token": TOKEN}).status_code == 200
        # 计数已清零：再来 5 次失败仍是 401，第 6 次才 429
        for i in range(5):
            assert c.post("/api/token", json={"token": "nope"}).status_code == 401
        assert c.post("/api/token", json={"token": "nope"}).status_code == 429

    def test_middleware_401_also_counted(self, seeded):
        db, _ = seeded
        c = _client(db)
        for i in range(5):
            r = c.get("/api/effects", headers={"X-SuperOps-Token": f"bad-{i}"})
            assert r.status_code == 401
        r = c.get("/api/effects", headers={"X-SuperOps-Token": "bad-6"})
        assert r.status_code == 429
        assert r.json() == {"error": "rate-limited"}


# ---------- cookie 安全位 ----------

class TestCookieSecurity:
    def test_httponly_samesite_lax(self, seeded):
        db, _ = seeded
        c = _client(db)
        r = c.post("/api/token", json={"token": TOKEN})
        assert r.status_code == 200
        sc = r.headers["set-cookie"].lower()
        assert "httponly" in sc          # 浏览器脚本拿不到 token
        assert "samesite=lax" in sc      # CSRF 缓解
        assert "max-age=2592000" in sc   # 30 天


# ---------- CLI 绑定：默认回环，--lan 放开 + stderr 警告 ----------

class TestCliBind:
    def _run_main(self, monkeypatch, tmp_path, argv):
        import uvicorn

        from superops_console import __main__ as m

        captured = {}

        def fake_run(app, host, port):
            captured["host"] = host
            captured["port"] = port

        monkeypatch.setattr(uvicorn, "run", fake_run)
        monkeypatch.setenv("SUPEROPS_TOKEN_FILE", str(tmp_path / "tok.txt"))
        monkeypatch.setattr("sys.argv", ["superops-console", "--db",
                                         str(tmp_path / "l.db"), *argv])
        m.main()
        return captured

    def test_default_loopback_no_warning(self, monkeypatch, tmp_path, capsys):
        cap = self._run_main(monkeypatch, tmp_path, [])
        assert cap["host"] == "127.0.0.1"
        assert "警告" not in capsys.readouterr().err

    def test_lan_binds_all_interfaces_with_warning(self, monkeypatch, tmp_path, capsys):
        cap = self._run_main(monkeypatch, tmp_path, ["--lan"])
        assert cap["host"] == "0.0.0.0"
        err = capsys.readouterr().err
        assert "警告" in err and "局域网" in err

    def test_explicit_host_overrides(self, monkeypatch, tmp_path, capsys):
        cap = self._run_main(monkeypatch, tmp_path, ["--host", "0.0.0.0"])
        assert cap["host"] == "0.0.0.0"
        assert "警告" in capsys.readouterr().err


# ---------- 端到端冒烟：gate 换 cookie → effects → full resolve → verify ----------

class TestE2ESmoke:
    def test_full_journey(self, seeded):
        db, ids = seeded
        c = _client(db)  # 不带任何 header，纯 cookie 旅程
        # 1) 未登录：API 401、页面 302
        assert c.get("/api/effects").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 302
        # 2) gate 换 cookie
        r = c.post("/api/token", json={"token": TOKEN})
        assert r.status_code == 200 and r.json() == {"ok": True}
        # 3) effects 列表（cookie 直通，无 header）
        d = c.get("/api/effects").json()
        assert d["count"] == 3
        states = {x["state"] for x in d["items"]}
        assert {"CONFIRMED", "UNCERTAIN", "PAUSED"} <= states
        # 4) full 模式 resolve 放行 PAUSED
        r = c.post("/api/resolve", json={"intent_id": ids["paused"],
                                         "action": "approve", "note": "e2e"})
        assert r.status_code == 200 and r.json()["action"] == "EXECUTE_ALLOWED"
        # 5) 审计链全链校验通过
        v = c.get("/api/audit/verify").json()
        assert v["valid"] is True and v["broken_at"] is None and v["checked"] >= 1
        # 审计里能看到这次裁决
        audit_items = c.get("/api/audit").json()["items"]
        assert any(x["what"] == "resolve.approve" for x in audit_items)
