// B4 安全中心页签：Wazuh 舰队状态 + 近 24h 高危告警（纯只读展示）。
// 铁律：数据全部来自服务端代理端点 /api/security/{fleet,alerts}，凭据只在
//       服务端持有，本文件不接触任何凭据；上游不可达时显示中文降级文案。
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

const STATUS_CN = {
  active: "在线",
  pending: "待注册",
  disconnected: "断连",
  never_connected: "从未连接",
};
function statusHtml(s) {
  const cls =
    s === "active"
      ? "CONFIRMED"
      : s === "never_connected" || s === "disconnected"
        ? "REJECTED"
        : "UNCERTAIN";
  return `<span class="st ${cls}">${esc(STATUS_CN[s] || s || "—")}</span>`;
}
function levelHtml(lv) {
  const cls = lv >= 12 ? "PAUSED" : lv >= 8 ? "UNCERTAIN" : "EXECUTING";
  return `<span class="st ${cls}">Lv.${esc(lv)}</span>`;
}
function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t);
  return isNaN(d) ? esc(t) : d.toLocaleString("zh-CN", { hour12: false });
}

async function api(path) {
  const r = await fetch(path);
  if (r.status === 401) {
    location.href = "/gate";
    throw new Error("401");
  }
  return r;
}

let loading = false;
async function loadSecurity() {
  if (loading) return;
  loading = true;
  try {
    const [fleet, alerts] = await Promise.all([
      (await api("/api/security/fleet")).json(),
      (await api("/api/security/alerts")).json(),
    ]);
    // 未配置 / 上游不可达 → 中文引导，不报错
    if (!fleet.available || !alerts.available) {
      const why = (!fleet.available ? fleet : alerts).reason || "未知原因";
      const hint = (!fleet.available ? fleet : alerts).hint || "";
      $("secGuideText").textContent = why + (hint ? "\n" + hint : "");
      $("secGuide").style.display = "block";
      $("secFleetSection").style.display = "none";
      $("secAlertsSection").style.display = "none";
      return;
    }
    $("secGuide").style.display = "none";
    $("secFleetSection").style.display = "";
    $("secAlertsSection").style.display = "";

    // 舰队摘要卡片：在线 / 待注册 / 断连 / 总数
    const s = fleet.summary || {};
    $("secCards").innerHTML = `
      <div class="statCard"><div class="statNum">${s.active ?? 0}</div><div class="statLbl">在线 Active</div></div>
      <div class="statCard warn"><div class="statNum">${s.pending ?? 0}</div><div class="statLbl">待注册 Pending</div></div>
      <div class="statCard bad"><div class="statNum">${(s.disconnected ?? 0) + (s.never_connected ?? 0)}</div><div class="statLbl">断连/未连接</div></div>
      <div class="statCard"><div class="statNum">${s.total ?? 0}</div><div class="statLbl">Agent 总数</div></div>`;

    $("secAgents").innerHTML = fleet.agents.length
      ? `<table><tr><th>ID</th><th>名称</th><th>IP</th><th>状态</th><th>版本</th><th>节点</th></tr>${fleet.agents
          .map(
            (a) => `<tr><td>${esc(a.id)}</td><td>${esc(a.name)}</td><td>${esc(a.ip || "—")}</td>
        <td>${statusHtml(a.status)}</td><td>${esc(a.version)}</td><td>${esc(a.node_name || "—")}</td></tr>`,
          )
          .join("")}</table>`
      : '<div class="empty">（无 agent）</div>';

    const lvBadge = alerts.total != null ? ` · 窗口内共 ${alerts.total} 条` : "";
    $("secAlertsTitle").textContent = `近 ${alerts.window_hours ?? 24} 小时高危告警（level ≥ ${alerts.min_level ?? 7}）${lvBadge}`;
    $("secAlerts").innerHTML = alerts.items.length
      ? `<table><tr><th>时间</th><th>Agent</th><th>规则</th><th>级别</th><th>描述</th></tr>${alerts.items
          .map(
            (r) => `<tr><td style="white-space:nowrap">${fmtTime(r.time)}</td>
        <td>${esc(r.agent_name || "—")} <span style="color:var(--dim)">#${esc(r.agent_id)}</span></td>
        <td>${esc(r.rule_id)}</td><td>${levelHtml(r.level)}</td><td>${esc(r.description || "")}</td></tr>`,
          )
          .join("")}</table>`
      : '<div class="empty">窗口内没有高危告警 ✓</div>';
  } catch (e) {
    $("secAgents").innerHTML = `<div class="empty">加载失败：${esc(e.message || e)}</div>`;
  } finally {
    loading = false;
  }
}
window.loadSecurity = loadSecurity;
window.__securityActivate = () => loadSecurity();
