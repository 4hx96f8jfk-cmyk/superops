"""HongYun 超级運維智能體（HongYun SuperOps）运维控制台 —— FastAPI 应用工厂。

双模式语义（2026-11-13 运维负责人令「不能只有只读模式，也要有全功能模式」）：
- read-only：所有写端点（POST/PUT/PATCH/DELETE）一律 403 {"error":"mode=read-only"}，
  纯安全展示。
- full：写操作放行，且每个写操作必须双留痕——ledger 的 audit_log（状态机事件）
  + 独立 console_audit 表（who/what/when/params 操作审计）。

鉴权语义（沿用内部版 C0 负向验收）：未鉴权 API 401、页面 302 → /gate；
gate 页提交 token 换 httpOnly cookie。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from effect_ledger import EffectLedger
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import COOKIE_NAME, HEADER_NAME, token_equals, token_file_path
from .wazuh import UNCONFIGURED_HINT as WAZUH_UNCONFIGURED_HINT
from .wazuh import DEFAULT_API_URL, DEFAULT_INDEXER_URL, DEFAULT_INDEXER_USER, WazuhClient

STATIC_DIR = Path(__file__).parent / "static"

# 写方法集合：read-only 模式下这些方法打 /api/* 一律 403
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 鉴权豁免路径：gate 提交口 + gate 页本身
AUTH_EXEMPT = {"/api/token", "/gate"}

# token 校验失败限流（WORKPLAN M3）：按 IP 滑动窗口，1 分钟内失败 ≥5 次后
# 第 6 次起直接 429，防 token 爆破。内存态即可——控制台是单进程服务。
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_WINDOW_S = 60.0


class _SlidingWindowLimiter:
    """按 key（IP）记录失败时间戳的滑动窗口限流器，线程安全。"""

    def __init__(self, max_failures: int, window_s: float):
        self.max_failures = max_failures
        self.window_s = window_s
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        """只读判定：窗口内失败次数已达上限（不记录本次）。

        用 .get 而非 defaultdict 下标——未知 IP 不得凭空建空 deque 键，
        否则每个来过的 IP 都永久占一个键（内存无界增长）。
        """
        now = time.time()
        with self._lock:
            dq = self._events.get(key)
            if not dq:
                return False
            while dq and now - dq[0] > self.window_s:
                dq.popleft()
            return len(dq) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > self.window_s:
                dq.popleft()
            dq.append(now)
            # 顺带回收：键数超阈值时全表剔除过期时间戳，再删空 deque 键，
            # 防历史 IP 键随流量无界增长（is_blocked 只读不删，回收只在这做）
            if len(self._events) > 64:
                for k in list(self._events):
                    v = self._events[k]
                    while v and now - v[0] > self.window_s:
                        v.popleft()
                    if not v:
                        del self._events[k]

    def reset(self, key: str) -> None:
        """鉴权成功后清零该 IP 的失败计数，避免正常用户被历史失败拖累。"""
        with self._lock:
            self._events.pop(key, None)


# 独立操作审计表：与 ledger 的 audit_log（状态机事件流）分离，
# 记录「谁在控制台做了什么」，full 模式每个写端点必写一条。
# prev_hash/row_hash 构成防篡改哈希链（M3）：row_hash =
# sha256(prev_hash + 本行关键字段)，链断即验真失败。
CONSOLE_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  who       TEXT NOT NULL,          -- 开源版单 token 即单操作者：'console'
  what      TEXT NOT NULL,          -- 动作名：resolve.approve / reconcile / ...
  intent_id TEXT,
  params    TEXT,                   -- 请求参数 JSON（脱敏由调用方负责）
  at        REAL NOT NULL,
  prev_hash TEXT,                   -- 上一行 row_hash（首行 = GENESIS_HASH）
  row_hash  TEXT                    -- 本行内容哈希（链式，防篡改）
);
CREATE TABLE IF NOT EXISTS console_audit_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 哈希链 schema 版本：升级哈希算法/字段拼接方式时 +1，并重新起链。
AUDIT_CHAIN_SCHEMA_VERSION = 1
# 创世哈希：由 schema_version 派生的固定常量，链的第一行以此为 prev_hash。
GENESIS_HASH = hashlib.sha256(
    f"superops-console-audit/v{AUDIT_CHAIN_SCHEMA_VERSION}".encode()
).hexdigest()


def _client_ip(request: Request) -> str:
    """限流计数用的客户端 IP（TestClient 下为 'testclient'）。"""
    return request.client.host if request.client else "unknown"


def _row_hash(
    prev_hash: str,
    row_id: int,
    who: str,
    what: str,
    intent_id: str | None,
    params: str | None,
    at: float,
) -> str:
    """行内容哈希：字段用 \\x1f 分隔拼接（不可出现在 JSON/常规文本），
    浮点用 repr 保证跨读写一致。"""
    payload = "\x1f".join(
        [
            prev_hash,
            str(row_id),
            who,
            what,
            intent_id or "",
            params or "",
            repr(float(at)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _init_audit_chain(conn: sqlite3.Connection) -> None:
    """迁移 + 起链（幂等，create_app 时调用一次）。

    兼容旧库：console_audit 无 hash 列时 ALTER 补列；旧行无 hash 时以
    schema_version 起点（GENESIS_HASH）重新起链，并记录一条
    audit.chain.genesis 创世事件说明迁移背景。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(console_audit)")}
    if "prev_hash" not in cols:
        conn.execute("ALTER TABLE console_audit ADD COLUMN prev_hash TEXT")
    if "row_hash" not in cols:
        conn.execute("ALTER TABLE console_audit ADD COLUMN row_hash TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS console_audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()

    n_unchained = conn.execute(
        "SELECT COUNT(*) FROM console_audit WHERE row_hash IS NULL"
    ).fetchone()[0]
    genesis_done = conn.execute(
        "SELECT 1 FROM console_audit_meta WHERE key='chain_genesis'"
    ).fetchone()
    if n_unchained and not genesis_done:
        # 旧行无 hash：记录创世事件（本行自身也参与随后的补链）
        conn.execute(
            "INSERT INTO console_audit(who,what,intent_id,params,at) VALUES(?,?,?,?,?)",
            (
                "system",
                "audit.chain.genesis",
                None,
                json.dumps(
                    {
                        "schema_version": AUDIT_CHAIN_SCHEMA_VERSION,
                        "legacy_rows": n_unchained,
                        "reason": "旧行无 hash，以 schema_version 起点重新起链",
                    },
                    ensure_ascii=False,
                ),
                time.time(),
            ),
        )
        conn.execute(
            "INSERT INTO console_audit_meta(key,value) VALUES('chain_genesis',?)",
            (
                json.dumps(
                    {"at": time.time(), "schema_version": AUDIT_CHAIN_SCHEMA_VERSION}
                ),
            ),
        )
        conn.commit()

    # 补链：所有无 hash 的行按 id 顺序接在链尾（链空则接 GENESIS_HASH）
    last = conn.execute(
        "SELECT row_hash FROM console_audit WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev = last[0] if last else GENESIS_HASH
    rows = conn.execute(
        "SELECT id,who,what,intent_id,params,at FROM console_audit "
        "WHERE row_hash IS NULL ORDER BY id"
    ).fetchall()
    for r in rows:
        h = _row_hash(
            prev, r["id"], r["who"], r["what"], r["intent_id"], r["params"], r["at"]
        )
        conn.execute(
            "UPDATE console_audit SET prev_hash=?, row_hash=? WHERE id=?",
            (prev, h, r["id"]),
        )
        prev = h
    conn.commit()


def _verify_audit_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    """全链校验：逐行重算哈希并核对 prev_hash 衔接。

    返回 {valid, broken_at, checked}；broken_at 指向首个断链行
    （{"id","what","reason"}），全链完好时为 None。
    """
    rows = conn.execute(
        "SELECT id,who,what,intent_id,params,at,prev_hash,row_hash "
        "FROM console_audit ORDER BY id"
    ).fetchall()
    prev = GENESIS_HASH
    for r in rows:
        if r["row_hash"] is None:
            return {
                "valid": False,
                "broken_at": {
                    "id": r["id"],
                    "what": r["what"],
                    "reason": "row_hash missing",
                },
                "checked": len(rows),
            }
        if r["prev_hash"] != prev:
            return {
                "valid": False,
                "broken_at": {
                    "id": r["id"],
                    "what": r["what"],
                    "reason": "prev_hash mismatch",
                },
                "checked": len(rows),
            }
        expect = _row_hash(
            prev, r["id"], r["who"], r["what"], r["intent_id"], r["params"], r["at"]
        )
        if r["row_hash"] != expect:
            return {
                "valid": False,
                "broken_at": {
                    "id": r["id"],
                    "what": r["what"],
                    "reason": "row_hash mismatch",
                },
                "checked": len(rows),
            }
        prev = r["row_hash"]
    return {"valid": True, "broken_at": None, "checked": len(rows)}


def _default_probe(task_id: str, step_id: str) -> None:
    """默认探针：保守返回 None（不可探测），reconcile 将按风险降级，
    绝不假装知道外部真值。调用方可经 create_app(probe=...) 注入真实探针。"""
    return None


class _ThreadSafeLedger(EffectLedger):
    """跨线程共享连接的 ledger：TestClient/uvicorn 线程池下 sqlite 默认
    check_same_thread 会炸，控制台只做单写者操作，配锁足够。"""

    def __init__(self, db_path: str):
        super().__init__(db_path)
        self.conn.close()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row


def _row_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """effects 行转对外 dict，JSON 字段解析成对象方便前端渲染。"""
    if row is None:
        return None
    d = dict(row)
    for k in ("receipt_json", "reconcile_json"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k.replace("_json", "")] = d[k]
        d.pop(k, None)
    return d


def create_app(
    db_path: str,
    token: str,
    mode: str = "read-only",
    modelhub_db: str | None = None,
    probe: Callable[[str, str], "bool | None"] | None = None,
    wazuh_compose: str | None = None,
    wazuh_api_url: str = DEFAULT_API_URL,
    wazuh_indexer_url: str = DEFAULT_INDEXER_URL,
    wazuh_indexer_user: str = DEFAULT_INDEXER_USER,
) -> FastAPI:
    """应用工厂（CLI 子代理按此契约接线，前四个参数签名不得偏离）。

    probe: 可注入对账回调 probe(task_id, step_id) -> True/False/None；
           默认保守返回 None（不可探测 → 高风险转 PAUSED）。
    wazuh_compose: Wazuh docker-compose.yml 路径（B4 安全中心）。凭据只在
           服务端运行时从该文件读取，绝不下发浏览器；None = 未配置，
           安全中心端点优雅降级。
    """
    if mode not in ("read-only", "full"):
        raise ValueError(f"mode must be 'read-only' or 'full', got {mode!r}")

    ledger = _ThreadSafeLedger(db_path)
    ledger.conn.executescript(CONSOLE_AUDIT_SCHEMA)
    ledger.conn.commit()
    _init_audit_chain(ledger.conn)  # 迁移旧库 + 起链（幂等）
    write_lock = threading.Lock()  # full 模式写路径串行化（单实例足够）
    _probe = probe or _default_probe
    limiter = _SlidingWindowLimiter(RATE_LIMIT_MAX_FAILURES, RATE_LIMIT_WINDOW_S)

    app = FastAPI(title="HongYun SuperOps Console", docs_url=None, redoc_url=None)
    app.state.mode = mode
    app.state.ledger = ledger
    app.state.modelhub_db = modelhub_db
    # 静态资源直出（vendor 三件套 + js/ 模块）：无构建链，pip 装完即用。
    # 鉴权中间件对 /static 豁免——内容全部是无敏感数据的公开前端资产。
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------- 鉴权 + 模式门（middleware，先 401 后 403） ----------
    class GateMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if path in AUTH_EXEMPT or path.startswith("/static"):
                return await call_next(request)
            # token 来源：cookie 或 X-SuperOps-Token 头，常量时间比较
            candidate = request.cookies.get(COOKIE_NAME) or request.headers.get(
                HEADER_NAME
            )
            if not token_equals(candidate, token):
                # token 校验失败按 IP 计入滑动窗口；超限直接 429（M3 限流）
                ip = _client_ip(request)
                if limiter.is_blocked(ip):
                    return JSONResponse({"error": "rate-limited"}, status_code=429)
                limiter.record_failure(ip)
                if path.startswith("/api/"):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return RedirectResponse("/gate", status_code=302)
            limiter.reset(_client_ip(request))
            # 鉴权通过后才检查模式：read-only 下写端点一律拒绝
            if (
                mode == "read-only"
                and request.method in WRITE_METHODS
                and path.startswith("/api/")
            ):
                return JSONResponse({"error": "mode=read-only"}, status_code=403)
            return await call_next(request)

    app.add_middleware(GateMiddleware)

    def audit(what: str, intent_id: str | None, params: dict) -> None:
        """full 模式写操作强制留痕：console_audit 一条，链式哈希防篡改。

        prev_hash 取链尾行 row_hash（空链用 GENESIS_HASH），插入后回填
        row_hash——两步在同一连接同一 commit 内完成，调用方持 write_lock
        保证单写者串行。
        """
        at = time.time()
        p = json.dumps(params, ensure_ascii=False)
        last = ledger.conn.execute(
            "SELECT row_hash FROM console_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = last[0] if last and last[0] else GENESIS_HASH
        cur = ledger.conn.execute(
            "INSERT INTO console_audit(who,what,intent_id,params,at,prev_hash) "
            "VALUES(?,?,?,?,?,?)",
            ("console", what, intent_id, p, at, prev),
        )
        rid = cur.lastrowid or 0  # AUTOINCREMENT 下必非 None，兜底给类型收窄
        row_hash = _row_hash(prev, rid, "console", what, intent_id, p, at)
        ledger.conn.execute(
            "UPDATE console_audit SET row_hash=? WHERE id=?", (row_hash, rid)
        )
        ledger.conn.commit()

    # ---------- 页面 ----------
    @app.get("/gate", include_in_schema=False)
    def gate_page():
        # token 路径由后端动态注入（占位符替换）——前端不硬编码文件名，
        # env SUPEROPS_TOKEN_FILE 或默认值变了，提示跟着变，杜绝口径漂移。
        html = (STATIC_DIR / "gate.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__TOKEN_PATH__", str(token_file_path()))
        )

    @app.get("/", include_in_schema=False)
    def index_page():
        return FileResponse(STATIC_DIR / "index.html")

    # ---------- 鉴权提交口 ----------
    async def safe_json_body(request: Request) -> dict | JSONResponse:
        """统一 JSON body 解析：非法 JSON / 非对象 body → 400，绝不让
        request.json() 的解析异常冒成 500（P2 健壮性）。返回 dict 或
        可直接 return 的 400 JSONResponse，调用方按类型分流。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        return body

    @app.post("/api/token")
    async def submit_token(request: Request):
        ip = _client_ip(request)
        # gate 是爆破面：超限直接 429，连 token 比较都不做（M3 限流）
        if limiter.is_blocked(ip):
            return JSONResponse({"error": "rate-limited"}, status_code=429)
        body = await safe_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        if not token_equals(str(body.get("token", "")), token):
            limiter.record_failure(ip)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        limiter.reset(ip)
        resp = JSONResponse({"ok": True})
        # httpOnly + sameSite=lax + 30 天：与内部版一致，浏览器脚本拿不到 token
        resp.set_cookie(
            COOKIE_NAME, token, httponly=True, samesite="lax", max_age=30 * 86400
        )
        return resp

    # ---------- 只读端点 ----------
    @app.get("/api/mode")
    def get_mode():
        return {"mode": mode}

    @app.get("/api/effects")
    def list_effects(state: str | None = None, limit: int = 100):
        limit = max(1, min(limit, 500))
        if state:
            rows = ledger.conn.execute(
                "SELECT * FROM effects WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = ledger.conn.execute(
                "SELECT * FROM effects ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"items": [_row_public(r) for r in rows], "count": len(rows)}

    @app.get("/api/effects/{intent_id}")
    def get_effect(intent_id: str):
        row = ledger._get(intent_id=intent_id)
        if row is None:
            return JSONResponse({"error": "intent not found"}, status_code=404)
        return {"effect": _row_public(row), "audit": ledger.audit_of(intent_id)}

    @app.get("/api/pending")
    def get_pending():
        uncertain = ledger.pending("UNCERTAIN")
        paused = ledger.pending("PAUSED")
        return {
            "uncertain": [
                _row_public(ledger._get(intent_id=r["intent_id"])) for r in uncertain
            ],
            "paused": [
                _row_public(ledger._get(intent_id=r["intent_id"])) for r in paused
            ],
            "counts": {"uncertain": len(uncertain), "paused": len(paused)},
        }

    @app.get("/api/audit")
    def get_console_audit(limit: int = 100):
        rows = ledger.conn.execute(
            "SELECT * FROM console_audit ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/audit/verify")
    def verify_console_audit():
        """防篡改全链校验：{valid, broken_at, checked}（M3）。"""
        return _verify_audit_chain(ledger.conn)

    @app.get("/api/graph/intent/{intent_id}")
    def get_intent_graph(intent_id: str):
        """G1 只读聚合端点（SUP-GUI-20261113 P0）：intent→step 因果 DAG。

        数据语义：effects 每行是任务(task_id)的一个 step；本端点以
        intent_id 定位其所属 task，返回该 task 全部 step 组成的 DAG：
        - 根节点 id="task:<task_id>"（kind=task），step 节点 id=intent_id
          （kind=step，含 state/risk/fencing_token 供前端状态色编码）；
        - 边按 created_at 升序串成因果链 root→最早 step→…→最新 step。
        图形层零特权：本端点纯只读，图上裁决仍走既有 /api/resolve、
        /api/reconcile 写端点（token 门 + 模式门 + 审计链照旧）。
        """
        row = ledger._get(intent_id=intent_id)
        if row is None:
            return JSONResponse({"error": "intent not found"}, status_code=404)
        task_id = row["task_id"]
        rows = ledger.conn.execute(
            "SELECT * FROM effects WHERE task_id=? ORDER BY created_at, rowid",
            (task_id,),
        ).fetchall()
        root_id = f"task:{task_id}"
        nodes: list[dict[str, Any]] = [
            {
                "id": root_id,
                "kind": "task",
                "label": task_id,
                "task_id": task_id,
                "step_count": len(rows),
            }
        ]
        edges: list[dict[str, str]] = []
        prev_id = root_id
        for r in rows:
            d = _row_public(r) or {}
            nodes.append(
                {
                    "id": d["intent_id"],
                    "kind": "step",
                    "label": d["step_id"],
                    "task_id": d["task_id"],
                    "step_id": d["step_id"],
                    "state": d["state"],
                    "risk": d["risk"],
                    "fencing_token": d["fencing_token"],
                    "created_at": d["created_at"],
                    "updated_at": d["updated_at"],
                }
            )
            edges.append({"from": prev_id, "to": d["intent_id"]})
            prev_id = d["intent_id"]
        return {
            "intent_id": intent_id,
            "task_id": task_id,
            "nodes": nodes,
            "edges": edges,
        }

    @app.get("/api/usage")
    def get_usage(day: str | None = None, provider: str | None = None):
        """modelhub 用量按日聚合。modelhub_db 未配置/表不存在时优雅降级。"""
        if not modelhub_db or not Path(modelhub_db).exists():
            return {"available": False, "items": []}
        conn = sqlite3.connect(modelhub_db)  # 普通连接：WAL 库禁 mode=ro（内部踩坑）
        try:
            has = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage'"
            ).fetchone()
            if not has:
                return {"available": True, "items": []}
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
            cur = conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            items = [dict(zip(cols, r)) for r in cur.fetchall()]
            return {"available": True, "items": items}
        finally:
            conn.close()

    # ---------- P1 只读聚合端点（SUP-GUI-20261113 G3/G4） ----------
    # 三端点全走既有 token 门（middleware），零新增写面；modelhub 未配置
    # 时一律优雅降级（configured=False + 中文引导），绝不报错。

    MH_UNCONFIGURED_HINT = (
        "未配置 modelhub：请在 ~/.superops/config.toml 的 [superops] 段添加 "
        'modelhub_db = "/绝对路径/modelhub.db"，然后重启 superops serve'
    )

    def _modelhub_open() -> sqlite3.Connection | None:
        """modelhub 库可用则打开（普通连接：WAL 库禁 mode=ro），否则 None。"""
        if not modelhub_db or not Path(modelhub_db).exists():
            return None
        conn = sqlite3.connect(modelhub_db)
        conn.row_factory = sqlite3.Row
        return conn

    def _mh_has_table(conn: sqlite3.Connection, name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )

    @app.get("/api/providers/topology")
    def get_providers_topology():
        """G3 provider 路由链拓扑（只读聚合）。

        - chain：回退链顺序，与 modelhub Router.candidates 同规则——
          仅 enabled 项，local 永远排 cloud 前，同级按 weight 降序；
        - probe：probe_cache 表最新真实探活结果（含 expired 标记），
          从未探活为 null —— 前端健康色必须来自这里，禁止自报绿。
        """
        conn = _modelhub_open()
        if conn is None:
            return {
                "configured": False,
                "reason": "modelhub_db 未配置或库文件不存在",
                "hint": MH_UNCONFIGURED_HINT,
                "chain": [],
                "providers": [],
            }
        try:
            if not _mh_has_table(conn, "providers"):
                return {
                    "configured": False,
                    "reason": "modelhub 库存在但无 providers 表",
                    "hint": MH_UNCONFIGURED_HINT,
                    "chain": [],
                    "providers": [],
                }
            probes: dict[str, dict] = {}
            if _mh_has_table(conn, "probe_cache"):
                for r in conn.execute(
                    "SELECT provider, ok, expires_at, result FROM probe_cache"
                ):
                    try:
                        d = json.loads(r["result"])
                    except (TypeError, ValueError):
                        continue  # 缓存行损坏按无探活结果处理，前端显示灰
                    d["expired"] = time.time() >= r["expires_at"]
                    probes[r["provider"]] = d
            providers: list[dict[str, Any]] = []
            for r in conn.execute(
                "SELECT name, kind, enabled, weight, config FROM providers "
                "ORDER BY name"
            ):
                try:
                    cfg = json.loads(r["config"])
                except (TypeError, ValueError):
                    cfg = {}
                providers.append(
                    {
                        "name": r["name"],
                        "kind": r["kind"],
                        "enabled": bool(r["enabled"]),
                        "weight": r["weight"],
                        "model": cfg.get("model"),
                        "base_url": cfg.get("base_url"),
                        "timeout": cfg.get("timeout", 30.0),
                        "price_input_per_1k": cfg.get("price_input_per_1k", 0.0),
                        "price_output_per_1k": cfg.get("price_output_per_1k", 0.0),
                        "probe": probes.get(r["name"]),
                    }
                )
            chain = [
                p["name"]
                for p in sorted(
                    (p for p in providers if p["enabled"]),
                    key=lambda p: (0 if p["kind"] == "local" else 1, -p["weight"]),
                )
            ]
            return {"configured": True, "chain": chain, "providers": providers}
        finally:
            conn.close()

    @app.get("/api/usage/cost")
    def get_usage_cost():
        """G3 成本聚合（只读）：按 provider 聚合 + 成本，附按日明细供画图。

        成本口径与 modelhub UsageStore.cost_summary 完全一致：
        cost = prompt_tokens/1000*input_per_1k + completion_tokens/1000*out_per_1k，
        单价取自 providers 表 config JSON，缺省/0 视为免费（本地端点默认）。
        """
        conn = _modelhub_open()
        if conn is None:
            return {
                "configured": False,
                "reason": "modelhub_db 未配置或库文件不存在",
                "hint": MH_UNCONFIGURED_HINT,
                "items": [],
                "daily": [],
                "total_cost": 0.0,
            }
        try:
            if not _mh_has_table(conn, "usage"):
                return {
                    "configured": True,
                    "items": [],
                    "daily": [],
                    "total_cost": 0.0,
                }
            prices: dict[str, dict] = {}
            if _mh_has_table(conn, "providers"):
                for r in conn.execute("SELECT name, config FROM providers"):
                    try:
                        cfg = json.loads(r["config"])
                    except (TypeError, ValueError):
                        continue
                    prices[r["name"]] = {
                        "input_per_1k": float(cfg.get("price_input_per_1k", 0.0)),
                        "output_per_1k": float(cfg.get("price_output_per_1k", 0.0)),
                    }
            cur = conn.execute("""SELECT provider,
                          COUNT(*) AS calls,
                          SUM(status='ok') AS ok_calls,
                          SUM(status='error') AS error_calls,
                          SUM(prompt_tokens) AS prompt_tokens,
                          SUM(completion_tokens) AS completion_tokens
                   FROM usage GROUP BY provider ORDER BY provider""")
            cols = [c[0] for c in cur.description]
            items = [dict(zip(cols, row)) for row in cur.fetchall()]
            for row in items:
                p = prices.get(row["provider"]) or {}
                row["cost"] = round(
                    row["prompt_tokens"] / 1000.0 * float(p.get("input_per_1k", 0.0))
                    + row["completion_tokens"]
                    / 1000.0
                    * float(p.get("output_per_1k", 0.0)),
                    10,
                )
            cur = conn.execute("""SELECT day, provider,
                          COUNT(*) AS calls,
                          SUM(status='error') AS error_calls,
                          SUM(prompt_tokens) AS prompt_tokens,
                          SUM(completion_tokens) AS completion_tokens
                   FROM usage GROUP BY day, provider ORDER BY day, provider""")
            cols = [c[0] for c in cur.description]
            daily = [dict(zip(cols, row)) for row in cur.fetchall()]
            return {
                "configured": True,
                "items": items,
                "daily": daily,
                "total_cost": round(sum(r["cost"] for r in items), 10),
            }
        finally:
            conn.close()

    @app.get("/api/stats/trend")
    def get_stats_trend(bucket: int = 86400, limit: int = 14):
        """G4 账本趋势（只读）：按时间桶统计 step 状态计数 + 审计事件数。

        - step 计数按 effects.created_at 落桶，state 为当前状态；
        - audit_events 按 console_audit.at 落桶（控制台写操作热度）；
        - 桶对齐到 bucket 秒边界，返回最近 limit 个桶（含空桶）。
        """
        bucket = max(60, min(bucket, 30 * 86400))
        limit = max(1, min(limit, 200))
        now = time.time()
        end = int(now // bucket) * bucket  # 当前桶起点
        starts = [end - (limit - 1 - i) * bucket for i in range(limit)]
        index = {s: i for i, s in enumerate(starts)}

        steps = [{"total": 0, "by_state": {}} for _ in range(limit)]
        for r in ledger.conn.execute("SELECT created_at, state FROM effects"):
            i = index.get(int(r["created_at"] // bucket) * bucket)
            if i is None:
                continue
            steps[i]["total"] += 1
            steps[i]["by_state"][r["state"]] = (
                steps[i]["by_state"].get(r["state"], 0) + 1
            )
        audits = [0] * limit
        if _mh_has_table(ledger.conn, "console_audit"):
            for r in ledger.conn.execute("SELECT at FROM console_audit"):
                i = index.get(int(r["at"] // bucket) * bucket)
                if i is not None:
                    audits[i] += 1
        return {
            "bucket_seconds": bucket,
            "generated_at": now,
            "total_effects": sum(s["total"] for s in steps),
            "buckets": [
                {
                    "start": s,
                    "end": s + bucket,
                    "steps_total": steps[i]["total"],
                    "steps_by_state": steps[i]["by_state"],
                    "audit_events": audits[i],
                }
                for i, s in enumerate(starts)
            ],
        }

    # ---------- B4 安全中心只读端点（Wazuh 代理） ----------
    # 凭据仅服务端持有（wazuh.py 运行时读 compose），响应里零凭据；
    # 全 GET 端点，read-only/full 模式门均不拦截，纯只读展示。
    wazuh: WazuhClient | None = None
    if wazuh_compose:
        wazuh = WazuhClient(
            wazuh_compose,
            api_url=wazuh_api_url,
            indexer_url=wazuh_indexer_url,
            indexer_user=wazuh_indexer_user,
        )

    @app.get("/api/security/fleet")
    def get_security_fleet():
        """舰队状态：Wazuh agents 摘要 + 明细（未配置优雅降级）。"""
        if wazuh is None:
            return {
                "available": False,
                "reason": "wazuh_compose 未配置",
                "hint": WAZUH_UNCONFIGURED_HINT,
                "summary": None,
                "agents": [],
            }
        return wazuh.fleet()

    @app.get("/api/security/alerts")
    def get_security_alerts(hours: int = 24, min_level: int = 7, size: int = 50):
        """近 N 小时高危告警（rule.level>=min_level，默认 24h/level≥7/前50条）。"""
        if wazuh is None:
            return {
                "available": False,
                "reason": "wazuh_compose 未配置",
                "hint": WAZUH_UNCONFIGURED_HINT,
                "items": [],
                "total": 0,
            }
        return wazuh.alerts(hours=hours, min_level=min_level, size=size)

    # ---------- 写端点（full 模式专属；read-only 已被 middleware 403 拦截） ----------
    @app.post("/api/reconcile")
    async def reconcile(request: Request):
        body = await safe_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        iid = str(body.get("intent_id", ""))
        row = ledger._get(intent_id=iid)
        if row is None:
            return JSONResponse({"error": "intent not found"}, status_code=404)
        if row["state"] != "UNCERTAIN":
            return JSONResponse(
                {"error": f"state={row['state']} is not UNCERTAIN"}, status_code=409
            )
        with write_lock:
            result = ledger.reconcile(iid, _probe)
            audit("reconcile", iid, {"intent_id": iid, "result": result.get("action")})
        return result

    @app.post("/api/resolve")
    async def resolve(request: Request):
        """PAUSED 人工裁决。

        - approve：人工确认放行重试 → 状态机映射为「重新 EXECUTE」：
          state=EXECUTING、fencing_token+1、续租（与 reconcile 判无效果后
          允许重试的语义一致，旧执行者的回执自然失效）。
        - reject：保持 PAUSED，仅记录裁决意见（不自动改状态，防止误操作）。
        """
        body = await safe_json_body(request)
        if isinstance(body, JSONResponse):
            return body
        iid = str(body.get("intent_id", ""))
        action = str(body.get("action", ""))
        note = str(body.get("note", ""))
        if action not in ("approve", "reject"):
            return JSONResponse(
                {"error": "action must be approve|reject"}, status_code=400
            )
        row = ledger._get(intent_id=iid)
        if row is None:
            return JSONResponse({"error": "intent not found"}, status_code=404)
        if row["state"] != "PAUSED":
            return JSONResponse(
                {"error": f"resolve applies to PAUSED only, state={row['state']}"},
                status_code=409,
            )
        with write_lock:
            now = time.time()
            if action == "approve":
                new_tok = row["fencing_token"] + 1
                ledger.conn.execute(
                    "UPDATE effects SET state='EXECUTING', fencing_token=?, "
                    "lease_expires_at=?, updated_at=? WHERE intent_id=?",
                    (new_tok, now + ledger.lease_seconds, now, iid),
                )
                ledger.conn.commit()
                ledger._audit(
                    iid, "CONSOLE_APPROVED_RETRY", {"note": note, "new_token": new_tok}
                )
                ledger.conn.commit()
                audit("resolve.approve", iid, {"note": note, "new_token": new_tok})
                return {
                    "action": "EXECUTE_ALLOWED",
                    "intent_id": iid,
                    "fencing_token": new_tok,
                    "note": "human approved — retry allowed with new fencing token",
                }
            # reject：保持 PAUSED，只记审计
            ledger._audit(iid, "CONSOLE_REJECTED", {"note": note})
            ledger.conn.commit()
            audit("resolve.reject", iid, {"note": note})
            return {
                "action": "STAY_PAUSED",
                "intent_id": iid,
                "note": note or "rejected — stays PAUSED",
            }

    @app.post("/api/providers/probe-all")
    def probe_all():
        """modelhub provider 全量健康实测（探活≠可用，真实推理在 modelhub 侧）。

        full 模式写操作之一：每次实测落 console_audit（含触发时间 at 与
        结果计数），与 resolve/reconcile 同一条哈希链，杜绝「探测无痕迹」。
        """
        if not modelhub_db or not Path(modelhub_db).exists():
            return JSONResponse(
                {"error": "modelhub_db not configured"}, status_code=404
            )
        try:
            from modelhub import (
                ModelHub,
            )  # 懒加载：未装 modelhub  extras 时不拖垮控制台
        except ImportError:
            return JSONResponse({"error": "modelhub not installed"}, status_code=501)
        hub = ModelHub(modelhub_db)
        try:
            results = hub.probe(force=True)
            with write_lock:
                audit(
                    "providers.probe_all",
                    None,
                    {
                        "probed": len(results),
                        "ok": sum(1 for v in results.values() if v.inference_ok),
                        "failed": sum(1 for v in results.values() if not v.inference_ok),
                    },
                )
            return {"items": {k: dataclasses.asdict(v) for k, v in results.items()}}
        finally:
            hub.close()

    return app
