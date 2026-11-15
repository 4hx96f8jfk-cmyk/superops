"""WORKPLAN M1 硬化项验收测试（1.1/1.2/1.3/1.4/1.5/1.7）。

每项对应 docs/WORKPLAN.md M1 表格行，验收标准=可实测断言。
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest
from effect_ledger import EffectLedger, LedgerStateError, make_idem_key


@pytest.fixture
def ledger(tmp_path):
    led = EffectLedger(str(tmp_path / "hard.db"))
    yield led
    led.close()


def _to_uncertain(led, task="t1", step="s1", risk="high"):
    b = led.begin(task, step, {"p": 1}, risk=risk)
    led.mark_receipt_lost(b["intent_id"], b["fencing_token"])
    return b["intent_id"]


def _to_paused(led, task="t1", step="s1"):
    """标准路径进 PAUSED：EXECUTING → 回执丢失 UNCERTAIN → 高风险探针不可用 PAUSED。"""
    b = led.begin(task, step, {"p": 1}, risk="high")
    led.mark_receipt_lost(b["intent_id"], b["fencing_token"])
    led.reconcile(b["intent_id"], lambda t, s: None)
    return b["intent_id"]


class TestWalHardening:
    """1.1 WAL + busy_timeout（文件库）。"""

    def test_wal_enabled_on_file_db(self, tmp_path):
        led = EffectLedger(str(tmp_path / "wal.db"))
        mode = led.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = led.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 5000
        led.close()

    def test_two_connections_concurrent_writes(self, tmp_path):
        """双实例（模拟双进程）并发 begin 各 50 次：0 异常、0 重复 intent。"""
        db = str(tmp_path / "dual.db")
        a = EffectLedger(db)
        b = EffectLedger(db)
        errors = []

        def hammer(led, tag):
            try:
                for i in range(50):
                    led.begin(f"task-{tag}", f"step-{i}", {"i": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=hammer, args=(a, "a"))
        t2 = threading.Thread(target=hammer, args=(b, "b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []
        n = a.conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        assert n == 100
        a.close()
        b.close()


class TestSchemaVersioning:
    """1.2 user_version 版本化 + 旧库自动迁移。"""

    def test_fresh_db_gets_version(self, tmp_path):
        led = EffectLedger(str(tmp_path / "v.db"))
        assert led.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        led.close()

    def test_legacy_db_migrated_on_open(self, tmp_path):
        """模拟旧库：裸 SQL 建表（user_version=0）+ 存量数据 → 打开后升 v1 且数据完好。"""
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE effects (intent_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL UNIQUE,"
            " task_id TEXT NOT NULL, step_id TEXT NOT NULL, params_hash TEXT NOT NULL,"
            " risk TEXT NOT NULL DEFAULT 'low', state TEXT NOT NULL,"
            " fencing_token INTEGER NOT NULL DEFAULT 1, lease_expires_at REAL NOT NULL DEFAULT 0,"
            " receipt_json TEXT, reconcile_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT NOT NULL,"
            " event TEXT NOT NULL, detail TEXT, at REAL NOT NULL);"
        )
        conn.execute(
            "INSERT INTO effects VALUES('eff-old','k|o|1','t','s','h','low','CONFIRMED',1,0,"
            "'{}',NULL,1.0,1.0)"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        conn.close()
        led = EffectLedger(db)
        assert led.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        row = led.conn.execute(
            "SELECT state FROM effects WHERE intent_id='eff-old'"
        ).fetchone()
        assert row["state"] == "CONFIRMED"  # 存量数据完好
        led.close()


class TestThreadSafety:
    """1.3 单实例多线程共享（RLock 串行化）。"""

    def test_eight_threads_mixed_ops(self, ledger):
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    b = ledger.begin(f"mt-{n}", f"s-{i}", {"i": i})
                    if b["action"] == "EXECUTE":
                        ledger.complete(b["intent_id"], b["fencing_token"], {"ok": 1})
                    ledger.pending("CONFIRMED")
                    ledger.state_of("mt-0", "s-0", {"i": 0})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        n = ledger.conn.execute(
            "SELECT COUNT(*) FROM effects WHERE state='CONFIRMED'"
        ).fetchone()[0]
        assert n == 160
        # 重放仍被阻断（并发没有破坏幂等语义）
        b = ledger.begin("mt-0", "s-0", {"i": 0})
        assert b["action"] == "ALREADY_CONFIRMED"


class TestProbeExceptionIsolation:
    """1.4 probe 抛异常 = 探针不可用（None），reconcile 不崩。"""

    def test_probe_raises_high_risk_pauses(self, ledger):
        iid = _to_uncertain(ledger, risk="high")

        def bad_probe(task_id, step_id):
            raise RuntimeError("probe endpoint down")

        r = ledger.reconcile(iid, bad_probe)
        assert r["action"] == "PAUSED"  # fail-closed 语义不变
        events = [a["event"] for a in ledger.audit_of(iid)]
        assert "PROBE_ERROR" in events
        assert "PAUSED_HIGH_RISK_UNPROBEABLE" in events

    def test_probe_raises_low_risk_stays_uncertain(self, ledger):
        iid = _to_uncertain(ledger, risk="low")

        def bad_probe(task_id, step_id):
            raise TimeoutError("probe timeout")

        r = ledger.reconcile(iid, bad_probe)
        assert r["action"] == "RECONCILE_LATER"
        assert ledger.state_of("t1", "s1", {"p": 1})["state"] == "UNCERTAIN"


class TestResolveApi:
    """1.5 PAUSED 人工裁决状态机。"""

    def test_approve_bumps_fencing_token(self, ledger):
        iid = _to_paused(ledger)
        before = ledger.conn.execute(
            "SELECT fencing_token FROM effects WHERE intent_id=?", (iid,)
        ).fetchone()["fencing_token"]
        r = ledger.resolve(iid, "approve", note="确认执行", actor="ops-lead")
        assert r["action"] == "EXECUTE"
        assert r["fencing_token"] == before + 1
        row = ledger.conn.execute(
            "SELECT state, fencing_token FROM effects WHERE intent_id=?", (iid,)
        ).fetchone()
        assert row["state"] == "EXECUTING"
        assert row["fencing_token"] == before + 1
        events = [a["event"] for a in ledger.audit_of(iid)]
        assert "RESOLVED_APPROVE" in events

    def test_reject_is_terminal_replay_forbidden(self, ledger):
        iid = _to_paused(ledger)
        r = ledger.resolve(iid, "reject", note="不执行", actor="ops-lead")
        assert r["action"] == "REJECTED"
        # 终态：同 idem_key 再 begin 永远 REJECTED，绝不 EXECUTE
        b = ledger.begin("t1", "s1", {"p": 1})
        assert b["action"] == "REJECTED"
        assert b["intent_id"] == iid

    def test_resolve_wrong_state_raises(self, ledger):
        b = ledger.begin("t2", "s1", {})  # EXECUTING
        with pytest.raises(LedgerStateError):
            ledger.resolve(b["intent_id"], "approve")

    def test_resolve_bad_action_raises(self, ledger):
        iid = _to_paused(ledger)
        with pytest.raises(ValueError):
            ledger.resolve(iid, "maybe")


class TestStructuredLogging:
    """1.7 关键审计事件进 logging。"""

    def test_replay_block_logged_info(self, ledger, caplog):
        b = ledger.begin("t1", "s1", {})
        ledger.complete(b["intent_id"], b["fencing_token"], {})
        with caplog.at_level(logging.INFO, logger="effect_ledger"):
            ledger.begin("t1", "s1", {})
        assert any(
            "REPLAY_BLOCKED_CONFIRMED" in rec.getMessage() for rec in caplog.records
        )

    def test_takeover_logged(self, tmp_path, caplog):
        led = EffectLedger(str(tmp_path / "lg.db"), lease_seconds=0)
        led.begin("t", "s", {})
        with caplog.at_level(logging.INFO, logger="effect_ledger"):
            led.begin("t", "s", {})  # lease=0 → takeover
        assert any("TAKEOVER" in (rec.getMessage()) for rec in caplog.records)
        led.close()


class TestSweepStaleExecuting:
    """恢复辅助：租期过期的 EXECUTING → UNCERTAIN（崩溃恢复标准姿势）。"""

    def test_stale_executing_marked_uncertain(self, tmp_path):
        led = EffectLedger(str(tmp_path / "st.db"), lease_seconds=0)
        led.begin("t", "s", {})
        out = led.sweep_stale_executing()
        assert len(out) == 1
        assert out[0]["action"] == "UNCERTAIN"
        assert led.pending("UNCERTAIN")[0]["task_id"] == "t"
        led.close()

    def test_live_lease_untouched(self, ledger):
        ledger.begin("t", "s", {})
        assert ledger.sweep_stale_executing() == []
        assert ledger.pending("EXECUTING")  # 活租期不动（IN_PROGRESS 保护）
