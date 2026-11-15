"""审计修复验收测试（P1-1 / P1-2 / P2-1 / P2-2 / P2-3 / P2-4）。

并发测试双实例（两个 EffectLedger 共享同一文件库，模拟双进程）+
threading.Barrier 同步起跑；「慢腿」通过给实例 _get 注入延时把
SELECT→写 竞态窗口拉宽到确定性复现，断链路径与真实竞态一致。
"""

from __future__ import annotations

import threading
import time

import pytest
from effect_ledger import (
    EffectLedger,
    LedgerStateError,
    LedgerWriteError,
)

LEGAL_BEGIN_ACTIONS = {
    "EXECUTE",
    "IN_PROGRESS",
    "ALREADY_CONFIRMED",
    "RECONCILE_REQUIRED",
    "PAUSED",
    "REJECTED",
}


def _slow_get(led: EffectLedger, delay: float = 0.04):
    """把该实例 _get 返回后的时刻推迟 delay 秒——拉宽 SELECT→写 竞态窗口。"""
    orig = led._get

    def slow_get(**kw):
        r = orig(**kw)
        time.sleep(delay)
        return r

    led._get = slow_get


def _race_two(fn_a, fn_b):
    """两线程 barrier 同步起跑，返回 (结果a, 结果b, 异常列表)。"""
    barrier = threading.Barrier(2)
    out, errors = {}, []

    def run(tag, fn):
        try:
            barrier.wait(timeout=10)
            out[tag] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append((tag, exc))

    ta = threading.Thread(target=run, args=("a", fn_a))
    tb = threading.Thread(target=run, args=("b", fn_b))
    ta.start()
    tb.start()
    ta.join(timeout=30)
    tb.join(timeout=30)
    return out.get("a"), out.get("b"), errors


class TestP1_1BeginSameKeyRace:
    """P1-1：begin 同 key 跨进程竞态不得裸露 sqlite3.IntegrityError。"""

    def test_barrier_concurrent_begin_same_key_zero_error(self, tmp_path):
        """双实例 barrier 并发 begin 同 key ×40 轮：0 异常、两返回值均合法、0 重复行。"""
        db = str(tmp_path / "p11.db")
        a = EffectLedger(db)
        b = EffectLedger(db)
        try:
            for i in range(40):
                ra, rb, errors = _race_two(
                    lambda i=i: a.begin("race", f"s{i}", {"i": i}),
                    lambda i=i: b.begin("race", f"s{i}", {"i": i}),
                )
                assert errors == [], f"round {i}: {errors}"
                assert ra["action"] in LEGAL_BEGIN_ACTIONS
                assert rb["action"] in LEGAL_BEGIN_ACTIONS
                assert ra["intent_id"] == rb["intent_id"]
            n = a.conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
            assert n == 40  # 0 重复：每 key 恰一行
        finally:
            a.close()
            b.close()

    def test_deterministic_insert_conflict_replay_blocked(self, tmp_path):
        """确定性复现：慢腿 SELECT 后对方已提交同 key → 慢腿 INSERT 撞 UNIQUE，
        必须被捕获并重派发为 IN_PROGRESS（绝不抛 sqlite3 原生异常）。"""
        db = str(tmp_path / "p11d.db")
        fast = EffectLedger(db)
        slow = EffectLedger(db)
        _slow_get(slow)
        try:
            rf, rs, errors = _race_two(
                lambda: fast.begin("t", "s", {"p": 1}),
                lambda: slow.begin("t", "s", {"p": 1}),
            )
            assert errors == []
            assert rf["action"] == "EXECUTE" and rf["fencing_token"] == 1
            assert rs["action"] == "IN_PROGRESS"
            assert rf["intent_id"] == rs["intent_id"]
            assert fast.conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
        finally:
            fast.close()
            slow.close()


