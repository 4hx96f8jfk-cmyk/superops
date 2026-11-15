"""Provider 注册表：SQLite 持久化的 OpenAI 兼容端点目录。

本地 llama.cpp/Ollama 类端点与云端 API 统一抽象为 ProviderConfig，
差异只体现在 kind（路由优先级）与 api_key_ref（云端通常要鉴权）。

持久化设计：整条配置以 JSON 存 config 列，另冗余 kind/enabled/weight
三个索引列便于 SQL 侧过滤。config 里只允许密钥*引用*（secrets.validate_ref
在 pydantic 校验层强制），所以整库可安全 grep 审计。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .secrets import validate_ref

_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    weight      REAL NOT NULL DEFAULT 1.0,
    config      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class ProviderConfig(BaseModel):
    """一个 OpenAI 兼容端点的完整配置。"""

    name: str
    base_url: str  # 例: http://127.0.0.1:8080/v1 或 https://api.openai.com/v1
    model: str = "default"
    kind: Literal["local", "cloud"] = "local"
    weight: float = 1.0
    enabled: bool = True
    api_key_ref: str | None = None  # 只存引用（env:/keychain:），校验器强制
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0  # per-provider 超时（M2-2.4），慢端点不会拖死回退链
    # 价格表（M2-2.6）：每 1k token 单价，默认 0 = 本地端点免费
    price_input_per_1k: float = 0.0
    price_output_per_1k: float = 0.0

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url 必须是 http(s) URL: {v!r}")
        return v

    @field_validator("api_key_ref")
    @classmethod
    def _check_key_ref(cls, v: str | None) -> str | None:
        # 入库前最后一道门：明文密钥在这里就会被拒
        return validate_ref(v) if v else v


class ProviderRegistry:
    """provider 注册/删除/列出，SQLite 持久化，线程安全。"""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """暴露连接供 UsageStore 复用（同一 SQLite 文件只开一条连接，锁统一）。"""
        return self._conn

    # ---- CRUD -------------------------------------------------------------

    def register(self, config: ProviderConfig) -> ProviderConfig:
        """注册或整体覆盖一个 provider（同名即更新）。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO providers(name, kind, enabled, weight, config, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     kind=excluded.kind, enabled=excluded.enabled,
                     weight=excluded.weight, config=excluded.config,
                     updated_at=excluded.updated_at""",
                (
                    config.name,
                    config.kind,
                    int(config.enabled),
                    config.weight,
                    config.model_dump_json(),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return config

    def remove(self, name: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM providers WHERE name=?", (name,))
            self._conn.commit()
            return cur.rowcount > 0

    def get(self, name: str) -> ProviderConfig:
        with self._lock:
            row = self._conn.execute(
                "SELECT config FROM providers WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            raise KeyError(f"未注册的 provider: {name}")
        return ProviderConfig.model_validate_json(row[0])

    def list(self, enabled_only: bool = False) -> list[ProviderConfig]:
        sql = "SELECT config FROM providers"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [ProviderConfig.model_validate_json(r[0]) for r in rows]

    def set_enabled(self, name: str, enabled: bool) -> ProviderConfig:
        cfg = self.get(name).model_copy(update={"enabled": enabled})
        self.register(cfg)
        return cfg

    def close(self) -> None:
        with self._lock:
            self._conn.close()
