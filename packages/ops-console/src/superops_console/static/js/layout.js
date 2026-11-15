// P2 画布体验共享模块（SUP-GUI-20261113 P2）：拖拽位置持久化 + 自绘 minimap + 键盘快捷键。
// 账本 DAG（graph.js）与模型拓扑（model.js）两个 X6 图共用；纯 ES module，无构建链。
//
// minimap 选型说明：@antv/x6-plugin-minimap@3.0.0（唯一 peer 为 @antv/x6 ^3.x 的版本）
// npm 包内不含任何 JS 产物（dist/ 仅 index.css，dist/index.js 404），2.0.7 的 UMD 只兼容
// X6 ^2.x——均不可用，故此处自绘轻量缩览（右下角视口矩形，点击/拖拽跳转）。
//
// localStorage key 设计：前缀 `superops.layout.` + 图 key。
//   账本 DAG key = intent_id（每个 intent 一份位置缓存）；模型拓扑 key = "topology"。
//   值 = JSON {nodeId: {x, y}}（节点左上角坐标，dagre 自动布局不落盘，仅存用户手动拖拽后的覆盖）。

const PREFIX = "superops.layout.";

export function loadPos(key) {
  try {
    const s = localStorage.getItem(PREFIX + key);
    return s ? JSON.parse(s) : null;
  } catch (e) {
    return null;
  }
}
export function savePos(key, map) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(map));
  } catch (e) {
    /* 隐私模式/配额满：静默降级为不持久化 */
  }
}
export function clearPos(key) {
  try {
    localStorage.removeItem(PREFIX + key);
  } catch (e) {}
}

/** 节点拖动后把全图位置写回 localStorage（key 由调用方闭包提供，支持切 intent 换 key）。 */
export function attachDragPersist(graph, keyOf) {
  graph.on("node:moved", () => {
    const m = {};
    for (const n of graph.getNodes()) {
      const p = n.getPosition();
      m[n.id] = { x: Math.round(p.x), y: Math.round(p.y) };
    }
    savePos(keyOf(), m);
  });
}

// ---------- 自绘 minimap（右下角缩览 + 视口矩形，点击/拖拽跳转） ----------
class MiniMap {
  constructor(container, graph, opts = {}) {
    this.el = document.createElement("div");
    this.el.className = "x6-minimap";
    this.el.title = "缩览：点击/拖拽跳转视口";
    this.el.__minimap = this; // attachMiniMap 替换旧实例时经此句柄 destroy()
    this.canvas = document.createElement("canvas");
    this.w = opts.w || 160;
    this.h = opts.h || 100;
    this.canvas.width = this.w;
    this.canvas.height = this.h;
    this.el.appendChild(this.canvas);
    container.appendChild(this.el);
    this.graph = graph;
    this.container = container;
    this.map = null;

    const upd = () => this.draw();
    for (const ev of ["cell:added", "cell:removed", "node:moved", "scale", "translate"]) {
      try {
        graph.on(ev, upd);
      } catch (e) {}
    }
    // 兜底轮询：视口平移/缩放事件名跨版本不稳，容器不可见时跳过，代价可忽略
    this.timer = setInterval(() => {
      if (this.container.offsetParent !== null) this.draw();
    }, 500);
    upd();

    const jump = (ev) => {
      if (!this.map) return;
      const r = this.canvas.getBoundingClientRect();
      const lx = this.map.bx + (ev.clientX - r.left - this.map.ox) / this.map.s;
      const ly = this.map.by + (ev.clientY - r.top - this.map.oy) / this.map.s;
      this.graph.centerPoint(lx, ly);
      upd();
    };
    this.canvas.addEventListener("pointerdown", (ev) => {
      this._drag = true;
      this.canvas.setPointerCapture(ev.pointerId);
      jump(ev);
    });
    this.canvas.addEventListener("pointermove", (ev) => this._drag && jump(ev));
    this.canvas.addEventListener("pointerup", () => (this._drag = false));
  }
  destroy() {
    clearInterval(this.timer);
    this.el.remove();
  }
  draw() {
    const g = this.graph;
    const ctx = this.canvas.getContext("2d");
    ctx.clearRect(0, 0, this.w, this.h);
    // 内容包围盒（优先 X6 API，缺失则手算节点并集）
    let c = null;
    try {
      const r = g.getContentBBox();
      if (r && r.width && r.height) c = r;
    } catch (e) {}
    if (!c) {
      const ns = g.getNodes();
      if (!ns.length) return;
      let x1 = Infinity,
        y1 = Infinity,
        x2 = -Infinity,
        y2 = -Infinity;
      for (const n of ns) {
        const p = n.getPosition(),
          s = n.getSize();
        x1 = Math.min(x1, p.x);
        y1 = Math.min(y1, p.y);
        x2 = Math.max(x2, p.x + s.width);
        y2 = Math.max(y2, p.y + s.height);
      }
      c = { x: x1, y: y1, width: x2 - x1, height: y2 - y1 };
    }
    // 视口矩形（本地坐标）
    let v = null;
    try {
      const r = g.getGraphArea();
      if (r && r.width && r.height) v = r;
    } catch (e) {}
    if (!v) {
      const z = g.zoom(),
        t = g.translate();
      v = {
        x: -t.tx / z,
        y: -t.ty / z,
        width: this.container.clientWidth / z,
        height: this.container.clientHeight / z,
      };
    }
    // 统一映射：内容∪视口 居中缩放到画布内
    const bx = Math.min(c.x, v.x),
      by = Math.min(c.y, v.y);
    const bw = Math.max(c.x + c.width, v.x + v.width) - bx;
    const bh = Math.max(c.y + c.height, v.y + v.height) - by;
    if (!(bw > 0 && bh > 0)) return;
    const s = Math.min((this.w - 10) / bw, (this.h - 10) / bh);
    const ox = (this.w - bw * s) / 2,
      oy = (this.h - bh * s) / 2;
    this.map = { bx, by, s, ox, oy };
    const mx = (x) => ox + (x - bx) * s,
      my = (y) => oy + (y - by) * s;
    // 节点块（描边色沿用节点 stroke）
    for (const n of g.getNodes()) {
      const p = n.getPosition(),
        sz = n.getSize();
      const st = (n.attr && n.attr("body/stroke")) || (n.attr && n.attr("rect/stroke")) || "#666";
      ctx.fillStyle = "#3a3a3a";
      ctx.strokeStyle = st;
      ctx.lineWidth = 1;
      ctx.fillRect(mx(p.x), my(p.y), Math.max(2, sz.width * s), Math.max(2, sz.height * s));
      ctx.strokeRect(mx(p.x), my(p.y), Math.max(2, sz.width * s), Math.max(2, sz.height * s));
    }
    // 视口矩形（品牌红）
    ctx.strokeStyle = "#E85D3D";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(mx(v.x), my(v.y), v.width * s, v.height * s);
  }
}

