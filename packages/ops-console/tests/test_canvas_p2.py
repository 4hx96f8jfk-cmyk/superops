"""SUP-GUI-20261113 P2 画布体验 + 移动端精修测试。

pytest 只能做静态契约断言（前端行为本身——localStorage 读写、X6 拖拽、键盘事件——
由浏览器无头实测覆盖，见工单验收记录）：
a. layout.js 共享模块存在且包含位置持久化 / 自绘 minimap / 键盘快捷键三块实现要素
b. graph.js / model.js 接线（import layout.js、重置布局、持久化 key 设计）
c. index.html 静态结构：重置布局按钮 ×2、快捷键说明、窄屏底部抽屉 CSS、页签横滑 CSS
d. vendor 目录未被 P2 引入新库（minimap 插件 3.0.0 无 UMD JS 产物，选型为自绘）——manifest 仍三件套
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from superops_console import create_app
from superops_console import app as app_module

TOKEN = "b" * 32
HDR = {"X-SuperOps-Token": TOKEN}
STATIC_DIR = Path(app_module.__file__).parent / "static"


def _read(p: str) -> str:
    return (STATIC_DIR / p).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(Path(":memory:"), TOKEN, mode="read-only"))


class TestLayoutModule:
    def test_layout_js_exists_with_storage_prefix(self):
        """共享模块存在，localStorage key 前缀钉死为 superops.layout.。"""
        src = _read("js/layout.js")
        assert 'PREFIX = "superops.layout."' in src

    def test_layout_js_drag_persist(self):
        """拖拽持久化：node:moved 监听 + load/save/clear 三函数导出。"""
        src = _read("js/layout.js")
        for token in ("node:moved", "export function loadPos", "export function savePos", "export function clearPos"):
            assert token in src, token

    def test_layout_js_selfdrawn_minimap(self):
        """minimap 为自绘 canvas 缩览（插件 3.x 无 UMD 产物），含视口矩形与点击跳转。"""
        src = _read("js/layout.js")
        for token in ("x6-minimap", "getContentBBox", "centerPoint", "pointerdown"):
            assert token in src, token

    def test_layout_js_keyboard_shortcuts(self):
        """键盘快捷键：Esc/+/-/0/方向键齐备，且焦点在输入框时不拦截。"""
        src = _read("js/layout.js")
        for token in ('"Escape"', '"+"', '"-"', '"0"', "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "zoomToFit", "translateBy"):
            assert token in src, token
        assert 'tagName === "INPUT"' in src  # 输入框守卫

    def test_layout_js_served(self, client):
        r = client.get("/static/js/layout.js", headers=HDR)
        assert r.status_code == 200
        assert "superops.layout." in r.text


class TestGraphWiring:
    def test_graph_js_uses_layout(self):
        src = _read("js/graph.js")
        assert 'from "./layout.js"' in src
        assert "attachDragPersist" in src and "attachMiniMap" in src and "registerCanvas" in src

    def test_graph_js_position_override_and_reset(self):
        """DAG 渲染读位置缓存覆盖 dagre；重置按钮清缓存回自动布局。key = intent_id。"""
        src = _read("js/graph.js")
        assert "loadPos(persistKey)" in src
        assert "clearPos(persistKey)" in src
        assert "resetLayoutBtn" in src
        assert "persistKey = d.intent_id" in src

    def test_model_js_uses_layout_topology_key(self):
        """拓扑图拖拽持久化 key 固定为 topology，且开启 nodeMovable。"""
        src = _read("js/model.js")
        assert 'from "./layout.js"' in src
        assert 'loadPos("topology")' in src
        assert 'clearPos("topology")' in src
        assert "nodeMovable: true" in src
        assert "resetTopoBtn" in src


class TestIndexHtmlStructure:
    def test_reset_buttons_present(self, client):
        html = client.get("/", headers=HDR).text
        assert 'id="resetLayoutBtn"' in html
        assert 'id="resetTopoBtn"' in html
        assert html.count("重置布局") >= 2

    def test_keyboard_hint_present(self):
        html = _read("index.html")
        assert "Esc" in html and "方向键平移" in html

    def test_mobile_bottom_sheet_css(self):
        """窄屏侧栏改 fixed 底部抽屉（bottom sheet），页签可横滑。"""
        html = _read("index.html")
        mq = html.split("@media (max-width:760px){", 1)[1]
        assert "position:fixed" in mq and "bottom:0" in mq
        assert "overflow-x:auto" in html  # nav.tabs 横滑 + 表格内滚

    def test_minimap_css(self):
        html = _read("index.html")
        assert ".x6-minimap" in html


class TestVendorUnchanged:
    def test_no_new_vendor_lib_for_minimap(self):
        """P2 未引入 minimap 插件（3.0.0 无 JS 产物），vendor 仍为三件套。"""
        manifest = json.loads((STATIC_DIR / "vendor" / "vendor-manifest.json").read_text())
        assert set(manifest["files"]) == {"x6.min.js", "echarts.min.js", "dagre.min.js"}
