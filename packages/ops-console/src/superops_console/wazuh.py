"""Wazuh 安全中心只读代理（部署批次 B4：部署自带 SIEM 可视化）。

安全铁律：
- 凭据仅服务端持有：API_USERNAME / API_PASSWORD / INDEXER_PASSWORD 在部署侧
  docker-compose.yml（路径由 config 的 wazuh_compose 指定），运行时读取，
  绝不下发浏览器、绝不写入日志或响应。
- 纯只读：只调 Wazuh API 的 GET /agents* 与 Indexer 的 _search，零写面；
  对控制台侧暴露的端点全是 GET，天然不受 full/read-only 模式门影响。
- 自签 TLS：Wazuh API(:55002) 与 Indexer(:9200) 均为自签证书且只绑
  127.0.0.1 回环，禁用证书校验（Indexer 必须 https，http 会被拒）。
- 优雅降级：上游不可达/未配置一律 {available: False, reason/hint}，
  绝不 500——安全面板挂了不能拖垮控制台其他视图。
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "https://127.0.0.1:55002"
DEFAULT_INDEXER_URL = "https://127.0.0.1:9200"
DEFAULT_INDEXER_USER = "admin"

# Wazuh JWT 默认 TTL 900s，提前到 600s 续期，避免边界上 401 竞态
_JWT_TTL_S = 600.0
_TIMEOUT_S = 10.0

UNCONFIGURED_HINT = (
    "未配置 Wazuh：请在 ~/.superops/config.toml 的 [superops] 段添加 "
    'wazuh_compose = "/绝对路径/wazuh-docker/single-node/docker-compose.yml"，'
    "然后重启 superops serve"
)


def _unverified_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def parse_compose_credentials(compose_path: str | Path) -> dict[str, str]:
    """从 docker-compose.yml 的 environment 行提取三个凭据键。

    只认 ``KEY=value`` 形态（Wazuh 官方 single-node compose 即此格式）；
    缺任一键抛 ValueError，调用方降级为「未配置/配置不完整」。
    """
    text = Path(compose_path).expanduser().read_text(encoding="utf-8")
    creds: dict[str, str] = {}
    for key in ("API_USERNAME", "API_PASSWORD", "INDEXER_PASSWORD"):
        m = re.search(rf"^\s*-?\s*{key}=(\S+)\s*$", text, re.MULTILINE)
        if not m:
            raise ValueError(f"compose 文件缺少 {key}")
        creds[key] = m.group(1)
    return creds


def _https_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """单次 HTTPS JSON 请求（自签证书放行）。异常原样上抛，由调用方降级。"""
    req = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method or ("POST" if body is not None else "GET")
    )
    with urllib.request.urlopen(req, context=_unverified_ssl_context(), timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WazuhClient:
    """Wazuh Manager API + Indexer 的只读客户端（线程安全，JWT 带缓存）。"""

    def __init__(
        self,
        compose_path: str | Path,
        api_url: str = DEFAULT_API_URL,
        indexer_url: str = DEFAULT_INDEXER_URL,
        indexer_user: str = DEFAULT_INDEXER_USER,
    ):
        self.compose_path = Path(compose_path).expanduser()
        self.api_url = api_url.rstrip("/")
        self.indexer_url = indexer_url.rstrip("/")
        self.indexer_user = indexer_user
        self._lock = threading.Lock()
        self._jwt: str | None = None
        self._jwt_at = 0.0

    # ---------- 凭据与鉴权 ----------
    def _creds(self) -> dict[str, str]:
        return parse_compose_credentials(self.compose_path)

    def _api_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._jwt and time.time() - self._jwt_at < _JWT_TTL_S:
                return self._jwt
            creds = self._creds()
            basic = base64.b64encode(
                f"{creds['API_USERNAME']}:{creds['API_PASSWORD']}".encode()
            ).decode()
            data = _https_json(
                f"{self.api_url}/security/user/authenticate",
                headers={"Authorization": f"Basic {basic}"},
                body=b"",
                method="POST",
            )
            self._jwt = str(data["data"]["token"])
            self._jwt_at = time.time()
            return self._jwt

    def _api_get(self, path: str) -> dict[str, Any]:
        try:
            return _https_json(
                f"{self.api_url}{path}",
                headers={"Authorization": f"Bearer {self._api_token()}"},
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:  # token 过期竞态：强制续期重试一次
                return _https_json(
                    f"{self.api_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_token(force=True)}"},
                )
            raise

    def _indexer_search(self, index: str, body: dict) -> dict[str, Any]:
        creds = self._creds()
        basic = base64.b64encode(
            f"{self.indexer_user}:{creds['INDEXER_PASSWORD']}".encode()
        ).decode()
        return _https_json(
            f"{self.indexer_url}/{index}/_search",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {basic}",
            },
            body=json.dumps(body).encode(),
            method="POST",
        )

    # ---------- 对外聚合（降级语义统一） ----------
    @staticmethod
    def _unavailable(exc: Exception) -> dict[str, Any]:
        return {
            "available": False,
            "reason": f"Wazuh 上游不可达或响应异常: {type(exc).__name__}",
            "hint": "确认 wazuh-docker 容器在跑、compose 凭据有效",
        }

    def fleet(self) -> dict[str, Any]:
        """舰队状态：/agents/summary/status 摘要 + /agents 明细列表。"""
        try:
            summary = self._api_get("/agents/summary/status")["data"]["connection"]
            agents_raw = self._api_get("/agents?pretty=false&limit=500")["data"][
                "affected_items"
            ]
        except Exception as exc:  # noqa: BLE001 —— 降级不 500 是设计
            return self._unavailable(exc)
        agents = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "ip": a.get("ip"),
                "status": a.get("status"),
                "version": a.get("version") or "—",
                "node_name": a.get("node_name"),
                "last_keep_alive": a.get("lastKeepAlive"),
            }
            for a in agents_raw
        ]
        return {
            "available": True,
            "summary": {
                "active": summary.get("active", 0),
                "pending": summary.get("pending", 0),
                "disconnected": summary.get("disconnected", 0),
                "never_connected": summary.get("never_connected", 0),
                "total": summary.get("total", 0),
            },
            "agents": agents,
            "generated_at": time.time(),
        }

    def alerts(self, hours: int = 24, min_level: int = 7, size: int = 50) -> dict[str, Any]:
        """近 N 小时高危告警：indexer wazuh-alerts-* 查 rule.level>=min_level。

        rule.description 不可 terms 聚合是已知限制，这里只做排序取明细，
        不做聚合，无此约束。
        """
        hours = max(1, min(hours, 168))
        min_level = max(1, min(min_level, 15))
        size = max(1, min(size, 200))
        body = {
            "size": size,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"timestamp": {"gte": f"now-{hours}h", "lte": "now"}}},
                        {"range": {"rule.level": {"gte": min_level}}},
                    ]
                }
            },
            "_source": [
                "timestamp",
                "agent.id",
                "agent.name",
                "rule.id",
                "rule.level",
                "rule.description",
            ],
        }
        try:
            resp = self._indexer_search("wazuh-alerts-*", body)
        except Exception as exc:  # noqa: BLE001
            return self._unavailable(exc)
        hits = resp.get("hits", {})
        items = []
        for h in hits.get("hits", []):
            s = h.get("_source", {})
            rule = s.get("rule", {})
            agent = s.get("agent", {})
            items.append(
                {
                    "time": s.get("timestamp"),
                    "agent_id": agent.get("id"),
                    "agent_name": agent.get("name"),
                    "rule_id": rule.get("id"),
                    "level": rule.get("level"),
                    "description": rule.get("description"),
                }
            )
        total = hits.get("total")
        return {
            "available": True,
            "window_hours": hours,
            "min_level": min_level,
            "total": total.get("value") if isinstance(total, dict) else total,
            "items": items,
            "generated_at": time.time(),
        }
