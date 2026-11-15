"""SUP-GUI-20261113 P0 图形操作能力测试（G1 账本 DAG + G2 审计链）。

覆盖四组验收：
a. vendor manifest sha256 校验（篡改必须被检出）
b. 新聚合端点 GET /api/graph/intent/<id> 契约（含无 token 401 反例）
c. DAG 数据装配（多 step intent → nodes/edges 结构断言）
d. 审计 verify 数据（全链完好 / 篡改定位 broken_at）
"""

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from effect_ledger import EffectLedger
from fastapi.testclient import TestClient

from superops_console import create_app
from superops_console import app as app_module

TOKEN = "a" * 32
HDR = {"X-SuperOps-Token": TOKEN}
VENDOR_DIR = Path(app_module.__file__).parent / "static" / "vendor"


def _client(db, mode="read-only", probe=None):
    app = create_app(db, TOKEN, mode=mode, probe=probe)
    return TestClient(app)


# ---------- a. vendor manifest sha256 校验 ----------


def _manifest_mismatches(vendor_dir: Path) -> list[str]:
    """逐文件核对 vendor-manifest.json 的 sha256；返回不一致描述列表。

    缺失文件、哈希不符都算不一致——升级 vendor 库必须同步改 manifest，
    否则本测试（与 doctor 校验项）红。
    """
    manifest = json.loads((vendor_dir / "vendor-manifest.json").read_text())
    problems = []
    for name, meta in manifest["files"].items():
        fp = vendor_dir / name
        if not fp.is_file():
            problems.append(f"{name}: 文件缺失")
            continue
        actual = hashlib.sha256(fp.read_bytes()).hexdigest()
        if actual != meta["sha256"]:
            problems.append(
                f"{name}: sha256 不符（manifest={meta['sha256'][:12]}… 实际={actual[:12]}…）"
            )
    return problems


class TestVendorManifest:
    def test_vendor_files_match_manifest(self):
        """真实 vendor 目录必须与 manifest 逐文件哈希一致。"""
        assert _manifest_mismatches(VENDOR_DIR) == []

    def test_manifest_covers_all_js_files(self):
        """vendor 目录里不得有 manifest 未登记的 js（防夹带乱改）。"""
        manifest = json.loads((VENDOR_DIR / "vendor-manifest.json").read_text())
        extra = {p.name for p in VENDOR_DIR.glob("*.js")} - set(manifest["files"])
        assert extra == set()

    def test_tampered_vendor_detected(self, tmp_path):
        """篡改反例：改一个字节必须被校验函数检出。"""
        shutil.copytree(VENDOR_DIR, tmp_path / "v")
        target = tmp_path / "v" / "dagre.min.js"
        target.write_bytes(target.read_bytes() + b"/*tamper*/")
        problems = _manifest_mismatches(tmp_path / "v")
        assert len(problems) == 1 and "dagre.min.js" in problems[0]


# ---------- c. DAG 数据装配夹具 ----------


def _seed_multi_step(tmp_path):
    """一个 task 三个 step：s1/s2 CONFIRMED，s3 UNCERTAIN（回执丢失）。"""
    db = str(tmp_path / "dag.db")
    led = EffectLedger(db)
    ids = {}
    b1 = led.begin("task-dag", "s1-preflight", {"n": 1})
    led.complete(b1["intent_id"], b1["fencing_token"], {"ok": True})
    b2 = led.begin("task-dag", "s2-deploy", {"n": 2})
    led.complete(b2["intent_id"], b2["fencing_token"], {"ok": True})
    b3 = led.begin("task-dag", "s3-verify", {"n": 3})
    led.mark_receipt_lost(b3["intent_id"], b3["fencing_token"])
    ids = {"s1": b1["intent_id"], "s2": b2["intent_id"], "s3": b3["intent_id"]}
    led.close()
    return db, ids


@pytest.fixture
def dag(tmp_path):
    return _seed_multi_step(tmp_path)


# ---------- b. 聚合端点契约 ----------


class TestGraphEndpointContract:
    def test_requires_token_401(self, dag):
        """无 token 反例：新端点同样吃 GateMiddleware 的 401。"""
        db, _ = dag
        c = _client(db)
        r = c.get("/api/graph/intent/whatever")
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_unknown_intent_404(self, dag):
        db, _ = dag
        c = _client(db)
        r = c.get("/api/graph/intent/eff-nonexistent", headers=HDR)
        assert r.status_code == 404

    def test_response_shape(self, dag):
        """契约：{intent_id, task_id, nodes[], edges[]}，节点 id 唯一，
        边端点必须引用存在的节点 id。"""
        db, ids = dag
        c = _client(db)
        r = c.get(f"/api/graph/intent/{ids['s2']}", headers=HDR)
        assert r.status_code == 200
        d = r.json()
        assert d["intent_id"] == ids["s2"] and d["task_id"] == "task-dag"
        node_ids = [n["id"] for n in d["nodes"]]
        assert len(node_ids) == len(set(node_ids))
        id_set = set(node_ids)
        for e in d["edges"]:
            assert e["from"] in id_set and e["to"] in id_set

    def test_graph_endpoint_is_read_only_safe(self, dag):
        """图形层零特权：聚合端点 GET 在 read-only 模式正常可用（只读）。"""
        db, ids = dag
        c = _client(db, mode="read-only")
        assert c.get(f"/api/graph/intent/{ids['s1']}", headers=HDR).status_code == 200