class TestP1_2FencingCAS:
    """P1-2：接管/重试放行 UPDATE 必须带 fencing_token 旧值守卫（CAS）。"""

    def _expired_ledgers(self, tmp_path, name):
        now = [1000.0]
        a = EffectLedger(str(tmp_path / name), clock=lambda: now[0], lease_seconds=60)
        b = EffectLedger(str(tmp_path / name), clock=lambda: now[0], lease_seconds=60)
        return a, b, now

    def test_concurrent_takeover_exactly_one_winner(self, tmp_path):
        """租期过期后双实例 barrier 并发 begin：双方均先对账，
        DB token 恰 +1（无盲重放，也无重复接管）。"""
        a, b, now = self._expired_ledgers(tmp_path, "p12t.db")
        try:
            r0 = a.begin("t", "s", {})
            assert r0["fencing_token"] == 1
            now[0] = 2000.0  # 租期过期
            for i in range(20):
                a.begin("t", f"x{i}", {})  # 占位避免同 key 干扰
                a.conn.execute(
                    "UPDATE effects SET lease_expires_at=0 WHERE step_id=?", (f"x{i}",)
                )
                a.conn.commit()
                ra, rb, errors = _race_two(
                    lambda i=i: a.begin("t", f"x{i}", {}),
                    lambda i=i: b.begin("t", f"x{i}", {}),
                )
                assert errors == []
                actions = sorted([ra["action"], rb["action"]])
                assert actions == ["RECONCILE_REQUIRED", "RECONCILE_REQUIRED"], f"round {i}: {ra} {rb}"
                assert sum(result.get("fencing_token") == 2 for result in (ra, rb)) == 1
                tok = a.conn.execute(
                    "SELECT fencing_token FROM effects WHERE step_id=?", (f"x{i}",)
                ).fetchone()[0]
                assert tok == 2  # 只 bump 一次
        finally:
            a.close()
            b.close()

    def test_deterministic_takeover_cas_loser_redispatches(self, tmp_path):
        """确定性：慢腿读到过期租期后对方已接管（token 1→2），慢腿 CAS 失败
        重读重派发为 RECONCILE_REQUIRED，不得返回 EXECUTE 造成盲重放。"""
        now = [1000.0]
        db = str(tmp_path / "p12d.db")
        fast = EffectLedger(db, clock=lambda: now[0], lease_seconds=60)
        slow = EffectLedger(db, clock=lambda: now[0], lease_seconds=60)
        try:
            fast.begin("t", "s", {})
            now[0] = 2000.0
            _slow_get(slow)
            rf, rs, errors = _race_two(
                lambda: fast.begin("t", "s", {}),
                lambda: slow.begin("t", "s", {}),
            )
            assert errors == []
            assert rf["action"] == "RECONCILE_REQUIRED" and rf["fencing_token"] == 2
            assert rs["action"] == "RECONCILE_REQUIRED"
            tok = fast.conn.execute(
                "SELECT fencing_token FROM effects WHERE intent_id=?",
                (rf["intent_id"],),
            ).fetchone()[0]
            assert tok == 2
        finally:
            fast.close()
            slow.close()

    def test_concurrent_reconcile_retry_exactly_one(self, tmp_path):
        """UNCERTAIN 双实例并发 reconcile（探针判无效果）：恰一方拿到
        EXECUTE+新 token，另一方 IN_PROGRESS；DB token 恰 +1。"""
        a, b, _ = self._expired_ledgers(tmp_path, "p12r.db")
        try:
            b0 = a.begin("t", "s", {})
            a.mark_receipt_lost(b0["intent_id"], b0["fencing_token"])
            probe_absent = lambda t, s: False
            ra, rb, errors = _race_two(
                lambda: a.reconcile(b0["intent_id"], probe_absent),
                lambda: b.reconcile(b0["intent_id"], probe_absent),
            )
            assert errors == []
            actions = sorted([ra["action"], rb["action"]])
            assert actions == ["EXECUTE", "IN_PROGRESS"], f"{ra} {rb}"
            winner = ra if ra["action"] == "EXECUTE" else rb
            assert winner["fencing_token"] == 2
            row = a.conn.execute(
                "SELECT state, fencing_token FROM effects WHERE intent_id=?",
                (b0["intent_id"],),
            ).fetchone()
            assert (row["state"], row["fencing_token"]) == ("EXECUTING", 2)
        finally:
            a.close()
            b.close()

    def test_deterministic_reconcile_cas_loser_redispatches(self, tmp_path):
        """确定性：慢腿读到 UNCERTAIN 后对方已放行重试，慢腿 CAS
        （AND state='UNCERTAIN'）失败 → 重读重派发 IN_PROGRESS。"""
        now = [1000.0]
        db = str(tmp_path / "p12rd.db")
        fast = EffectLedger(db, clock=lambda: now[0], lease_seconds=60)
        slow = EffectLedger(db, clock=lambda: now[0], lease_seconds=60)
        try:
            b0 = fast.begin("t", "s", {})
            fast.mark_receipt_lost(b0["intent_id"], b0["fencing_token"])
            _slow_get(slow)
            probe_absent = lambda t, s: False
            rf, rs, errors = _race_two(
                lambda: fast.reconcile(b0["intent_id"], probe_absent),
                lambda: slow.reconcile(b0["intent_id"], probe_absent),
            )
            assert errors == []
            assert rf["action"] == "EXECUTE" and rf["fencing_token"] == 2
            assert rs["action"] == "IN_PROGRESS"
        finally:
            fast.close()
            slow.close()


