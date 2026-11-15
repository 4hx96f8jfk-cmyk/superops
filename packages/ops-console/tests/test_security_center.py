"""B4 安全中心（Wazuh 只读代理）测试。

覆盖验收：
a. /api/security/{fleet,alerts} 无 token 一律 401（token 门复用反例）。
b. 未配置 wazuh_compose → available=False + 中文引导，不报错。
c. fleet/alerts 正常态：mock 上游 HTTP 层（_https_json），断言凭据解析、
   JWT 流程（Basic 换 token → Bearer 调用）、响应整形与 Indexer 查询体
   （now-24h / rule.level>=7 / size / 排序）。
d. 上游不可达 → available=False 优雅降级（200 不 500）。
e. read-only 模式下 GET 照常放行（模式门不影响纯只读面板）。
f. compose 缺文件/缺键 → 降级不炸。
"""

import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

import superops_console.wazuh as wazuh_mod
from superops_console import create_app

TOKEN = "b" * 32
HDR = {"X-SuperOps-Token": TOKEN}
ENDPOINTS = ["/api/security/fleet", "/api/security/alerts"]

FAKE_COMPOSE = """
services:
  wazuh.manager:
    environment:
      - API_USERNAME=fake_test_user
      - API_PASSWORD=fake_tes…word
      - INDEXER_PASSWORD=fake_t…word
"""


@pytest.fixture
def compose(tmp_path):
    p = tmp_path / "docker-compose.yml"
    p.write_text(FAKE_COMPOSE, encoding="utf-8")
    return str(p)


def _app(tmp_path, mode="full", wazuh_compose=None):
    return create_app(
        db_path=str(tmp_path / "effects.db"),
        token=TOKEN,
        mode=mode,
        wazuh_compose=wazuh_compose,
    )


# ---------- a. token 门 ----------


def test_security_endpoints_require_token(tmp_path, compose):
    client = TestClient(_app(tmp_path, wazuh_compose=compose))
    for ep in ENDPOINTS:
        assert client.get(ep).status_code == 401


# ---------- b. 未配置降级 ----------


def test_unconfigured_degrades(tmp_path):
    client = TestClient(_app(tmp_path))  # 不传 wazuh_compose
    for ep in ENDPOINTS:
        d = client.get(ep, headers=HDR).json()
        assert d["available"] is False
        assert "wazuh" in d["hint"] or "Wazuh" in d["hint"]


# ---------- c. 正常态（mock 上游 HTTP 层） ----------


FLEET_ROUTES = {
    "security/user/authenticate": {"data": {"token": "fake.jwt.token"}},
    "agents/summary/status": {
        "data": {
            "connection": {
                "active": 3,
                "pending": 0,
                "disconnected": 0,
                "never_connected": 2,
                "total": 5,
            }
        }
    },
    "agents?pretty=false": {
        "data": {
            "affected_items": [
                {
                    "id": "002",
                    "name": "host10",
                    "ip": "127.0.0.1",
                    "status": "active",
                    "version": "Wazuh v4.14.9",
                    "node_name": "node01",
                    "lastKeepAlive": "2026-11-15T00:00:00Z",
                }
            ]
        }
    },
}


def test_fleet_parses_compose_and_shapes_response(tmp_path, compose, monkeypatch):
    calls = []

    def fake(url, *, headers=None, body=None, method=None):
        calls.append((url, dict(headers or {})))
        for key, payload in FLEET_ROUTES.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(wazuh_mod, "_https_json", fake)
    client = TestClient(_app(tmp_path, wazuh_compose=compose))
    d = client.get("/api/security/fleet", headers=HDR).json()
    assert d["available"] is True
    assert d["summary"] == {
        "active": 3,
        "pending": 0,
        "disconnected": 0,
        "never_connected": 2,
        "total": 5,
    }
    assert d["agents"][0]["name"] == "host10"
    assert d["agents"][0]["last_keep_alive"] == "2026-11-15T00:00:00Z"
    # JWT 流程：首呼 Basic 认证换 token，后续业务调用必须 Bearer
    assert any("Basic " in h.get("Authorization", "") for _, h in calls)
    assert any("Bearer fake.jwt" in h.get("Authorization", "") for _, h in calls)
    # 响应零凭据：compose 里的假凭据值绝不出现在响应 JSON
    assert "fake_test_password" not in json.dumps(d)
    assert "fake_test_user" not in json.dumps(d)


