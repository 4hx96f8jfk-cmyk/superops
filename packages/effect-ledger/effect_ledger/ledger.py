"""Core EffectLedger implementation (stdlib + SQLite only).

Mechanism semantics are production-proven (production fleet dogfooding, 2026-11):
do not weaken replay blocking, fencing, or fail-closed behavior.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("effect_ledger")

SCHEMA_VERSION = 1


def _sync(fn):
    """公开方法统一加实例锁（WORKPLAN 1.3）。
    RLock 可重入：guard() 内部会再进 begin/complete/reconcile。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
  intent_id       TEXT PRIMARY KEY,
  idem_key        TEXT NOT NULL UNIQUE,
  task_id         TEXT NOT NULL,
  step_id         TEXT NOT NULL,
  params_hash     TEXT NOT NULL,
  risk            TEXT NOT NULL DEFAULT 'low',
  state           TEXT NOT NULL,            -- EXECUTING / CONFIRMED / UNCERTAIN / PAUSED / REJECTED
  fencing_token   INTEGER NOT NULL DEFAULT 1,
  lease_expires_at REAL NOT NULL DEFAULT 0,
  receipt_json    TEXT,
  reconcile_json  TEXT,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effects_state ON effects(state);
"""


def make_idem_key(task_id: str, step_id: str, params: dict) -> str:
    """Idempotency key = task + step + canonical param hash."""
    ph = hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return f"{task_id}|{step_id}|{ph[:16]}"


class LedgerWriteError(RuntimeError):
    """Raised when a ledger write fails. Callers MUST NOT proceed to touch
    the external world after this — that is the fail-closed contract."""


class LedgerStateError(LedgerWriteError):
    """业务状态错误：意图存在但当前状态不允许该操作（如对非 UNCERTAIN
    行 reconcile、对非 PAUSED 行 resolve）。设计为 LedgerWriteError 的
    子类：沿用「ledger 拒绝 → 调用方禁止触碰外部世界」的 fail-closed
    契约，且既有 ``except LedgerWriteError`` 的调用方无需改动。"""


class EffectLedger:
    """Durable effect ledger. One instance owns one SQLite connection.

    Parameters
    ----------
    db_path:
        SQLite file path. Use ``":memory:"`` for tests.
    clock:
        Injectable clock (returns epoch seconds) for deterministic lease tests.
    lease_seconds:
        How long an EXECUTING lease stays valid before another executor may
        take over (with a bumped fencing token).
    enabled:
        ``False`` turns the ledger into a pass-through (counter-example mode
        for experiments only — never in production).
    """

    def __init__(
        self,
        db_path: str,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60.0,
        enabled: bool = True,
    ):
        # 1.3 多线程共享单实例：check_same_thread=False + 实例锁串行化（见 _sync）
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # 1.1 多进程并发加固：WAL 让读不阻塞写；busy_timeout 消 'database is locked'
        # 顺序铁律 + 重试铁律（2026-11-14 并发夹具实测）：
        # ① busy_timeout 必须先设——后续普通写锁竞争靠它排队；
        # ② journal_mode=WAL 是例外：SQLite 文档行为——该 PRAGMA 遇锁立即
        #    返回 SQLITE_BUSY 且【不调用 busy handler】，busy_timeout 对其无效；
        # ③ WAL 是 DB 文件持久属性：并发首开时另一连接可能已把它设为 WAL，
        #    此时读当前值即通。故用「短重试 + 现值检查」而非裸 execute。
        self.conn.execute("PRAGMA busy_timeout=5000")
        _wal_exc: Exception | None = None
        for _ in range(50):  # 50×50ms=2.5s 上界，覆盖并发首开窗口
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                _wal_exc = None
                break
            except sqlite3.OperationalError as exc:
                _wal_exc = exc
                time.sleep(0.05)
                cur = self.conn.execute("PRAGMA journal_mode").fetchone()
                if cur and str(cur[0]).lower() == "wal":
                    _wal_exc = None  # 别的连接已落 WAL，持久属性已生效
                    break
        if _wal_exc is not None:
            raise _wal_exc
        self._init_schema()
        self._lock = threading.RLock()
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.enabled = enabled
        self._fail_next_write = False  # fault injection hook (tests)

    def _init_schema(self) -> None:
        """1.2 schema 版本化：user_version 记录版本，未来迁移按序追加。
        v1 = 基线（SCHEMA 幂等建表）；旧库 user_version=0 打开即自动升到 1。"""
        self.conn.executescript(SCHEMA)
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # 未来版本在此按 `elif version < N:` 追加迁移，禁改已发布版本语义
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    # ---------- internals ----------
    def _now(self) -> float:
        return self.clock()

    def _audit(self, intent_id: str, event: str, detail: dict | None = None) -> None:
        # 1.7 结构化日志：审计事件同步进 logging（cron/守护进程可采集）
        if event in (
            "TAKEOVER",
            "RECONCILED_CONFIRMED",
            "RECONCILED_RETRY_ALLOWED",
            "PAUSED_HIGH_RISK_UNPROBEABLE",
            "RESOLVED_APPROVE",
            "RESOLVED_REJECT",
            "REPLAY_BLOCKED_CONFIRMED",
            "REPLAY_BLOCKED_REJECTED",
            "STALE_COMPLETE_REJECTED",
            "PROBE_ERROR",
        ):
            logger.info("effect %s %s %s", intent_id, event, detail or "")
        else:
            logger.debug("effect %s %s %s", intent_id, event, detail or "")
        self.conn.execute(
            "INSERT INTO audit_log(intent_id,event,detail,at) VALUES(?,?,?,?)",
            (
                intent_id,
                event,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                self._now(),
            ),
        )

    def _write(self, sql: str, args: tuple):
        """Single entry point for all writes: injection point + fail-closed.

        1.8 审计原子性：本方法不再 commit——状态迁移与随后的 _audit 事件
        由调用方在同一事务内一起 commit，崩溃时二者要么都在要么都无，
        杜绝「状态已变而审计缺行」的半提交窗口。返回 cursor 供 CAS 调用方
        检查 rowcount（P1-2 fencing）。"""
        if self._fail_next_write:
            self._fail_next_write = False
            raise LedgerWriteError("injected: db write failure")
        return self.conn.execute(sql, args)

    def _get(self, idem_key: str | None = None, intent_id: str | None = None):
        if idem_key is not None:
            return self.conn.execute(
                "SELECT * FROM effects WHERE idem_key=?", (idem_key,)
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM effects WHERE intent_id=?", (intent_id,)
        ).fetchone()

    def _dispatch_existing(self, row, now: float) -> dict:
        """按已存在行的当前状态重派发（begin 正常路径 / P1-1 竞态重读 /
        P1-2 CAS 失败重读 三条路径共用，语义单一来源）。"""
        st = row["state"]
        if st == "CONFIRMED":
            self._audit(row["intent_id"], "REPLAY_BLOCKED_CONFIRMED")
            self.conn.commit()
            return {"action": "ALREADY_CONFIRMED", "intent_id": row["intent_id"]}
        if st == "EXECUTING":
            if row["lease_expires_at"] > now:
                return {
                    "action": "IN_PROGRESS",
                    "intent_id": row["intent_id"],
                    "note": "another executor holds a valid lease",
                }
            return self._takeover(row, "lease_expired_takeover")
        if st == "UNCERTAIN":
            return {
                "action": "RECONCILE_REQUIRED",
                "intent_id": row["intent_id"],
                "note": "previous outcome unknown — blind replay forbidden, reconcile first",
            }
        if st == "PAUSED":
            return {
                "action": "PAUSED",
                "intent_id": row["intent_id"],
                "note": "high risk unverifiable — escalated to human",
            }
        if st == "REJECTED":
            # 人工裁决拒绝 = 终态，重放禁止（与 CONFIRMED 同级对待）
            self._audit(row["intent_id"], "REPLAY_BLOCKED_REJECTED")
            self.conn.commit()
            return {
                "action": "REJECTED",
                "intent_id": row["intent_id"],
                "note": "human rejected — replay forbidden",
            }
        raise AssertionError(f"unknown state {st}")

    def _takeover(self, row, reason: str) -> dict:
        """Lease expired: fence the old executor and require external reconciliation.

        P1-2 fencing CAS：UPDATE 带 ``AND fencing_token=<读到的旧值> AND
        state='EXECUTING'`` 守卫，跨进程并发接管只有一个赢家；rowcount==0
        说明他人已接管（或行已被他人收口），重读按当前状态重派发。"""
        old_tok = row["fencing_token"]
        new_tok = old_tok + 1
        now = self._now()
        cur = self._write(
            "UPDATE effects SET state='UNCERTAIN', fencing_token=?, lease_expires_at=?, updated_at=? "
            "WHERE intent_id=? AND fencing_token=? AND state='EXECUTING' AND lease_expires_at<=?",
            (new_tok, now, now, row["intent_id"], old_tok, now),
        )
        if cur.rowcount == 0:
            # CAS 失败：他人已接管/已收口。回滚空事务后重读，按当前状态重派发。
            self.conn.rollback()
            fresh = self._get(intent_id=row["intent_id"])
            if fresh is None:
                raise LedgerWriteError(
                    f"intent {row['intent_id']} vanished during takeover — fail-closed"
                )
            return self._dispatch_existing(fresh, self._now())
        self._audit(
            row["intent_id"], "TAKEOVER", {"reason": reason, "new_token": new_tok}
        )
        self.conn.commit()
        return {
            "action": "RECONCILE_REQUIRED",
            "intent_id": row["intent_id"],
            "fencing_token": new_tok,
            "note": "expired execution outcome unknown — reconcile before retry",
        }

    # ---------- core API ----------
    @_sync
    def begin(
        self, task_id: str, step_id: str, params: dict, risk: str = "low"
    ) -> dict:
        """Call before ANY external side effect. Returns one of:

        - ``EXECUTE``            — go ahead; carries ``intent_id`` + ``fencing_token``
        - ``ALREADY_CONFIRMED``  — zero replay, the world already has the effect
        - ``IN_PROGRESS``        — another executor holds a valid lease
        - ``RECONCILE_REQUIRED`` — previous attempt is UNCERTAIN; reconcile first
        - ``PAUSED``             — high risk unverifiable; human required
        """
        if not self.enabled:  # counter-example mode: unprotected pass-through
            return {
                "action": "EXECUTE",
                "intent_id": "no-ledger",
                "fencing_token": 0,
                "note": "ledger disabled — unprotected",
            }
        key = make_idem_key(task_id, step_id, params)
        row = self._get(idem_key=key)
        now = self._now()
        if row is None:
            iid = f"eff-{uuid.uuid4().hex[:12]}"
            try:
                self._write(
                    "INSERT INTO effects(intent_id,idem_key,task_id,step_id,params_hash,risk,state,"
                    "fencing_token,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        iid,
                        key,
                        task_id,
                        step_id,
                        key.split("|")[2],
                        risk,
                        "EXECUTING",
                        1,
                        now + self.lease_seconds,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # P1-1 跨进程竞态：另一实例已抢先提交同 idem_key（SELECT→INSERT
                # 窗口裸露 UNIQUE 约束）。回滚失败事务，重读该行按现有状态重派发
                # （IN_PROGRESS / ALREADY_CONFIRMED / RECONCILE_REQUIRED / ...），
                # 绝不向外抛 sqlite3 原生异常（WORKPLAN 1.1「0 报错 0 重复」）。
                self.conn.rollback()
                row = self._get(idem_key=key)
                if row is None:
                    raise LedgerWriteError(
                        f"intent for key {key} vanished after conflict — fail-closed"
                    )
                return self._dispatch_existing(row, self._now())
            self._audit(iid, "INTENT_PERSISTED", {"risk": risk})
            self.conn.commit()
            return {"action": "EXECUTE", "intent_id": iid, "fencing_token": 1}
        return self._dispatch_existing(row, now)

    @_sync
    def complete(self, intent_id: str, fencing_token: int, receipt: dict) -> dict:
        """Success receipt for the external op. A token mismatch means the
        caller is a stale executor — the receipt is rejected, never applied."""
        if not self.enabled:
            return {"action": "OK", "note": "no-ledger"}
        row = self._get(intent_id=intent_id)
        if row is None:
            raise LedgerWriteError(f"intent {intent_id} not persisted — fail-closed")
        if row["state"] != "EXECUTING" or row["fencing_token"] != fencing_token:
            self._audit(
                intent_id,
                "STALE_COMPLETE_REJECTED",
                {
                    "got_token": fencing_token,
                    "cur_token": row["fencing_token"],
                    "cur_state": row["state"],
                },
            )
            self.conn.commit()
            return {
                "action": "REJECTED_STALE",
                "note": "fencing token stale or state mismatch",
            }
        now = self._now()
        cursor = self._write(
            "UPDATE effects SET state='CONFIRMED', receipt_json=?, updated_at=? "
            "WHERE intent_id=? AND fencing_token=? AND state='EXECUTING'",
            (json.dumps(receipt, ensure_ascii=False), now, intent_id, fencing_token),
        )
        if cursor.rowcount == 0:
            self.conn.rollback()
            self._audit(
                intent_id, "STALE_COMPLETE_REJECTED", {"got_token": fencing_token}
            )
            self.conn.commit()
            return {"action": "REJECTED_STALE"}
        self._audit(intent_id, "CONFIRMED", receipt)
        self.conn.commit()
        return {"action": "CONFIRMED"}

    @_sync
    def mark_receipt_lost(self, intent_id: str, fencing_token: int) -> dict:
        """External op may have happened but the receipt was lost -> UNCERTAIN
        (blind replay forbidden; reconcile with a probe first)."""
        row = self._get(intent_id=intent_id)
        if row is None:
            # P2-1 与 complete 口径一致：不存在的意图 = fail-closed 显式报错，
            # 不再裸露 TypeError('NoneType' object is not subscriptable)。
            raise LedgerWriteError(f"intent {intent_id} not persisted — fail-closed")
        if row["state"] != "EXECUTING" or row["fencing_token"] != fencing_token:
            return {"action": "REJECTED_STALE"}
        cursor = self._write(
            "UPDATE effects SET state='UNCERTAIN', updated_at=? "
            "WHERE intent_id=? AND fencing_token=? AND state='EXECUTING'",
            (self._now(), intent_id, fencing_token),
        )
        if cursor.rowcount == 0:
            self.conn.rollback()
            return {"action": "REJECTED_STALE"}
        self._audit(intent_id, "RECEIPT_LOST_UNCERTAIN")
        self.conn.commit()
        return {"action": "UNCERTAIN"}

    @_sync
    def reconcile(
        self, intent_id: str, probe: Callable[[str, str], bool | None]
    ) -> dict:
        """Reconcile an UNCERTAIN intent against external ground truth.

        ``probe(task_id, step_id)`` returns:
        - ``True``  — the effect verifiably exists -> CONFIRMED (zero replay)
        - ``False`` — verifiably absent -> EXECUTE allowed (new fencing token)
        - ``None``  — probe unavailable -> PAUSED if high risk, else stay UNCERTAIN
        """
        row = self._get(intent_id=intent_id)
        if row is None:
            raise LedgerWriteError(f"intent {intent_id} not persisted — fail-closed")
        if row["state"] != "UNCERTAIN":
            raise LedgerStateError("reconcile applies only to UNCERTAIN")
        try:
            verdict = probe(row["task_id"], row["step_id"])
        except (
            Exception
        ) as exc:  # noqa: BLE001 — 1.4 probe 异常=探针不可用(None)，fail-closed 语义不变
            verdict = None
            self._audit(intent_id, "PROBE_ERROR", {"error": str(exc)})
        now = self._now()
        if verdict is True:
            self._write(
                "UPDATE effects SET state='CONFIRMED', reconcile_json=?, updated_at=? "
                "WHERE intent_id=?",
                (
                    json.dumps({"reconciled_by": "probe", "verdict": "effect_exists"}),
                    now,
                    intent_id,
                ),
            )
            self._audit(intent_id, "RECONCILED_CONFIRMED", {"probe": "effect_exists"})
            self.conn.commit()
            return {
                "action": "CONFIRMED",
                "note": "external effect exists — closed with zero replay",
            }
        if verdict is False:
            old_tok = row["fencing_token"]
            new_tok = old_tok + 1
            # P1-2 fencing CAS：带旧 token + state='UNCERTAIN' 守卫，两个并发
            # reconcile 只有一个能放行重试，杜绝「同 token 双执行器」。
            cur = self._write(
                "UPDATE effects SET state='EXECUTING', fencing_token=?, lease_expires_at=?, "
                "reconcile_json=?, updated_at=? "
                "WHERE intent_id=? AND fencing_token=? AND state='UNCERTAIN'",
                (
                    new_tok,
                    now + self.lease_seconds,
                    json.dumps({"reconciled_by": "probe", "verdict": "no_effect"}),
                    now,
                    intent_id,
                    old_tok,
                ),
            )
            if cur.rowcount == 0:
                # CAS 失败：他人已改写该行 → 回滚空事务，重读按当前状态重派发。
                self.conn.rollback()
                fresh = self._get(intent_id=intent_id)
                if fresh is None:
                    raise LedgerWriteError(
                        f"intent {intent_id} vanished during reconcile — fail-closed"
                    )
                st = fresh["state"]
                if st == "CONFIRMED":
                    return {
                        "action": "ALREADY_CONFIRMED",
                        "intent_id": intent_id,
                        "note": "concurrent reconcile closed the effect — zero replay",
                    }
                if st == "EXECUTING":
                    return {
                        "action": "IN_PROGRESS",
                        "intent_id": intent_id,
                        "note": "concurrent reconcile already allowed a retry",
                    }
                if st == "PAUSED":
                    return {
                        "action": "PAUSED",
                        "intent_id": intent_id,
                        "note": "concurrent reconcile escalated to human",
                    }
                if st == "REJECTED":
                    return {
                        "action": "REJECTED",
                        "intent_id": intent_id,
                        "note": "human rejected — replay forbidden",
                    }
                return {
                    "action": "RECONCILE_REQUIRED",
                    "intent_id": intent_id,
                    "note": "state changed concurrently — reconcile again",
                }
            self._audit(intent_id, "RECONCILED_RETRY_ALLOWED", {"new_token": new_tok})
            self.conn.commit()
            return {
                "action": "EXECUTE",
                "intent_id": intent_id,
                "fencing_token": new_tok,
                "note": "reconcile proved no external effect — retry allowed",
            }
        # probe unavailable
        if row["risk"] == "high":
            self._write(
                "UPDATE effects SET state='PAUSED', reconcile_json=?, updated_at=? "
                "WHERE intent_id=?",
                (
                    json.dumps(
                        {"reconciled_by": "probe", "verdict": "probe_unavailable"}
                    ),
                    now,
                    intent_id,
                ),
            )
            self._audit(intent_id, "PAUSED_HIGH_RISK_UNPROBEABLE")
            self.conn.commit()
            return {
                "action": "PAUSED",
                "note": "high risk and probe cannot confirm — escalate to human",
            }
        return {
            "action": "RECONCILE_LATER",
            "note": "low risk — stay UNCERTAIN, retry reconcile later",
        }

    @_sync
    def resolve(
        self, intent_id: str, action: str, note: str = "", actor: str = "human"
    ) -> dict:
        """人工裁决 PAUSED 意图（WORKPLAN 1.5，控制台/CLI 共用同一状态机语义）。

        - ``approve`` -> EXECUTING，fencing_token+1（执行器持新 token 重试）
        - ``reject``  -> REJECTED 终态（begin() 对该 idem_key 永远返回 REJECTED）
        """
        row = self._get(intent_id=intent_id)
        if row is None:
            raise LedgerWriteError(f"intent {intent_id} not persisted — fail-closed")
        if row["state"] != "PAUSED":
            raise LedgerStateError(
                f"resolve applies only to PAUSED (got {row['state']})"
            )
        if action not in ("approve", "reject"):
            raise ValueError("action must be approve|reject")
        now = self._now()
        if action == "approve":
            new_tok = row["fencing_token"] + 1
            self._write(
                "UPDATE effects SET state='EXECUTING', fencing_token=?, lease_expires_at=?, "
                "updated_at=? WHERE intent_id=?",
                (new_tok, now + self.lease_seconds, now, intent_id),
            )
            self._audit(
                intent_id,
                "RESOLVED_APPROVE",
                {"actor": actor, "note": note, "new_token": new_tok},
            )
            self.conn.commit()
            return {
                "action": "EXECUTE",
                "intent_id": intent_id,
                "fencing_token": new_tok,
                "note": note,
            }
        self._write(
            "UPDATE effects SET state='REJECTED', updated_at=? WHERE intent_id=?",
            (now, intent_id),
        )
        self._audit(intent_id, "RESOLVED_REJECT", {"actor": actor, "note": note})
        self.conn.commit()
        return {"action": "REJECTED", "intent_id": intent_id, "note": note}

    # ---------- productized helpers ----------
    @_sync
    def guard(
        self,
        task_id: str,
        step_id: str,
        params: dict,
        execute: Callable[[], dict],
        probe: Callable[[str, str], bool | None],
        risk: str = "low",
    ) -> dict:
        """One-shot guard: run ``execute()`` exactly once *in effect*, even
        across crashes and retries.

        ``execute()`` performs the external side effect and returns a receipt
        dict. If it raises, the ledger assumes the receipt was lost (the op
        may still have happened) and marks UNCERTAIN, then reconciles via
        ``probe`` before any retry. Returns a final dict with ``action`` in
        {CONFIRMED, ALREADY_CONFIRMED, EXECUTED->CONFIRMED, PAUSED, ...}.
        """
        b = self.begin(task_id, step_id, params, risk=risk)
        act = b["action"]
        if act == "ALREADY_CONFIRMED":
            return {"action": "ALREADY_CONFIRMED", "intent_id": b["intent_id"]}
        if act in ("IN_PROGRESS", "PAUSED"):
            return b
        if act == "RECONCILE_REQUIRED":
            r = self.reconcile(b["intent_id"], probe)
            if r["action"] != "EXECUTE":
                return r
            b = r  # reconcile proved absence -> retry allowed with new token
        # act == EXECUTE (fresh or via reconcile/takeover)
        try:
            receipt = execute()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — any failure may still mean "it happened"
            u = self.mark_receipt_lost(b["intent_id"], b["fencing_token"])
            if u["action"] != "UNCERTAIN":
                return u
            r = self.reconcile(b["intent_id"], probe)
            if r["action"] == "EXECUTE":
                return {
                    "action": "RETRY_ALLOWED",
                    "intent_id": b["intent_id"],
                    "fencing_token": r["fencing_token"],
                    "error": str(exc),
                    "note": "reconcile proved the op did not land — safe to retry",
                }
            return {**r, "error": str(exc)}
        c = self.complete(b["intent_id"], b["fencing_token"], receipt)
        return {**c, "intent_id": b["intent_id"], "receipt": receipt}

    @_sync
    def sweep_pending(
        self, probe: Callable[[str, str], bool | None], limit: int = 100
    ) -> list[dict]:
        """Batch sweeper for background jobs: reconcile every UNCERTAIN row
        against external truth. Returns one result dict per row."""
        rows = self.conn.execute(
            "SELECT intent_id FROM effects WHERE state='UNCERTAIN' ORDER BY updated_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self.reconcile(r["intent_id"], probe) for r in rows]

    @_sync
    def sweep_stale_executing(self, limit: int = 100) -> list[dict]:
        """恢复辅助（1.6 崩溃恢复标准姿势）：租期已过期仍 EXECUTING 的行 =
        执行器硬崩溃现场，转 UNCERTAIN（fail-closed，绝不盲目重放）。
        转完后配合 sweep_pending 用探针对账收口。"""
        now = self._now()
        rows = self.conn.execute(
            "SELECT intent_id, fencing_token FROM effects "
            "WHERE state='EXECUTING' AND lease_expires_at <= ? ORDER BY updated_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [
            self.mark_receipt_lost(r["intent_id"], r["fencing_token"]) for r in rows
        ]

    @_sync
    def pending(self, state: str = "UNCERTAIN") -> list[dict]:
        """List intents in a given state (UNCERTAIN/PAUSED/EXECUTING) for ops review."""
        rows = self.conn.execute(
            "SELECT * FROM effects WHERE state=? ORDER BY updated_at", (state,)
        ).fetchall()
        return [dict(r) for r in rows]

    @_sync
    def state_of(self, task_id: str, step_id: str, params: dict) -> dict | None:
        row = self._get(idem_key=make_idem_key(task_id, step_id, params))
        return dict(row) if row else None

    @_sync
    def audit_of(self, intent_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log WHERE intent_id=? ORDER BY id", (intent_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
