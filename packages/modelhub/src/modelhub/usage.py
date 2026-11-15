"""用量统计：每次调用一条流水 + 按日聚合查询。

记录维度：provider/model/prompt_tokens/completion_tokens/延迟/成败。
失败调用也记录（status='error'），否则『错误率』无从算起。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    day               TEXT NOT NULL,          -- YYYY-MM-DD（UTC），聚合键冗余成列便于索引
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        REAL,
    status            TEXT NOT NULL            -- ok | error
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day, provider, model);
"""


@dataclass
class UsageRecord:
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float | None = None
    status: str = "ok"
    ts: float = 0.0


class UsageStore:
    def __init__(self, conn: sqlite3.Connection):
        # 与 registry 共用连接（同一 SQLite 文件），锁由调用方 Hub 统一管理
        self._conn = conn
        self._lock = threading.Lock()
        self._conn.executescript(_USAGE_SCHEMA)
        self._conn.commit()

    def record(self, rec: UsageRecord) -> None:
        ts = rec.ts or time.time()
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                """INSERT INTO usage(ts, day, provider, model, prompt_tokens,
                                      completion_tokens, latency_ms, status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    ts,
                    day,
                    rec.provider,
                    rec.model,
                    rec.prompt_tokens,
                    rec.completion_tokens,
                    rec.latency_ms,
                    rec.status,
                ),
            )
            self._conn.commit()

    def daily_stats(
        self, day: str | None = None, provider: str | None = None
    ) -> list[dict]:
        """按 (day, provider, model) 聚合。day=None 时返回所有历史日。"""
        sql = """SELECT day, provider, model,
                        COUNT(*) AS calls,
                        SUM(status='ok') AS ok_calls,
                        SUM(status='error') AS error_calls,
                        SUM(prompt_tokens) AS prompt_tokens,
                        SUM(completion_tokens) AS completion_tokens,
                        AVG(latency_ms) AS avg_latency_ms
                 FROM usage"""
        conds, args = [], []
        if day:
            conds.append("day=?")
            args.append(day)
        if provider:
            conds.append("provider=?")
            args.append(provider)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " GROUP BY day, provider, model ORDER BY day DESC, provider, model"
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def all_rows(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, provider, model, prompt_tokens, completion_tokens, latency_ms, status FROM usage ORDER BY id"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def cost_summary(
        self, prices: dict[str, dict] | None = None, provider: str | None = None
    ) -> list[dict]:
        """按 provider 聚合用量 + 估算成本（M2-2.6）。

        prices: {provider: {"input_per_1k": x, "output_per_1k": y}}，
        缺省或单价 0 视为免费（本地端点默认）。成本 =
        prompt_tokens/1000*input_per_1k + completion_tokens/1000*output_per_1k。
        """
        prices = prices or {}
        sql = """SELECT provider,
                        COUNT(*) AS calls,
                        SUM(status='ok') AS ok_calls,
                        SUM(status='error') AS error_calls,
                        SUM(prompt_tokens) AS prompt_tokens,
                        SUM(completion_tokens) AS completion_tokens
                 FROM usage"""
        args: list = []
        if provider:
            sql += " WHERE provider=?"
            args.append(provider)
        sql += " GROUP BY provider ORDER BY provider"
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for row in rows:
            p = prices.get(row["provider"]) or {}
            in_price = float(p.get("input_per_1k", 0.0))
            out_price = float(p.get("output_per_1k", 0.0))
            row["cost"] = round(
                row["prompt_tokens"] / 1000.0 * in_price
                + row["completion_tokens"] / 1000.0 * out_price,
                10,
            )
        return rows