def test_alerts_query_shape_and_items(tmp_path, compose, monkeypatch):
    captured = {}

    def fake(url, *, headers=None, body=None, method=None):
        if "_search" in url:
            captured["url"] = url
            captured["body"] = json.loads(body)
            captured["auth"] = (headers or {}).get("Authorization", "")
            return {
                "hits": {
                    "total": {"value": 277},
                    "hits": [
                        {
                            "_source": {
                                "timestamp": "2026-11-14T20:55:10.130+0000",
                                "agent": {"id": "005", "name": "host168"},
                                "rule": {
                                    "id": "510",
                                    "level": 7,
                                    "description": "Host-based anomaly detection event (rootcheck).",
                                },
                            }
                        }
                    ],
                }
            }
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(wazuh_mod, "_https_json", fake)
    client = TestClient(_app(tmp_path, wazuh_compose=compose))
    d = client.get("/api/security/alerts", headers=HDR).json()
    assert d["available"] is True
    assert d["total"] == 277
    assert d["items"][0] == {
        "time": "2026-11-14T20:55:10.130+0000",
        "agent_id": "005",
        "agent_name": "host168",
        "rule_id": "510",
        "level": 7,
        "description": "Host-based anomaly detection event (rootcheck).",
    }
    # Indexer 查询体：默认 24h 窗口 + level>=7 + size 50 + 时间倒序
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"range": {"timestamp": {"gte": "now-24h", "lte": "now"}}} in q
    assert {"range": {"rule.level": {"gte": 7}}} in q
    assert captured["body"]["size"] == 50
    assert captured["body"]["sort"] == [{"timestamp": {"order": "desc"}}]
    assert captured["url"].endswith("/wazuh-alerts-*/_search")
    assert captured["auth"].startswith("Basic ")
    assert "fake_test_indexer" not in json.dumps(d)


def test_alerts_params_clamped(tmp_path, compose, monkeypatch):
    captured = {}

    def fake(url, *, headers=None, body=None, method=None):
        captured["body"] = json.loads(body)
        return {"hits": {"total": {"value": 0}, "hits": []}}

    monkeypatch.setattr(wazuh_mod, "_https_json", fake)
    client = TestClient(_app(tmp_path, wazuh_compose=compose))
    d = client.get(
        "/api/security/alerts?hours=99999&min_level=99&size=9999", headers=HDR
    ).json()
    assert d["window_hours"] == 168
    assert d["min_level"] == 15
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"range": {"timestamp": {"gte": "now-168h", "lte": "now"}}} in q
    assert {"range": {"rule.level": {"gte": 15}}} in q
    assert captured["body"]["size"] == 200


# ---------- d. 上游不可达降级 ----------


def test_upstream_down_degrades(tmp_path, compose, monkeypatch):
    def dead(url, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(wazuh_mod, "_https_json", dead)
    client = TestClient(_app(tmp_path, wazuh_compose=compose))
    for ep in ENDPOINTS:
        r = client.get(ep, headers=HDR)
        assert r.status_code == 200
        d = r.json()
        assert d["available"] is False
        assert "不可达" in d["reason"] or "异常" in d["reason"]


# ---------- e. read-only 模式放行 GET ----------


def test_readonly_mode_allows_security_gets(tmp_path, compose, monkeypatch):
    def fake(url, *, headers=None, body=None, method=None):
        for key, payload in FLEET_ROUTES.items():
            if key in url:
                return payload
        return {"hits": {"total": {"value": 0}, "hits": []}}

    monkeypatch.setattr(wazuh_mod, "_https_json", fake)
    client = TestClient(_app(tmp_path, mode="read-only", wazuh_compose=compose))
    for ep in ENDPOINTS:
        assert client.get(ep, headers=HDR).status_code == 200


# ---------- f. compose 异常 ----------


def test_missing_compose_file_degrades(tmp_path):
    client = TestClient(
        _app(tmp_path, wazuh_compose=str(tmp_path / "nope" / "docker-compose.yml"))
    )
    for ep in ENDPOINTS:
        r = client.get(ep, headers=HDR)
        assert r.status_code == 200
        assert r.json()["available"] is False


def test_compose_missing_key_raises(tmp_path):
    bad = tmp_path / "docker-compose.yml"
    bad.write_text(
        "services:\n  x:\n    environment:\n"
        "      - API_USERNAME=fake_test_user\n"
        "      - API_PASSWORD=fake_tes…word\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="INDEXER_PASSWORD"):
        wazuh_mod.parse_compose_credentials(bad)
