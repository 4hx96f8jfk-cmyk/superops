"""1.6 崩溃恢复 e2e：子进程 exit 9 模拟硬崩溃（对齐内部 §8 场景）。

场景：执行器 begin → 外部副作用已发生 → 进程在写回执前被杀（exit 9）。
恢复：租期过期 → sweep_stale_executing 转 UNCERTAIN → sweep_pending 探针对账
→ CONFIRMED，全程世界副作用计数恒为 1（零重放）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from effect_ledger import EffectLedger

CRASH_SCRIPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
from effect_ledger import EffectLedger

led = EffectLedger(sys.argv[2], lease_seconds=0.2)
b = led.begin("crash-task", "charge", {"amount": 100}, risk="high")
# 外部副作用：真实世界扣了钱（写 world 文件模拟）
with open(sys.argv[3], "w") as f:
    f.write("CHARGED-100")
# 崩溃：回执永远没写出去
os._exit(9)
"""


def test_crash_recovery_zero_replay(tmp_path):
    db = str(tmp_path / "crash.db")
    world = str(tmp_path / "world.txt")
    import effect_ledger as _el

    pkg_parent = os.path.dirname(os.path.dirname(_el.__file__))
    # phase 1: 子进程崩溃（副作用已发生，回执丢失）
    r = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, pkg_parent, db, world],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 9
    assert open(world).read() == "CHARGED-100"

    # 崩溃现场：EXECUTING 悬挂（进程死了没人写回执）
    led = EffectLedger(db, lease_seconds=0.2)
    pend_exec = led.pending("EXECUTING")
    assert len(pend_exec) == 1

    # 租期内：重放被 IN_PROGRESS 挡住（并发保护）
    b = led.begin("crash-task", "charge", {"amount": 100})
    assert b["action"] == "IN_PROGRESS"

    # 等租期过期 → 标准恢复：stale EXECUTING → UNCERTAIN → 探针对账
    time.sleep(0.3)
    swept = led.sweep_stale_executing()
    assert swept and swept[0]["action"] == "UNCERTAIN"

    def probe(task_id, step_id):
        # 探针查外部世界真相：钱确实扣了
        return os.path.exists(world) and open(world).read() == "CHARGED-100"

    results = led.sweep_pending(probe)
    assert len(results) == 1
    assert results[0]["action"] == "CONFIRMED"

    # 关键断言：世界副作用计数 == 1（恢复全程没有重复扣款）
    assert open(world).read() == "CHARGED-100"

    # 再跑一次 guard：已 CONFIRMED，重放阻断
    calls = []
    g = led.guard(
        "crash-task",
        "charge",
        {"amount": 100},
        execute=lambda: calls.append(1) or {"ok": 1},
        probe=probe,
        risk="high",
    )
    assert g["action"] == "ALREADY_CONFIRMED"
    assert calls == []  # execute 根本没被调用
    led.close()
