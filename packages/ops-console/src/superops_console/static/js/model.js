// G3 模型接入页签（SUP-GUI-20261113 P1）：provider 路由链拓扑（X6）+ 成本/用量图表（ECharts）。
// 铁律：健康色只来自 /api/providers/topology 的 probe_cache 真实探活结果，禁止自报绿；
//       未配置 modelhub 时显示中文引导文案，不报错。X6 3.x 必须 async:false（UMD 异步渲染不落地大坑）。
// P2：拓扑节点可拖拽，位置持久化 localStorage（key="topology"）；自绘 minimap；键盘快捷键；重置布局。
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

const COLOR = { ok: "#3ecf8e", bad: "#ff6b6b", unknown: "#8a8a8a" };
let activated = false;
let lastTopo = null; // 最近一次 topology 响应（重置布局重绘用）
let topoGraph = null,
  costChart = null,
  usageChart = null;

function probeColor(p) {
  if (!p || p.expired) return COLOR.unknown; // 从未探活/缓存过期 → 灰（宁灰勿假绿）
  return p.inference_ok ? COLOR.ok : COLOR.bad;
}
function probeText(p) {
  if (!p) return "未探活";
  const lat = p.latency_ms != null ? ` · ${Math.round(p.latency_ms)}ms` : "";
  if (p.expired) return "探活已过期" + lat;
  if (p.inference_ok) return `健康${lat}`;
  return p.status === "down" ? "宕机（TCP 不通）" : "可达但推理失败";
}

async function api(path) {
  const r = await fetch(path);
  if (r.status === 401) {
    location.href = "/gate";
    throw new Error("401");
  }
  return r.json();
}

function showGuidance(d) {
  $("mhGuidance").style.display = "block";
  $("topoSection").style.display = "none";
  $("chartsSection").style.display = "none";
  $("mhGuidanceText").textContent =
    (d.reason ? d.reason + "。" : "") +
    (d.hint ||
      "请在 ~/.superops/config.toml 的 [superops] 段配置 modelhub_db 后重启 serve。");
}

function renderTopology(d) {
  lastTopo = d;
  $("mhGuidance").style.display = "none";
  $("topoSection").style.display = "";
  const by = {};
  for (const p of d.providers) by[p.name] = p;
  const chain = d.chain.map((n) => by[n]).filter(Boolean);
  // 窄屏降级：X6 画布隐藏，改列表（与账本图同一策略）
  const fb = $("topoFallback");
  fb.innerHTML = chain.length
    ? chain
        .map(
          (p, i) =>
            `<div class="fbItem"><span class="dot" style="background:${probeColor(p.probe)}"></span>
         <b>${i + 1}. ${esc(p.name)}</b>（${esc(p.kind)} · ${esc(p.model || "")}）<br>
         <span style="color:var(--dim)">${esc(probeText(p.probe))} · timeout ${p.timeout}s · ${esc(p.base_url || "")}</span></div>`,
        )
        .join("")
    : '<div class="empty">（无启用的 provider）</div>';
  if (!chain.length) return;
  if (topoGraph) topoGraph.dispose();
  topoGraph = new X6.Graph({
    container: $("topoCanvas"),
    async: false, // 大坑：X6 3.x UMD 默认异步渲染 DOM 恒空且零报错
    panning: { enabled: true },
    mousewheel: { enabled: true, modifiers: ["ctrl", "meta"] },
    interacting: { nodeMovable: true, edgeMovable: false }, // P2：允许拖拽并持久化
  });
  // P2：拖拽持久化（key="topology"）+ 自绘 minimap + 键盘快捷键注册
  attachDragPersist(topoGraph, () => "topology");
  attachMiniMap($("topoCanvas"), topoGraph);
  registerCanvas("model", {
    el: $("tab-model"),
    getGraph: () => topoGraph,
    onClose: () => {},
    fit: () => topoGraph && topoGraph.zoomToFit({ padding: 16, maxScale: 1 }),
  });
  const saved = loadPos("topology");
  const W = 210,
    H = 78,
    GAP = 90,
    X0 = 30,
    Y = 130;
  chain.forEach((p, i) => {
    const color = probeColor(p.probe);
    const sp = saved && saved[p.name];
    topoGraph.addNode({
      id: p.name,
      x: sp ? sp.x : X0 + i * (W + GAP),
      y: sp ? sp.y : Y,
      width: W,
      height: H,
      shape: "rect",
      attrs: {
        body: {
          fill: "#1e1e1e",
          stroke: color,
          strokeWidth: 2,
          rx: 10,
          ry: 10,
        },
        label: {
          text: `${p.name}\n${p.model || ""} · ${p.kind === "local" ? "本地" : "云端"}\n${probeText(p.probe)}`,
          fill: "#eaeaea",
          fontSize: 12,
          refX: 0.5,
          refY: 0.5,
          textAnchor: "middle",
          textVerticalAnchor: "middle",
        },
      },
    });
    if (i > 0) {
      topoGraph.addEdge({
        source: chain[i - 1].name,
        target: p.name,
        attrs: {
          line: {
            stroke: "#666",
            strokeWidth: 2,
            targetMarker: { name: "block", size: 8 },
          },
        },
        labels: [
          {
            attrs: {
              text: {
                text: `timeout ${p.timeout}s`,
                fill: "#9a9a9a",
                fontSize: 11,
              },
            },
          },
        ],
      });
    }
  });
  const total = chain.length * (W + GAP);
  topoGraph.zoomTo(
    Math.min(1, ($("topoCanvas").clientWidth - 20) / total || 1),
  );
}