# ---------- c. DAG 装配结构 ----------


class TestDagAssembly:
    def test_nodes_edges_structure(self, dag):
        db, ids = dag
        c = _client(db)
        d = c.get(f"/api/graph/intent/{ids['s3']}", headers=HDR).json()
        # 1 个 task 根节点 + 3 个 step 节点
        assert (
            d["nodes"][0]["kind"] == "task" and d["nodes"][0]["id"] == "task:task-dag"
        )
        steps = [n for n in d["nodes"] if n["kind"] == "step"]
        assert [n["id"] for n in steps] == [ids["s1"], ids["s2"], ids["s3"]]
        # 因果链：root→s1→s2→s3（created_at 升序）
        assert d["edges"] == [
            {"from": "task:task-dag", "to": ids["s1"]},
            {"from": ids["s1"], "to": ids["s2"]},
            {"from": ids["s2"], "to": ids["s3"]},
        ]

    def test_step_node_fields(self, dag):
        """step 节点带状态色编码所需字段：state/risk/fencing_token。"""
        db, ids = dag
        c = _client(db)
        d = c.get(f"/api/graph/intent/{ids['s3']}", headers=HDR).json()
        by_id = {n["id"]: n for n in d["nodes"]}
        assert by_id[ids["s1"]]["state"] == "CONFIRMED"
        assert by_id[ids["s3"]]["state"] == "UNCERTAIN"
        assert by_id[ids["s2"]]["label"] == "s2-deploy"
        assert by_id[ids["s2"]]["risk"] == "low"
        assert by_id[ids["s2"]]["fencing_token"] == 1

    def test_single_step_task_still_valid_dag(self, tmp_path):
        """单 step 任务：root→step 一条边，仍是合法 DAG。"""
        db = str(tmp_path / "one.db")
        led = EffectLedger(db)
        b = led.begin("task-solo", "only", {})
        led.close()
        c = _client(db)
        d = c.get(f"/api/graph/intent/{b['intent_id']}", headers=HDR).json()
        assert len(d["nodes"]) == 2 and len(d["edges"]) == 1


# ---------- d. 审计 verify 数据 ----------


def _seed_paused(tmp_path):
    """造一条 PAUSED（高风险不可探测）供 full 模式 resolve 产生审计行。"""
    db = str(tmp_path / "audit.db")
    led = EffectLedger(db)
    b = led.begin("task-a", "wire", {"amt": 1}, risk="high")
    led.mark_receipt_lost(b["intent_id"], b["fencing_token"])
    led.reconcile(b["intent_id"], lambda t, s: None)
    iid = b["intent_id"]
    led.close()
    return db, iid


class TestAuditVerifyData:
    def test_valid_chain_after_console_writes(self, tmp_path):
        """full 模式 resolve/reconcile 落审计行后，全链校验必须 valid。"""
        db, iid = _seed_paused(tmp_path)
        c = _client(db, mode="full")
        r = c.post(
            "/api/resolve",
            json={"intent_id": iid, "action": "reject", "note": "图上驳回"},
            headers=HDR,
        )
        assert r.status_code == 200
        v = c.get("/api/audit/verify", headers=HDR).json()
        assert v["valid"] is True and v["broken_at"] is None
        assert v["checked"] >= 1
        # 审计条目内容与图上裁决一致
        items = c.get("/api/audit?limit=10", headers=HDR).json()["items"]
        assert any(
            it["what"] == "resolve.reject" and it["intent_id"] == iid for it in items
        )

    def test_tampered_row_located_by_verify(self, tmp_path):
        """篡改反例：偷改中间行 params → verify 必须 broken 且 broken_at
        精确指向被篡改行（G2 红标定位的数据前提）。"""
        db, iid = _seed_paused(tmp_path)
        c = _client(db, mode="full")
        c.post(
            "/api/resolve",
            json={"intent_id": iid, "action": "reject", "note": "第一条"},
            headers=HDR,
        )
        c.post(
            "/api/resolve",
            json={"intent_id": iid, "action": "approve", "note": "第二条"},
            headers=HDR,
        )
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT id FROM console_audit ORDER BY id").fetchall()
        victim = rows[0][0]  # 篡改首行——其后所有行 prev_hash 衔接随之断裂
        conn.execute(
            "UPDATE console_audit SET params='{\"evil\":true}' WHERE id=?", (victim,)
        )
        conn.commit()
        conn.close()
        v = c.get("/api/audit/verify", headers=HDR).json()
        assert v["valid"] is False
        assert v["broken_at"]["id"] == victim
        assert v["broken_at"]["reason"] == "row_hash mismatch"

    def test_verify_empty_chain_valid(self, tmp_path):
        """空链语义：无审计行时 valid=True、checked=0（前端绿标不误报）。"""
        db = str(tmp_path / "empty.db")
        EffectLedger(db).close()
        c = _client(db)
        v = c.get("/api/audit/verify", headers=HDR).json()
        assert v == {"valid": True, "broken_at": None, "checked": 0}
