"""SUP-GUI-20261113 P1 图形操作能力测试（G3 模型接入拓扑+成本 / G4 仪表盘趋势）。

覆盖验收：
a. 三新端点（/api/providers/topology、/api/usage/cost、/api/stats/trend）
   无 token 一律 401（token 门复用反例）。
b. topology：未配置态优雅降级（configured=False + 中文引导，不报错）；
   已配置态结构断言——chain 顺序（local 优先 + weight 降序）、
   probe 健康色数据必须来自 probe_cache 真实探活（mock OpenAI 端点
   真探成功 + 死端口真探失败，禁手填假结果）。
c. cost：端点数字与直接调 modelhub cost_summary()/usage_summary() 一致。
d. trend：桶结构断言——steps 按 created_at 落桶、状态计数、审计事件计数。
"""

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from effect_ledger import EffectLedger
from fastapi.testclient import TestClient
from modelhub import ModelHub, ProviderConfig, UsageRecord

from superops_console import create_app

TOKEN = "a" * 32
HDR = {"X-SuperOps-Token": TOKEN}
NEW_ENDPOINTS = ["/api/providers/topology", "/api/usage/cost", "/api/stats/trend"]


# ---------- 夹具：mock OpenAI 端点（真实探活用） ----------


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {
                "id": "mock-p1",
                "object": "chat.completion",
                "model": "ok-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def mock_openai():
    """本地 mock OpenAI 兼容端点（探活必须打真 HTTP，不许假结果）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture
def ledger_db(tmp_path):
    """3 条 effects（CONFIRMED/UNCERTAIN/PAUSED），created_at 由用例改布。"""
    db = str(tmp_path / "ledger.db")
    led = EffectLedger(db)
    b1 = led.begin("task-ok", "s1", {})
    led.complete(b1["intent_id"], b1["fencing_token"], {})
    b2 = led.begin("task-u", "s2", {})
    led.mark_receipt_lost(b2["intent_id"], b2["fencing_token"])
    b3 = led.begin("task-p", "s3", {}, risk="high")
    led.mark_receipt_lost(b3["intent_id"], b3["fencing_token"])
    led.reconcile(b3["intent_id"], lambda t, s: None)
    led.close()
    return db


@pytest.fixture
def modelhub_db(tmp_path, mock_openai):
    """注册 3 provider（2 真探活成功 + 1 死端口探活失败）+ 灌多日 usage 流水。"""
    db = str(tmp_path / "modelhub.db")
    hub = ModelHub(db)
    hub.registry.register(
        ProviderConfig(
            name="p-local",
            base_url=mock_openai,
            model="ok-model",
            kind="local",
            weight=1.0,
            timeout=5.0,
        )
    )
    hub.registry.register(
        ProviderConfig(
            name="p-cloud-hi",
            base_url=mock_openai,
            model="ok-model",
            kind="cloud",
            weight=2.0,
            timeout=8.0,
            price_input_per_1k=0.011,
            price_output_per_1k=0.027,
        )
    )
    hub.registry.register(
        ProviderConfig(
            name="p-cloud-dead",
            base_url="http://127.0.0.1:59321/v1",  # 死端口：TCP 必拒
            model="ghost",
            kind="cloud",
            weight=0.5,
            timeout=2.0,
        )
    )
    results = hub.probe(force=True)  # 真实探活 → probe_cache 落库
    assert results["p-local"].inference_ok is True
    assert results["p-cloud-dead"].tcp_ok is False
    # 多日多 provider usage 流水（含 token 数，成本图非零）
    now = time.time()
    for day_off in range(5):
        ts = now - day_off * 86400
        hub.usage.record(
            UsageRecord(
                provider="p-cloud-hi",
                model="ok-model",
                prompt_tokens=1000 * (day_off + 1),
                completion_tokens=500,
                latency_ms=120.0,
                ts=ts,
            )
        )
        hub.usage.record(
            UsageRecord(
                provider="p-local",
                model="ok-model",
                prompt_tokens=2000,
                completion_tokens=300,
                ts=ts,
            )
        )
    hub.usage.record(
        UsageRecord(
            provider="p-cloud-dead",
            model="ghost",
            status="error",
            ts=now - 3600,
        )
    )
    hub.close()
    return db


def _client(db, mode="read-only", modelhub_db=None):
    app = create_app(db, TOKEN, mode=mode, modelhub_db=modelhub_db)
    return TestClient(app)


# ---------- a. 三端点无 token 401 反例 ----------


@pytest.mark.parametrize("path", NEW_ENDPOINTS)
def test_new_endpoints_require_token(tmp_path, path):
    db = str(tmp_path / "empty-ledger.db")
    EffectLedger(db).close()
    c = _client(db)
    r = c.get(path)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


# ---------- b. topology ----------


class TestTopologyUnconfigured:
    def test_not_configured_degrades_gracefully(self, ledger_db):
        c = _client(ledger_db)  # modelhub_db=None
        d = c.get("/api/providers/topology", headers=HDR).json()
        assert d["configured"] is False
        assert "modelhub_db" in d["hint"]  # 中文引导：如何配置
        assert d["chain"] == [] and d["providers"] == []

    def test_db_file_missing_degrades(self, ledger_db, tmp_path):
        c = _client(ledger_db, modelhub_db=str(tmp_path / "nope.db"))
        d = c.get("/api/providers/topology", headers=HDR).json()
        assert d["configured"] is False


class TestTopologyConfigured:
    @pytest.fixture
    def topo(self, ledger_db, modelhub_db):
        c = _client(ledger_db, modelhub_db=modelhub_db)
        r = c.get("/api/providers/topology", headers=HDR)
        assert r.status_code == 200
        return r.json()

    def test_chain_order_local_first_weight_desc(self, topo):
        assert topo["configured"] is True
        # local 永远在 cloud 前；cloud 内 weight 降序（hi=2.0 > dead=0.5）
        assert topo["chain"] == ["p-local", "p-cloud-hi", "p-cloud-dead"]

    def test_probe_health_from_real_probe_cache(self, topo):
        by = {p["name"]: p for p in topo["providers"]}
        assert len(topo["providers"]) == 3
        ok = by["p-local"]["probe"]
        assert ok is not None and ok["status"] == "healthy"
        assert ok["inference_ok"] is True and ok["latency_ms"] is not None
        dead = by["p-cloud-dead"]["probe"]
        assert dead["status"] == "down" and dead["tcp_ok"] is False
        assert by["p-cloud-hi"]["probe"]["status"] == "healthy"

    def test_timeout_and_price_config_exposed(self, topo):
        by = {p["name"]: p for p in topo["providers"]}
        assert by["p-local"]["timeout"] == 5.0
        assert by["p-cloud-hi"]["timeout"] == 8.0
        assert by["p-cloud-hi"]["price_input_per_1k"] == 0.011

    def test_readonly_mode_serves_topology(self, ledger_db, modelhub_db):
        # read-only 下 GET 照常（图形层零特权：全只读端点）
        c = _client(ledger_db, mode="read-only", modelhub_db=modelhub_db)
        assert c.get("/api/providers/topology", headers=HDR).status_code == 200


# ---------- c. cost ----------


class TestCost:
    def test_unconfigured_degrades(self, ledger_db):
        d = _client(ledger_db).get("/api/usage/cost", headers=HDR).json()
        assert d["configured"] is False
        assert d["items"] == [] and d["total_cost"] == 0.0

    def test_numbers_match_cost_summary(self, ledger_db, modelhub_db):
        """端点数字必须与直接调 modelhub usage_summary()（cost_summary+价格表）一致。"""
        hub = ModelHub(modelhub_db)
        expected = hub.usage_summary()
        hub.close()
        d = (
            _client(ledger_db, modelhub_db=modelhub_db)
            .get("/api/usage/cost", headers=HDR)
            .json()
        )
        assert d["configured"] is True
        got = {r["provider"]: r for r in d["items"]}
        assert len(got) == len(expected)
        for exp in expected:
            row = got[exp["provider"]]
            for k in (
                "calls",
                "ok_calls",
                "error_calls",
                "prompt_tokens",
                "completion_tokens",
                "cost",
            ):
                assert row[k] == exp[k], f"{exp['provider']}.{k}"
        # 成本非零断言（云端有价格表，本地免费）
        assert got["p-cloud-hi"]["cost"] > 0
        assert got["p-local"]["cost"] == 0
        assert d["total_cost"] == round(sum(r["cost"] for r in d["items"]), 10)

    def test_daily_detail_for_charts(self, ledger_db, modelhub_db):
        d = (
            _client(ledger_db, modelhub_db=modelhub_db)
            .get("/api/usage/cost", headers=HDR)
            .json()
        )
        assert d["daily"], "按日明细必须非空供画图"
        row = d["daily"][0]
        assert {"day", "provider", "calls", "prompt_tokens"} <= set(row)
        days = {r["day"] for r in d["daily"]}
        assert len(days) >= 2  # 多日流水


# ---------- d. trend ----------


class TestTrend:
    def test_structure(self, ledger_db):
        d = _client(ledger_db).get("/api/stats/trend?bucket=3600&limit=6", headers=HDR)
        assert d.status_code == 200
        body = d.json()
        assert body["bucket_seconds"] == 3600
        assert len(body["buckets"]) == 6
        b = body["buckets"][-1]
        assert {"start", "end", "steps_total", "steps_by_state", "audit_events"} <= set(
            b
        )
        assert b["end"] - b["start"] == 3600

    def test_counts_match_ledger(self, ledger_db):
        """把 created_at 手动布到两个桶，断言分桶计数与账本一致。

        时间钉在桶中心（非墙钟 now）：墙钟落在桶边界后 200s 内时，
        now-200 会跌进上一桶造成确定性假红（2026-11-16 20:02 实测踩中）。
        """
        now = time.time() // 3600 * 3600 + 1800
        c = _client(ledger_db)  # 先建 app：console_audit 表由 create_app 初始化
        conn = sqlite3.connect(ledger_db)
        rows = conn.execute(
            "SELECT intent_id FROM effects ORDER BY created_at"
        ).fetchall()
        # 前 2 条 → 当前桶；第 3 条 → 上一桶（bucket=3600）
        conn.execute(
            "UPDATE effects SET created_at=? WHERE intent_id=?",
            (now - 100, rows[0][0]),
        )
        conn.execute(
            "UPDATE effects SET created_at=? WHERE intent_id=?",
            (now - 200, rows[1][0]),
        )
        conn.execute(
            "UPDATE effects SET created_at=? WHERE intent_id=?",
            (now - 3600 - 100, rows[2][0]),
        )
        # 审计事件 2 条落当前桶
        conn.execute(
            "INSERT INTO console_audit(who,what,intent_id,params,at) VALUES(?,?,?,?,?)",
            ("test", "x", None, None, now),
        )
        conn.execute(
            "INSERT INTO console_audit(who,what,intent_id,params,at) VALUES(?,?,?,?,?)",
            ("test", "y", None, None, now),
        )
        conn.commit()
        conn.close()
        d = c.get("/api/stats/trend?bucket=3600&limit=6", headers=HDR)
        body = d.json()
        cur, prev = body["buckets"][-1], body["buckets"][-2]
        assert cur["steps_total"] == 2
        assert prev["steps_total"] == 1
        assert sum(cur["steps_by_state"].values()) == 2
        # 审计期望值直查库（create_app 起链可能补 audit.chain.genesis 行，
        # 端点口径 = console_audit 真实行数，不硬编码）
        expect_audit = (
            sqlite3.connect(ledger_db)
            .execute(
                "SELECT COUNT(*) FROM console_audit WHERE at>=?",
                (body["buckets"][-1]["start"],),
            )
            .fetchone()[0]
        )
        assert cur["audit_events"] == expect_audit >= 2
        assert body["total_effects"] == 3

    def test_params_clamped(self, ledger_db):
        d = (
            _client(ledger_db)
            .get("/api/stats/trend?bucket=1&limit=99999", headers=HDR)
            .json()
        )
        assert d["bucket_seconds"] == 60  # 下限钳制
        assert len(d["buckets"]) == 200  # 上限钳制
