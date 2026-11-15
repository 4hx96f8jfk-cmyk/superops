"""effect-ledger test suite — mechanism guarantees as executable contracts.

Every test maps to a production counter-example class:
replay blocking / fencing / lease takeover / UNCERTAIN reconcile /
fail-closed writes / high-risk human escalation / guard() end-to-end.
"""

import os
import sqlite3
import tempfile
import unittest

from effect_ledger import EffectLedger, LedgerWriteError, make_idem_key


class FakeExternal:
    """Deliberately NON-idempotent external system: every execute() really
    happens once. Fault injection via ``mode``."""

    def __init__(self):
        self.effects = {}
        self.mode: str | None = None

    def _key(self, task_id, step_id):
        return f"{task_id}/{step_id}"

    def execute(self, task_id, step_id):
        k = self._key(task_id, step_id)
        self.effects[k] = self.effects.get(k, 0) + 1
        if self.mode == "lose_receipt":
            raise TimeoutError("injected: external succeeded, receipt lost")

    def probe(self, task_id, step_id):
        if self.mode == "probe_down":
            return None
        return self.effects.get(self._key(task_id, step_id), 0) > 0

    def count(self, task_id, step_id):
        return self.effects.get(self._key(task_id, step_id), 0)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.now = 1000.0
        self.ledger = EffectLedger(self.db, clock=lambda: self.now, lease_seconds=60)
        self.ext = FakeExternal()

    def tearDown(self):
        self.ledger.close()
        os.unlink(self.db)

    # ---------- idempotency ----------
    def test_idem_key_stable_and_param_sensitive(self):
        k1 = make_idem_key("t", "s", {"a": 1, "b": 2})
        k2 = make_idem_key("t", "s", {"b": 2, "a": 1})
        k3 = make_idem_key("t", "s", {"a": 2})
        self.assertEqual(k1, k2)  # dict order irrelevant
        self.assertNotEqual(k1, k3)

    def test_happy_path_execute_confirm(self):
        b = self.ledger.begin("t1", "send_email", {"to": "a@b.c"})
        self.assertEqual(b["action"], "EXECUTE")
        self.ext.execute("t1", "send_email")
        c = self.ledger.complete(b["intent_id"], b["fencing_token"], {"msg_id": "m1"})
        self.assertEqual(c["action"], "CONFIRMED")

    def test_replay_blocked_after_confirmed(self):
        b = self.ledger.begin("t1", "charge", {"amt": 10})
        self.ext.execute("t1", "charge")
        self.ledger.complete(b["intent_id"], b["fencing_token"], {})
        b2 = self.ledger.begin("t1", "charge", {"amt": 10})
        self.assertEqual(b2["action"], "ALREADY_CONFIRMED")
        self.assertEqual(self.ext.count("t1", "charge"), 1)  # zero replay

    # ---------- concurrency / fencing ----------
    def test_in_progress_lease_blocks_second_executor(self):
        self.ledger.begin("t1", "s", {})
        b2 = self.ledger.begin("t1", "s", {})
        self.assertEqual(b2["action"], "IN_PROGRESS")

    def test_lease_expiry_takeover_invalidates_old_token(self):
        b = self.ledger.begin("t1", "s", {})
        self.now += 61  # lease expired
        b2 = self.ledger.begin("t1", "s", {})
        self.assertEqual(b2["action"], "RECONCILE_REQUIRED")
        self.assertEqual(b2["fencing_token"], 2)
        # old executor's receipt must be rejected
        c = self.ledger.complete(b["intent_id"], 1, {"late": True})
        self.assertEqual(c["action"], "REJECTED_STALE")
        # Execution may resume only after a probe confirms no external effect.
        retry = self.ledger.reconcile(b2["intent_id"], self.ext.probe)
        self.assertEqual(retry["action"], "EXECUTE")
        self.assertEqual(retry["fencing_token"], 3)
        c2 = self.ledger.complete(b2["intent_id"], retry["fencing_token"], {"ok": True})
        self.assertEqual(c2["action"], "CONFIRMED")

    # ---------- UNCERTAIN ----------
    def test_receipt_lost_forces_reconcile_not_replay(self):
        b = self.ledger.begin("t1", "charge", {"amt": 5})
        self.ext.mode = "lose_receipt"
        with self.assertRaises(TimeoutError):
            self.ext.execute("t1", "charge")  # happened, receipt lost
        self.ledger.mark_receipt_lost(b["intent_id"], b["fencing_token"])
        b2 = self.ledger.begin("t1", "charge", {"amt": 5})
        self.assertEqual(b2["action"], "RECONCILE_REQUIRED")

    def test_reconcile_confirmed_zero_replay(self):
        b = self.ledger.begin("t1", "charge", {"amt": 5})
        self.ledger.mark_receipt_lost(b["intent_id"], b["fencing_token"])
        r = self.ledger.reconcile(b["intent_id"], self.ext.probe)
        # probe says no effect -> retry allowed with new token
        self.assertEqual(r["action"], "EXECUTE")
        self.assertEqual(r["fencing_token"], 2)
        # now simulate effect exists
        self.ext.execute("t1", "charge")
        b3 = self.ledger.begin("t1", "charge", {"amt": 5})
        self.assertEqual(b3["action"], "IN_PROGRESS")  # fresh lease from reconcile
        self.now += 61
        self.ledger.begin(
            "t1", "charge", {"amt": 5}
        )  # takeover -> UNCERTAIN path below
        # direct reconcile on a confirmed-effect case
        self.ledger._write(
            "UPDATE effects SET state='UNCERTAIN' WHERE intent_id=?", (b["intent_id"],)
        )
        r2 = self.ledger.reconcile(b["intent_id"], self.ext.probe)
        self.assertEqual(r2["action"], "CONFIRMED")
        self.assertEqual(self.ext.count("t1", "charge"), 1)

    def test_high_risk_probe_down_pauses_to_human(self):
        b = self.ledger.begin("t1", "wire", {"amt": 999}, risk="high")
        self.ledger.mark_receipt_lost(b["intent_id"], b["fencing_token"])
        self.ext.mode = "probe_down"
        r = self.ledger.reconcile(b["intent_id"], self.ext.probe)
        self.assertEqual(r["action"], "PAUSED")
        b2 = self.ledger.begin("t1", "wire", {"amt": 999})
        self.assertEqual(b2["action"], "PAUSED")  # stays paused until human

    def test_low_risk_probe_down_stays_uncertain(self):
        b = self.ledger.begin("t1", "notify", {})
        self.ledger.mark_receipt_lost(b["intent_id"], b["fencing_token"])
        self.ext.mode = "probe_down"
        r = self.ledger.reconcile(b["intent_id"], self.ext.probe)
        self.assertEqual(r["action"], "RECONCILE_LATER")
        self.assertEqual(len(self.ledger.pending("UNCERTAIN")), 1)

    # ---------- fail-closed ----------
    def test_begin_fail_closed_on_write_error(self):
        self.ledger._fail_next_write = True
        with self.assertRaises(LedgerWriteError):
            self.ledger.begin("t1", "s", {})
        # nothing persisted -> external world untouched is caller's contract
        self.assertIsNone(self.ledger.state_of("t1", "s", {}))

    def test_complete_unpersisted_intent_fail_closed(self):
        with self.assertRaises(LedgerWriteError):
            self.ledger.complete("eff-nonexistent", 1, {})

    # ---------- guard() ----------
    def test_guard_end_to_end_once_only(self):
        calls = {"n": 0}

        def execute():
            calls["n"] += 1
            self.ext.execute(
                "t9",
                "ship",
            )
            return {"order": "o1"}

        r1 = self.ledger.guard("t9", "ship", {"sku": "A"}, execute, self.ext.probe)
        self.assertEqual(r1["action"], "CONFIRMED")
        r2 = self.ledger.guard("t9", "ship", {"sku": "A"}, execute, self.ext.probe)
        self.assertEqual(r2["action"], "ALREADY_CONFIRMED")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.ext.count("t9", "ship"), 1)

    def test_guard_crash_then_reconcile_confirms(self):
        # first attempt: external succeeds but receipt lost (crash after op)
        def flaky():
            self.ext.mode = "lose_receipt"
            try:
                self.ext.execute("t9", "email")
            finally:
                self.ext.mode = None
            return {}

        r = self.ledger.guard("t9", "email", {"n": 1}, flaky, self.ext.probe)
        # reconcile probe sees the effect -> closed CONFIRMED, no retry
        self.assertEqual(r["action"], "CONFIRMED")
        self.assertEqual(self.ext.count("t9", "email"), 1)
        # second guard call must not re-execute
        r2 = self.ledger.guard("t9", "email", {"n": 1}, lambda: 1 / 0, self.ext.probe)
        self.assertEqual(r2["action"], "ALREADY_CONFIRMED")

    def test_guard_crash_absent_effect_retry_allowed(self):
        def never_landed():
            raise ConnectionError("external never received the op")

        r = self.ledger.guard("t10", "email", {}, never_landed, self.ext.probe)
        self.assertEqual(r["action"], "RETRY_ALLOWED")
        self.assertEqual(self.ext.count("t10", "email"), 0)

    # ---------- sweeper ----------
    def test_sweep_pending_batch(self):
        for i in range(3):
            b = self.ledger.begin(f"t{i}", "s", {})
            self.ledger.mark_receipt_lost(b["intent_id"], b["fencing_token"])
        results = self.ledger.sweep_pending(self.ext.probe)
        self.assertEqual(len(results), 3)
        self.assertTrue(
            all(r["action"] == "EXECUTE" for r in results)
        )  # no effects -> retry
        self.assertEqual(self.ledger.pending("UNCERTAIN"), [])

    # ---------- audit ----------
    def test_audit_trail_records_key_events(self):
        b = self.ledger.begin("t1", "s", {})
        self.ledger.complete(b["intent_id"], b["fencing_token"], {"x": 1})
        events = [a["event"] for a in self.ledger.audit_of(b["intent_id"])]
        self.assertIn("INTENT_PERSISTED", events)
        self.assertIn("CONFIRMED", events)

    def test_replay_block_is_audited(self):
        b = self.ledger.begin("t1", "s", {})
        self.ledger.complete(b["intent_id"], b["fencing_token"], {})
        self.ledger.begin("t1", "s", {})
        events = [a["event"] for a in self.ledger.audit_of(b["intent_id"])]
        self.assertIn("REPLAY_BLOCKED_CONFIRMED", events)

    # ---------- disabled mode (counter-example) ----------
    def test_disabled_ledger_produces_replay(self):
        off = EffectLedger(":memory:", enabled=False)
        b1 = off.begin("t", "s", {})
        off.complete(b1["intent_id"], b1["fencing_token"], {})
        b2 = off.begin("t", "s", {})
        self.assertEqual(b2["action"], "EXECUTE")  # unprotected — by design
        off.close()

    def test_schema_persistence_across_reopen(self):
        b = self.ledger.begin("t1", "s", {})
        self.ledger.complete(b["intent_id"], b["fencing_token"], {})
        self.ledger.close()
        again = EffectLedger(self.db, clock=lambda: self.now)
        self.assertEqual(again.state_of("t1", "s", {})["state"], "CONFIRMED")
        again.close()
        self.ledger = EffectLedger(self.db, clock=lambda: self.now)  # tearDown reuse


if __name__ == "__main__":
    unittest.main()