class TestP2ErrorSemantics:
    """P2-1 / P2-2：错误类型口径。"""

    def test_mark_receipt_lost_unknown_intent_raises_write_error(self, tmp_path):
        """P2-1：不存在的 intent_id → LedgerWriteError（与 complete 口径一致），
        不再裸露 TypeError。"""
        led = EffectLedger(str(tmp_path / "p21.db"))
        try:
            with pytest.raises(LedgerWriteError):
                led.mark_receipt_lost("eff-does-not-exist", 1)
        finally:
            led.close()

    def test_reconcile_wrong_state_raises_state_error(self, tmp_path):
        """P2-2：对 EXECUTING 行 reconcile → LedgerStateError（原 AssertionError）。"""
        led = EffectLedger(str(tmp_path / "p22.db"))
        try:
            b = led.begin("t", "s", {})
            with pytest.raises(LedgerStateError):
                led.reconcile(b["intent_id"], lambda t, s: None)
        finally:
            led.close()

    def test_ledger_state_error_is_fail_closed_subclass(self, tmp_path):
        """LedgerStateError 设计为 LedgerWriteError 子类：沿用 fail-closed
        契约，既有 except LedgerWriteError 调用方零改动。"""
        assert issubclass(LedgerStateError, LedgerWriteError)
        led = EffectLedger(str(tmp_path / "p22b.db"))
        try:
            b = led.begin("t", "s", {})
            with pytest.raises(LedgerWriteError):  # 旧口径仍捕获
                led.reconcile(b["intent_id"], lambda t, s: None)
        finally:
            led.close()


class TestP2_3AuditAtomicity:
    """P2-3：状态迁移与审计事件必须同一 commit（崩溃原子性）。"""

    def test_crash_between_state_write_and_audit_leaves_nothing(self, tmp_path, monkeypatch):
        """模拟「状态 UPDATE 已执行、审计前崩溃」：新实现中 UPDATE 未 commit，
        回滚后状态不变——旧实现此处会留下已 CONFIRMED 但审计缺行的半提交。"""
        led = EffectLedger(str(tmp_path / "p23.db"))
        try:
            b = led.begin("t", "s", {})
            iid = b["intent_id"]

            def crash_audit(*args, **kwargs):
                raise RuntimeError("simulated crash after state write, before commit")

            monkeypatch.setattr(led, "_audit", crash_audit)
            with pytest.raises(RuntimeError):
                led.complete(iid, b["fencing_token"], {"x": 1})
            led.conn.rollback()
            row = led.conn.execute(
                "SELECT state FROM effects WHERE intent_id=?", (iid,)
            ).fetchone()
            assert row["state"] == "EXECUTING"  # 状态迁移随事务一起被丢弃
            events = [a["event"] for a in led.audit_of(iid)]
            assert "CONFIRMED" not in events  # 审计与状态同生共死
        finally:
            led.close()

    def test_begin_insert_and_audit_atomic(self, tmp_path, monkeypatch):
        """begin 路径同样原子：INSERT 后审计前崩溃 → 回滚后行不存在（fail-closed）。"""
        led = EffectLedger(str(tmp_path / "p23b.db"))
        try:
            def crash_audit(*args, **kwargs):
                raise RuntimeError("simulated crash before commit")

            monkeypatch.setattr(led, "_audit", crash_audit)
            with pytest.raises(RuntimeError):
                led.begin("t", "s", {})
            led.conn.rollback()
            assert led.state_of("t", "s", {}) is None
        finally:
            led.close()

    def test_happy_path_state_and_audit_both_present(self, tmp_path):
        """正向确认：单 commit 后状态与审计事件同时可见。"""
        led = EffectLedger(str(tmp_path / "p23c.db"))
        try:
            b = led.begin("t", "s", {})
            led.complete(b["intent_id"], b["fencing_token"], {"x": 1})
            assert led.state_of("t", "s", {})["state"] == "CONFIRMED"
            events = [a["event"] for a in led.audit_of(b["intent_id"])]
            assert "INTENT_PERSISTED" in events and "CONFIRMED" in events
        finally:
            led.close()


class TestP2_4Sanitization:
    """P2-4：内部代号脱敏。"""

    def test_module_docstring_has_no_internal_codename(self):
        import effect_ledger.ledger as m

        doc = m.__doc__ or ""
        assert "Avatar" not in doc
        assert "fengguo" not in doc.lower()
