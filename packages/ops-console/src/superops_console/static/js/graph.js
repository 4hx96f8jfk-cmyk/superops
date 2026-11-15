// G1 账本图形视图（SUP-GUI-20261113 P0）：X6 + dagre，浏览器原生 ES module，无构建链。
// 图形操作零特权：图上所有写操作复用既有 /api/resolve、/api/reconcile 端点，
// read-only 模式按钮禁用；数据来自只读聚合端点 /api/graph/intent/<id>。
// P2：拖拽位置持久化（localStorage key=intent_id）、自绘 minimap、键盘快捷键、重置布局。
import {
  loadPos,
  clearPos,
  attachDragPersist,
  attachMiniMap,
  registerCanvas,
} from "./layout.js";
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
const fmt = (t) => new Date(t * 1000).toLocaleString("zh-CN");
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 401) {
    location.href = "/gate";
    throw new Error("401");
  }
  return r;
}
function toast(m) {
  const t = $("toast");
  if (!t) return;
  t.textContent = m;
  t.style.display = "block";
  setTimeout(() => (t.style.display = "none"), 2600);
}

// 状态色编码（与 index.html 表格徽章一致）
const STATE_COLORS = {
  CONFIRMED: "#3ecf8e",
  UNCERTAIN: "#e8b93d",
  PAUSED: "#ff6b6b",
  REJECTED: "#777777",
  EXECUTING: "#6ab0ff",
};
const NODE_W = 190,
  NODE_H = 62;

let MODE = "read-only";
let x6 = null; // X6.Graph 实例（懒建）
let currentIntent = null; // 侧栏当前 step
let currentGraph = null; // 最近一次 /api/graph 响应（重绘用）
let persistKey = null; // 当前图位置缓存 key（= intent_id，P2 拖拽持久化）