function baseAxis() {
  return {
    axisLine: { lineStyle: { color: "#444" } },
    axisLabel: { color: "#9a9a9a", fontSize: 11 },
    splitLine: { lineStyle: { color: "#2b2b2b" } },
  };
}

function renderCharts(d) {
  $("chartsSection").style.display = "";
  $("usageTrendSection").style.display = "";
  if (typeof echarts === "undefined") {
    $("costChart").innerHTML = '<div class="empty">echarts 未加载</div>';
    return;
  }
  // 成本柱状图（按 provider）
  costChart =
    costChart && !costChart.isDisposed()
      ? costChart
      : echarts.init($("costChart"));
  costChart.setOption({
    backgroundColor: "transparent",
    textStyle: { color: "#eaeaea" },
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 20, top: 34, bottom: 30 },
    xAxis: {
      type: "category",
      data: d.items.map((r) => r.provider),
      ...baseAxis(),
    },
    yAxis: { type: "value", name: "成本", ...baseAxis() },
    series: [
      {
        name: "成本",
        type: "bar",
        barMaxWidth: 46,
        data: d.items.map((r) => r.cost),
        itemStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "#E85D3D" },
              { offset: 1, color: "#C8102E" },
            ],
          },
          borderRadius: [4, 4, 0, 0],
        },
        label: { show: true, position: "top", color: "#eaeaea", fontSize: 11 },
      },
    ],
  });
  // 用量趋势折线（按日 × provider 调用次数）
  const days = [...new Set(d.daily.map((r) => r.day))].sort();
  const providers = [...new Set(d.daily.map((r) => r.provider))].sort();
  const idx = (day, prov) =>
    d.daily.find((r) => r.day === day && r.provider === prov);
  usageChart =
    usageChart && !usageChart.isDisposed()
      ? usageChart
      : echarts.init($("usageTrendChart"));
  usageChart.setOption({
    backgroundColor: "transparent",
    textStyle: { color: "#eaeaea" },
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#9a9a9a" }, top: 0 },
    grid: { left: 50, right: 20, top: 34, bottom: 30 },
    xAxis: { type: "category", data: days, ...baseAxis() },
    yAxis: { type: "value", name: "调用次数", ...baseAxis() },
    series: providers.map((p) => ({
      name: p,
      type: "line",
      smooth: true,
      data: days.map((day) => (idx(day, p) || { calls: 0 }).calls),
    })),
  });
}

async function activate() {
  if (activated) {
    costChart && costChart.resize();
    usageChart && usageChart.resize();
    topoGraph && topoGraph.centerContent();
    return;
  }
  activated = true;
  // P2：重置布局按钮——清 topology 位置缓存并回默认排布
  $("resetTopoBtn").onclick = () => {
    clearPos("topology");
    if (typeof window.toast === "function") window.toast("已清除位置缓存，回到默认布局");
    if (lastTopo) renderTopology(lastTopo);
  };
  try {
    const topo = await api("/api/providers/topology");
    if (!topo.configured) {
      showGuidance(topo);
      return;
    }
    renderTopology(topo);
    const cost = await api("/api/usage/cost");
    if (cost.configured && cost.items.length) renderCharts(cost);
    else {
      $("chartsSection").style.display = "";
      $("costChart").innerHTML = '<div class="empty">（暂无用量记录）</div>';
    }
  } catch (e) {
    $("mhGuidance").style.display = "block";
    $("mhGuidanceText").textContent = "加载失败：" + e;
    activated = false;
  }
}
window.__modelActivate = activate;
window.addEventListener("resize", () => {
  costChart && costChart.resize();
  usageChart && usageChart.resize();
});
