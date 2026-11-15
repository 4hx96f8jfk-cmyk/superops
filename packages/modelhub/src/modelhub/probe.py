"""健康探针：区分『端点可达』与『真能推理』。

内部生产教训（必须保留的设计）：
1. 只测 TCP/响应形状会『假绿』——端口开着但模型没加载/推理崩了的端点比比皆是。
   所以健康 = 真实发一条 chat completion 并检查产出。
2. reasoning 模型的思考 token 会吃掉 max_tokens 预算：给 16 token 时
   content 可能为空（全被 reasoning_content 吃了），曾被误判为不可用。
   因此探活默认 max_tokens=64，且 content 与 reasoning_content 任一非空
   都算『真能推理』。
3. 探活结果带时间戳缓存（TTL），避免高频调用反复打探活请求。
"""

from __future__ import annotations

import dataclasses
import json
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .registry import ProviderConfig, ProviderRegistry
from .secrets import resolve_secret

# 探活 prompt 与预算：64 是给 reasoning 模型留的思考余量（见模块注释第 2 条）
_PROBE_MESSAGES = [{"role": "user", "content": "ping"}]
DEFAULT_PROBE_MAX_TOKENS = 64

STATUS_HEALTHY = "healthy"  # TCP 通 + 真实推理出词
STATUS_REACHABLE = "reachable"  # TCP 通（HTTP 有响应）但推理产出为空/失败
STATUS_DOWN = "down"  # TCP 都不通

# 探活缓存持久化表（M2-2.2）：provider/ok/expires_at 为约定列，
# result 列存整条 ProbeResult 的 JSON，重启进程后可无损重建缓存条目。
_PROBE_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_cache (
    provider   TEXT PRIMARY KEY,
    ok         INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    result     TEXT NOT NULL
);
"""


@dataclass
class ProbeResult:
    provider: str
    status: str
    tcp_ok: bool
    inference_ok: bool
    latency_ms: float | None
    detail: str
    checked_at: float = field(default_factory=time.time)


def _tcp_check(base_url: str, timeout: float) -> tuple[bool, str]:
    u = urlparse(base_url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    if not host:
        return False, "base_url 无法解析主机"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"TCP {host}:{port} 可达"
    except OSError as e:
        return False, f"TCP {host}:{port} 不可达: {e}"


def _auth_headers(cfg: ProviderConfig) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = resolve_secret(cfg.api_key_ref)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(cfg.extra_headers)
    return headers


def inference_probe(
    cfg: ProviderConfig,
    max_tokens: int = DEFAULT_PROBE_MAX_TOKENS,
    timeout: float | None = None,
) -> tuple[bool, float, str]:
    """真实推理探活：发一条小 max_tokens 的 chat completion。

    返回 (inference_ok, latency_ms, detail)。
    判定标准：HTTP 200 且 content 或 reasoning_content 任一非空。
    """
    t0 = time.monotonic()
    url = cfg.base_url + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": _PROBE_MESSAGES,
        "max_tokens": max_tokens,  # 给够，防 reasoning 模型吃光预算返回空 content
    }
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers=_auth_headers(cfg),
            timeout=timeout or cfg.timeout,
        )
    except httpx.HTTPError as e:
        return False, (time.monotonic() - t0) * 1000, f"推理请求异常: {e}"
    latency = (time.monotonic() - t0) * 1000
    if resp.status_code != 200:
        return False, latency, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        msg = resp.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as e:
        return False, latency, f"响应形状异常: {e}"
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    if content:
        return True, latency, f"推理正常: {content[:60]!r}"
    if reasoning:
        # reasoning 模型 content 为空但思考链有产出 —— 这就是当年 16-token 误判的修正
        return True, latency, f"推理正常(reasoning 产出): {reasoning[:60]!r}"
    finish = ""
    try:
        finish = resp.json()["choices"][0].get("finish_reason", "")
    except Exception:
        pass
    return (
        False,
        latency,
        (
            f"HTTP 200 但产出为空（finish_reason={finish!r}）。"
            f"若为 reasoning 模型请增大探活 max_tokens（当前 {max_tokens}）"
        ),
    )


class HealthProber:
    """探活结果缓存（SQLite 持久化，M2-2.2）。

    缓存落 modelhub 库的 probe_cache 表（provider/ok/expires_at + result JSON），
    进程重启后仍命中，不重复打探活请求；TTL 语义与内存版一致：
    ttl<=0 永不缓存，force=True 无视缓存重新探活。
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        cache_ttl: float = 60.0,
        probe_max_tokens: int = DEFAULT_PROBE_MAX_TOKENS,
        tcp_timeout: float = 3.0,
    ):
        self._registry = registry
        self._ttl = cache_ttl
        self._probe_max_tokens = probe_max_tokens
        self._tcp_timeout = tcp_timeout
        # 与 registry 共用连接（同一 SQLite 文件只开一条连接），锁独立
        self._conn = registry.conn
        self._lock = threading.Lock()
        self._conn.executescript(_PROBE_CACHE_SCHEMA)
        self._conn.commit()

    def probe(self, name: str, force: bool = False) -> ProbeResult:
        if not force:
            hit = self._cache_get(name)
            if hit is not None:
                return hit
        cfg = self._registry.get(name)
        result = self._probe_config(cfg)
        self._cache_put(result)
        return result

    def probe_all(self, force: bool = False) -> dict[str, ProbeResult]:
        return {c.name: self.probe(c.name, force=force) for c in self._registry.list()}

    def invalidate(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._conn.execute("DELETE FROM probe_cache")
            else:
                self._conn.execute("DELETE FROM probe_cache WHERE provider=?", (name,))
            self._conn.commit()

    # ---- SQLite 缓存读写 ----------------------------------------------------

    def _cache_get(self, name: str) -> ProbeResult | None:
        if self._ttl <= 0:
            return None  # TTL=0 → 本探活器完全不使用缓存（与内存版语义一致）
        with self._lock:
            row = self._conn.execute(
                "SELECT result, expires_at FROM probe_cache WHERE provider=?",
                (name,),
            ).fetchone()
        if row is None or time.time() >= row[1]:
            return None
        try:
            return ProbeResult(**json.loads(row[0]))
        except (ValueError, TypeError):
            # 缓存行损坏按未命中处理，重新探活覆盖
            return None

    def _cache_put(self, result: ProbeResult) -> None:
        if self._ttl <= 0:
            return  # TTL=0 → 永不缓存（与内存版语义一致）
        with self._lock:
            self._conn.execute(
                """INSERT INTO probe_cache(provider, ok, expires_at, result)
                   VALUES(?,?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET
                     ok=excluded.ok, expires_at=excluded.expires_at,
                     result=excluded.result""",
                (
                    result.provider,
                    int(result.inference_ok),
                    result.checked_at + self._ttl,
                    json.dumps(dataclasses.asdict(result), ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def _probe_config(self, cfg: ProviderConfig) -> ProbeResult:
        tcp_ok, tcp_detail = _tcp_check(cfg.base_url, self._tcp_timeout)
        if not tcp_ok:
            return ProbeResult(cfg.name, STATUS_DOWN, False, False, None, tcp_detail)
        ok, latency, detail = inference_probe(cfg, self._probe_max_tokens)
        status = STATUS_HEALTHY if ok else STATUS_REACHABLE
        return ProbeResult(
            cfg.name, status, True, ok, latency, f"{tcp_detail}; {detail}"
        )
