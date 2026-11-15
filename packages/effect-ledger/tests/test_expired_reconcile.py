"""Expired execution leases must reconcile before any external retry."""
from pathlib import Path
import subprocess
import sys

import pytest

from effect_ledger import EffectLedger


def test_expired_begin_requires_reconcile_and_fences_old_writer(tmp_path):
    now = [1000.0]
    ledger = EffectLedger(str(tmp_path / "effects.db"), clock=lambda: now[0], lease_seconds=60)
    first = ledger.begin("task", "send", {})
    now[0] += 61
    resumed = ledger.begin("task", "send", {})
    assert resumed["action"] == "RECONCILE_REQUIRED"
    assert resumed["fencing_token"] == 2
    assert ledger.complete(first["intent_id"], 1, {})["action"] == "REJECTED_STALE"
    assert ledger.mark_receipt_lost(first["intent_id"], 1)["action"] == "REJECTED_STALE"
    assert ledger.state_of("task", "send", {})["state"] == "UNCERTAIN"
    retry = ledger.reconcile(first["intent_id"], lambda *_: False)
    assert retry["action"] == "EXECUTE"
    assert retry["fencing_token"] == 3
    assert ledger.complete(first["intent_id"], retry["fencing_token"], {})["action"] == "CONFIRMED"
    ledger.close()


def test_expired_unknown_high_risk_pauses(tmp_path):
    now = [1000.0]
    ledger = EffectLedger(str(tmp_path / "unknown.db"), clock=lambda: now[0], lease_seconds=60)
    ledger.begin("task", "send", {}, risk="high")
    now[0] += 61
    called = []
    result = ledger.guard("task", "send", {}, execute=lambda: called.append(1), probe=lambda *_: None, risk="high")
    assert result["action"] == "PAUSED"
    assert called == []
    ledger.close()


def test_killed_after_external_success_guard_does_not_repeat(tmp_path):
    import effect_ledger
    package = str(Path(effect_ledger.__file__).parent.parent)
    db = str(tmp_path / "crash.db")
    world = tmp_path / "external-count.txt"
    script = '''import os,sys
sys.path.insert(0,sys.argv[1])
from effect_ledger import EffectLedger
ledger=EffectLedger(sys.argv[2],clock=lambda:1000.0,lease_seconds=60)
ledger.begin("crash", "send", {})
with open(sys.argv[3],"w") as stream: stream.write("1")
os._exit(9)
'''
    crashed = subprocess.run([sys.executable, "-c", script, package, db, str(world)], capture_output=True, text=True)
    assert crashed.returncode == 9
    ledger = EffectLedger(db, clock=lambda: 1061.0, lease_seconds=60)
    def external_retry():
        world.write_text(str(int(world.read_text()) + 1))
        return {"ok": True}
    result = ledger.guard("crash", "send", {}, execute=external_retry, probe=lambda *_: world.read_text() == "1")
    assert world.read_text() == "1"
    assert result["action"] == "CONFIRMED"
    ledger.close()


@pytest.mark.parametrize("method", ["complete", "mark_receipt_lost"])
def test_writer_read_before_takeover_cannot_overwrite_new_token(tmp_path, method):
    now = [1000.0]
    path = str(tmp_path / f"stale-{method}.db")
    stale = EffectLedger(path, clock=lambda: now[0], lease_seconds=60)
    recoverer = EffectLedger(path, clock=lambda: now[0], lease_seconds=60)
    first = stale.begin("task", "send", {})
    now[0] += 61
    write = stale._write
    intercepted = []
    def write_after_takeover(sql, args):
        if not intercepted and sql.startswith("UPDATE effects SET state="):
            intercepted.append(True)
            assert recoverer.begin("task", "send", {})["action"] == "RECONCILE_REQUIRED"
            assert recoverer.reconcile(first["intent_id"], lambda *_: False)["action"] == "EXECUTE"
        return write(sql, args)
    stale._write = write_after_takeover
    result = stale.complete(first["intent_id"], 1, {}) if method == "complete" else stale.mark_receipt_lost(first["intent_id"], 1)
    assert result["action"] == "REJECTED_STALE"
    row = recoverer.state_of("task", "send", {})
    assert (row["state"], row["fencing_token"]) == ("EXECUTING", 3)
    stale.close()
    recoverer.close()
