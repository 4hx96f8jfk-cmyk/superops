// G2 审计哈希链可视化（SUP-GUI-20261113 P0）：时间线 + /api/audit/verify 联动。
// 纯只读视图：不改哈希链逻辑，仅消费既有 /api/audit 与 /api/audit/verify。
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

export async function activate() {
  const [v, a] = await Promise.all([
    (await api("/api/audit/verify")).json(),
    (await api("/api/audit?limit=100")).json(),
  ]);
  // 链校验横幅：valid=绿标全链完好；broken=红标并给出断点定位
  const banner = $("chainBanner");
  if (v.valid) {
    banner.className = "chainBanner ok";
    banner.innerHTML = `✓ 哈希链完好 —— 全链 ${v.checked} 条记录逐行验真通过`;
  } else {
    const b = v.broken_at || {};
    banner.className = "chainBanner bad";
    banner.innerHTML =
      `✗ 哈希链断裂 —— 断点 <b>#${esc(b.id)} ${esc(b.what)}</b>（${esc(b.reason)}），` +
      `已校验 ${v.checked} 条。下方红标行即断点。`;
  }
  // 时间线：/api/audit 返回 id 倒序，反转为旧→新
  const items = (a.items || []).slice().reverse();
  const tl = $("auditTimeline");
  if (!items.length) {
    tl.innerHTML = '<div class="empty">（暂无控制台操作）</div>';
    return;
  }
  tl.innerHTML = items
    .map((r) => {
      const broken = v.broken_at && v.broken_at.id === r.id;
      return `<div class="tlItem${broken ? " broken" : ""}">
      <span class="tlDot"></span>
      <div class="tlBody">
        <div class="tlHead"><b>#${r.id}</b> ${esc(r.what)}
          <span class="note">${esc(r.who)} · ${fmt(r.at)}</span>
          ${broken ? `<span class="st PAUSED">断点：${esc(v.broken_at.reason)}</span>` : ""}</div>
        ${r.intent_id ? `<div class="note">intent <code>${esc(r.intent_id)}</code></div>` : ""}
        ${r.params ? `<pre class="tlPre">${esc(r.params)}</pre>` : ""}
        <div class="note">row_hash ${esc((r.row_hash || "（无）").slice(0, 16))}…</div>
      </div></div>`;
    })
    .join("");
  if (v.broken_at) {
    const el = tl.querySelector(".tlItem.broken");
    if (el) el.scrollIntoView({ block: "center" });
  }
}
window.__auditActivate = activate;
