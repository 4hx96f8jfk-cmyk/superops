// G4 总览仪表盘趋势图（SUP-GUI-20261113 P1）：消费 /api/stats/trend，
// 按时间桶画 step 总量折线 + 审计事件热度。纯 ECharts，无构建链。
const $ = (id) => document.getElementById(id);

let trendChart = null;

async function loadTrend() {
  const el = $("trendChart");
  if (!el || typeof echarts === "undefined") return;
  try {
    const d = await (
      await fetch("/api/stats/trend?bucket=86400&limit=14")
    ).json();
    if (!el.clientWidth) return; // 画布不可见时跳过（窄屏/未激活）
    trendChart =
      trendChart && !trendChart.isDisposed() ? trendChart : echarts.init(el);
    const fmt = (ts) =>
      new Date(ts * 1000).toLocaleDateString("zh-CN", {
        month: "numeric",
        day: "numeric",
      });
    trendChart.setOption({
      backgroundColor: "transparent",
      textStyle: { color: "#eaeaea" },
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "#9a9a9a" }, top: 0 },
      grid: { left: 44, right: 20, top: 34, bottom: 28 },
      xAxis: {
        type: "category",
        data: d.buckets.map((b) => fmt(b.start)),
        axisLine: { lineStyle: { color: "#444" } },
        axisLabel: { color: "#9a9a9a", fontSize: 11 },
      },
      yAxis: {
        type: "value",
        minInterval: 1,
        axisLine: { lineStyle: { color: "#444" } },
        axisLabel: { color: "#9a9a9a", fontSize: 11 },
        splitLine: { lineStyle: { color: "#2b2b2b" } },
      },
      series: [
        {
          name: "新增 step",
          type: "line",
          smooth: true,
          data: d.buckets.map((b) => b.steps_total),
          itemStyle: { color: "#6ab0ff" },
          areaStyle: { opacity: 0.12 },
        },
        {
          name: "审计事件",
          type: "line",
          smooth: true,
          data: d.buckets.map((b) => b.audit_events),
          itemStyle: { color: "#3ecf8e" },
          areaStyle: { opacity: 0.12 },
        },
      ],
    });
  } catch (e) {
    /* 静默：轮询场景下网络抖动不打扰用户 */
  }
}
window.loadTrend = loadTrend;
window.addEventListener("resize", () => trendChart && trendChart.resize());