/** 给图挂自绘 minimap；同容器重复调用会先 destroy 旧的（回收 interval，防泄漏）。 */
export function attachMiniMap(container, graph) {
  const old = container.querySelector(":scope > .x6-minimap");
  if (old) {
    if (old.__minimap) old.__minimap.destroy();
    else old.remove(); // 兜底：无句柄（理论上不发生）至少移除 DOM
  }
  return new MiniMap(container, graph);
}

// ---------- 键盘快捷键（两图通用；焦点在输入框时不拦截） ----------
// 注册表：name -> {el 可见性锚点, getGraph(), onClose(), fit()}
const CANVASES = [];
export function registerCanvas(name, cfg) {
  const i = CANVASES.findIndex((c) => c.name === name);
  if (i >= 0) CANVASES.splice(i, 1);
  CANVASES.push({ name, ...cfg });
}
function activeCanvas() {
  for (const c of CANVASES) {
    if (c.el && c.el.offsetParent !== null) return c;
  }
  return null;
}
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (
    t &&
    (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)
  )
    return;
  const c = activeCanvas();
  if (!c) return;
  const g = c.getGraph ? c.getGraph() : null;
  if (e.key === "Escape" || e.key === "Delete") {
    // Esc 与 Delete 同义：关侧栏面板（G5 快捷键补全）
    if (c.onClose) {
      c.onClose();
      e.preventDefault();
    }
    return;
  }
  if (!g) return;
  let hit = true;
  switch (e.key) {
    case "+":
    case "=":
      g.zoom(0.1);
      break;
    case "-":
    case "_":
      g.zoom(-0.1);
      break;
    case "0":
      if (c.fit) c.fit();
      else g.zoomToFit({ padding: 24, maxScale: 1 });
      break;
    case "ArrowUp":
      g.translateBy(0, 50);
      break;
    case "ArrowDown":
      g.translateBy(0, -50);
      break;
    case "ArrowLeft":
      g.translateBy(50, 0);
      break;
    case "ArrowRight":
      g.translateBy(-50, 0);
      break;
    default:
      hit = false;
  }
  if (hit) e.preventDefault();
});