// ---------- intent 清单（按 task 分组，复用 /api/effects，零新增写面） ----------
async function loadIntentList() {
  const d = await (await api("/api/effects?limit=200")).json();
  const groups = new Map();
  for (const e of d.items) {
    if (!groups.has(e.task_id)) groups.set(e.task_id, []);
    groups.get(e.task_id).push(e);
  }
  const list = $("intentList");
  if (!groups.size) {
    list.innerHTML = '<div class="empty">账本为空</div>';
    return;
  }
  const entries = [...groups.entries()]
    .map(([tid, steps]) => {
      steps.sort((a, b) => a.created_at - b.created_at);
      return [tid, steps, steps[steps.length - 1]];
    })
    .sort((a, b) => b[2].updated_at - a[2].updated_at);
  list.innerHTML = entries
    .map(
      ([tid, steps, last]) => `
    <div class="intentItem" data-iid="${esc(last.intent_id)}">
      <div class="intentName">${esc(tid)}</div>
      <div class="intentMeta">${steps.length} 步 · ${fmt(last.updated_at)}</div>
      <div class="dots">${steps
        .map(
          (s) =>
            `<span class="dot" style="background:${STATE_COLORS[s.state] || "#555"}" title="${esc(s.step_id)}: ${esc(s.state)}"></span>`,
        )
        .join("")}</div>
    </div>`,
    )
    .join("");
  list.querySelectorAll(".intentItem").forEach((el) =>
    el.addEventListener("click", () => {
      list
        .querySelectorAll(".intentItem")
        .forEach((x) => x.classList.remove("sel"));
      el.classList.add("sel");
      renderGraph(el.dataset.iid);
    }),
  );
}

// ---------- DAG 渲染（dagre 分层布局 + X6） ----------
async function renderGraph(intentId) {
  const d = await (
    await api("/api/graph/intent/" + encodeURIComponent(intentId))
  ).json();
  if (d.error) {
    toast(d.error);
    return;
  }
  currentGraph = d;
  persistKey = d.intent_id || intentId;
  closePanel();
  renderFallbackList(d);
  // P2：键盘快捷键与重置按钮在窄屏也要生效（Esc 关底部抽屉），注册先于窄屏早退
  registerCanvas("graph", {
    el: $("tab-graph"),
    getGraph: () => x6,
    onClose: closePanel,
    fit: () => x6 && x6.zoomToFit({ padding: 24, maxScale: 1 }),
  });
  $("resetLayoutBtn").onclick = () => {
    if (!persistKey) return;
    clearPos(persistKey);
    toast("已清除位置缓存，回到自动布局");
    renderGraph(persistKey);
  };
  if (window.innerWidth <= 760) return; // 窄屏降级为列表，不建画布
  const container = $("graphCanvas");
  if (!x6) {
    x6 = new X6.Graph({
      container,
      // X6 3.x 默认异步渲染在直出页面下不落地（实测 DOM 空），账本规模小，同步渲染
      async: false,
      interacting: { nodeMovable: true, edgeMovable: false },
      panning: { enabled: true },
      mousewheel: { enabled: true, modifiers: ["ctrl", "meta"] },
      background: "#151515",
    });
    x6.on("node:click", ({ node }) => {
      const data = node.getData() || {};
      if (data.kind === "step") openPanel(data.id);
    });
    // P2：拖拽后把全图节点位置写入 localStorage（key 随 intent 切换）
    attachDragPersist(x6, () => persistKey);
    attachMiniMap(container, x6);
  }
  x6.clearCells();
  // dagre 分层布局（LR），X6 节点坐标取左上角；有 localStorage 位置缓存则覆盖（P2）
  const gl = new dagre.graphlib.Graph();
  gl.setGraph({
    rankdir: "LR",
    nodesep: 36,
    ranksep: 90,
    marginx: 24,
    marginy: 24,
  });
  gl.setDefaultEdgeLabel(() => ({}));
  d.nodes.forEach((n) => gl.setNode(n.id, { width: NODE_W, height: NODE_H }));
  d.edges.forEach((e) => gl.setEdge(e.from, e.to));
  dagre.layout(gl);
  const saved = loadPos(persistKey);
  for (const n of d.nodes) {
    const p = gl.node(n.id);
    const sp = saved && saved[n.id];
    const isTask = n.kind === "task";
    const color = isTask ? "#C8102E" : STATE_COLORS[n.state] || "#666";
    x6.addNode({
      id: n.id,
      shape: "rect",
      x: sp ? sp.x : p.x - NODE_W / 2,
      y: sp ? sp.y : p.y - NODE_H / 2,
      width: NODE_W,
      height: NODE_H,
      attrs: {
        rect: {
          fill: "#222",
          stroke: color,
          strokeWidth: isTask ? 2.5 : 2,
          rx: 10,
          ry: 10,
        },
        label: {
          text: isTask ? `任务\n${n.label}` : `${n.step_id}\n${n.state}`,
          fill: "#eaeaea",
          fontSize: 12,
          textWrap: { width: NODE_W - 20, height: NODE_H - 12, ellipsis: true },
        },
      },
      data: n,
    });
  }
  for (const e of d.edges) {
    x6.addEdge({
      source: e.from,
      target: e.to,
      attrs: {
        line: {
          stroke: "#5a5a5a",
          strokeWidth: 1.6,
          targetMarker: { name: "block", width: 10, height: 7 },
        },
      },
      connector: { name: "rounded" },
    });
  }
  x6.zoomToFit({ padding: 24, maxScale: 1 });
}

// 窄屏降级列表（G6：移动端不破版）
function renderFallbackList(d) {
  $("graphFallback").innerHTML = d.nodes
    .filter((n) => n.kind === "step")
    .map(
      (n) => `
    <div class="fbItem" data-iid="${esc(n.id)}">
      <span class="st ${esc(n.state)}">${esc(n.state)}</span>
      <b>${esc(n.step_id)}</b> <span class="note">风险 ${esc(n.risk)} · ${fmt(n.updated_at)}</span>
    </div>`,
    )
    .join("");
  $("graphFallback")
    .querySelectorAll(".fbItem")
    .forEach((el) =>
      el.addEventListener("click", () => openPanel(el.dataset.iid)),
    );
}

// ---------- 侧栏详情 + 图上裁决 ----------
async function openPanel(iid) {
  currentIntent = iid;
  const d = await (await api("/api/effects/" + encodeURIComponent(iid))).json();
  if (d.error) {
    toast(d.error);
    return;
  }
  const e = d.effect;
  const ro = MODE !== "full";
  const why = ro ? "read-only 模式禁止写操作" : "";
  const canResolve = !ro && e.state === "PAUSED";
  const canReconcile = !ro && e.state === "UNCERTAIN";
  const tip = (ok, need) => (ok ? "" : why || `仅 ${need} 状态可执行`);
  $("panel").innerHTML = `
    <div class="panelHead"><b>${esc(e.step_id)}</b>
      <button class="gray" id="panelClose" style="float:right">✕</button></div>
    <div><span class="st ${esc(e.state)}">${esc(e.state)}</span>
      <span class="note">风险 ${esc(e.risk)} · token ${e.fencing_token}</span></div>
    <div class="note">intent <code>${esc(e.intent_id)}</code><br>task ${esc(e.task_id)}
      <br>创建 ${fmt(e.created_at)} · 更新 ${fmt(e.updated_at)}</div>
    <h4>回执 / 对账（payload）</h4>
    <pre>${esc(JSON.stringify({ receipt: e.receipt ?? null, reconcile: e.reconcile ?? null }, null, 2))}</pre>
    <h4>事件流（audit_log）</h4>
    <pre>${(d.audit || []).map((a) => `${fmt(a.at)}  ${a.event}${a.detail ? "  " + a.detail : ""}`).join("\n") || "（无）"}</pre>
    <div class="panelActions">
      <button id="btnApprove" ${canResolve ? "" : "disabled"} title="${tip(canResolve, "PAUSED")}">放行重试</button>
      <button class="gray" id="btnReject" ${canResolve ? "" : "disabled"} title="${tip(canResolve, "PAUSED")}">驳回</button>
      <button id="btnRetry" ${canReconcile ? "" : "disabled"} title="${tip(canReconcile, "UNCERTAIN")}">对账重试</button>
    </div>
    ${ro ? '<div class="note">⚠ 当前为 read-only 模式，图上操作按钮已禁用。</div>' : ""}`;
  $("panel").style.display = "block";
  $("panelClose").onclick = closePanel;
  $("btnApprove").onclick = () => doResolve("approve");
  $("btnReject").onclick = () => doResolve("reject");
  $("btnRetry").onclick = doReconcile;
}
function closePanel() {
  currentIntent = null;
  $("panel").style.display = "none";
}

// 图上裁决：与总览表单同一端点、同一审计链（图形层零特权）
async function doResolve(action) {
  if (!currentIntent) return;
  const note = action === "approve" ? "账本图人工放行" : "账本图人工驳回";
  const r = await api("/api/resolve", {
    method: "POST",
    body: JSON.stringify({ intent_id: currentIntent, action, note }),
  });
  toast((await r.json()).action || "HTTP " + r.status);
  await afterAction();
}
async function doReconcile() {
  if (!currentIntent) return;
  const r = await api("/api/reconcile", {
    method: "POST",
    body: JSON.stringify({ intent_id: currentIntent }),
  });
  toast((await r.json()).action || "HTTP " + r.status);
  await afterAction();
}
async function afterAction() {
  if (currentGraph) renderGraph(currentGraph.intent_id);
  loadIntentList();
  if (window.refreshOverview) window.refreshOverview(); // 总览同步刷新（若已加载）
}

// ---------- 入口（由 index.html 页签切换调用） ----------
export async function activate() {
  MODE = (await (await api("/api/mode")).json()).mode;
  loadIntentList();
  if (!currentGraph) {
    // 默认打开最近更新的一个 intent，页面打开即有图
    const d = await (await api("/api/effects?limit=1")).json();
    if (d.count) renderGraph(d.items[0].intent_id);
  }
}
window.__graphActivate = activate;
