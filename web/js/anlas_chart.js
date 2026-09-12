// ============================================================
// 额度恢复统计面板: 折线图 + 恢复速度估算
//   NovelAI 的额度恢复规则未公开 (黑箱), 后端定时调用纯查询接口
//   (/user/subscription, 不消耗额度) 采样落盘, 这里画折线并统计
//   "每小时恢复多少额度", 同时给出接口自带的 timeUntilNextPercent 推算值作对照。
//   纯 Canvas 绘制, 无第三方依赖, 颜色跟随 WebUI 主题。
// ============================================================
import { el, toast } from "./ui.js";
import { get, post } from "./api.js";

/** 令牌配色 (与主题色系一致) */
const LINE_COLORS = ["#8b5cf6", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4"];
/** 圈码序号 (与徽标、统计行一致) */
const MARKS = ["①", "②", "③", "④", "⑤", "⑥"];

/** 两条线的固定配色: 额度(紫, 左轴) / 点数(橙, 右轴) */
const COLOR_USAGE = "#8b5cf6";
const COLOR_ANLAS = "#f59e0b";

/** 点数显示: 大数加千分位 */
const fmtNum = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return Math.abs(n) >= 1000 ? n.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) : String(Math.round(n * 100) / 100);
};

function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (cs.getPropertyValue(name) || "").trim() || fallback;
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  return {
    dark,
    text2: v("--text-2", dark ? "#9aa0ae" : "#6b7280"),
    border: v("--border", dark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.1)"),
    grid: dark ? "rgba(255,255,255,0.14)" : "rgba(0,0,0,0.11)",
    bg: v("--panel-solid", dark ? "#161821" : "#ffffff"),
  };
}

const fmtTime = (t) => {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const fmtDateTime = (t) => {
  const d = new Date(t * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${fmtTime(t)}`;
};
/** 速度显示: <0 表示在消耗 (生成中), >0 表示在恢复 */
function fmtRate(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)} %/小时`;
}

/** 画折线图: 横轴时间; 左轴=额度%(0-100 固定), 右轴=点数(自适应); 两条线
 *  hoverT: 鼠标悬停的采样时刻 (整数秒), 传入时画竖参考线并放大该时刻的数据点
 */
function drawChart(canvas, stats, hours, selIdx = 0, hoverT = null) {
  const wrap = canvas.parentElement;
  const cssW = Math.max(240, wrap.clientWidth);
  const cssH = 210;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const th = themeColors();
  const padL = 36;
  const padR = 62; // 右侧留出点数刻度 + 末端标签
  const padT = 24; // 顶部留给图例
  const padB = 20;
  const plotW = cssW - padL - padR;
  const plotH = cssH - padT - padB;

  const list = stats || [];
  // selIdx < 0 = 查看全部 Token (多 Token 时 2×N 条线); 只有 1 个 Token 时等同单选
  const showAll = selIdx < 0 && list.length > 1;
  const shown = showAll
    ? list
    : (list.length ? [list[Math.max(0, Math.min(list.length - 1, selIdx))]] : []);
  const series = shown.map((s, i) => ({
    stat: s,
    // 单选时: 额度=紫 / 点数=橙; 全部时: 每个 Token 一种颜色, 额度实线 / 点数虚线
    color: showAll ? LINE_COLORS[i % LINE_COLORS.length] : COLOR_USAGE,
    anlasColor: showAll ? LINE_COLORS[i % LINE_COLORS.length] : COLOR_ANLAS,
    usage: [...(s.points || [])].sort((a, b) => a[0] - b[0]),
    anlas: [...(s.anlas_points || [])].sort((a, b) => a[0] - b[0]),
    mark: MARKS[i] || String(i + 1),
  }));

  const allTs = [];
  series.forEach((s) => {
    s.usage.forEach((p) => allTs.push(p[0]));
    s.anlas.forEach((p) => allTs.push(p[0]));
  });
  let t0 = allTs.length ? Math.min(...allTs) : 0;
  let t1 = allTs.length ? Math.max(...allTs) : 0;
  if (t1 - t0 < 60) t1 = t0 + 60; // 单点/极短跨度时给个最小窗口

  // 选了时间范围时按固定窗口显示 (随时间推移逐渐填满);
  // 但数据跨度不足 1 小时时先贴着数据画, 避免刚开始记录时全挤在右边缘
  if (hours > 0 && (t1 - t0) / 3600 >= 1) {
    t0 = Math.max(t0, t1 - hours * 3600);
  }

  const X = (t) => padL + ((t - t0) / (t1 - t0)) * plotW;
  // 左轴: 额度% (固定 0-100)
  const Y = (p) => padT + (1 - Math.max(0, Math.min(100, p)) / 100) * plotH;
  // 右轴: 点数 (按所有显示中的 Token 自适应, 留 8% 余量)
  const avalsRaw = [];
  series.forEach((s) => s.anlas.forEach((p) => avalsRaw.push(Number(p[1]))));
  const avals = avalsRaw.filter((v) => Number.isFinite(v));
  let aMin = avals.length ? Math.min(...avals) : 0;
  let aMax = avals.length ? Math.max(...avals) : 1;
  if (aMax - aMin < 1) {
    const pad = Math.max(1, Math.abs(aMax) * 0.02);
    aMin -= pad;
    aMax += pad;
  } else {
    const pad = (aMax - aMin) * 0.08;
    aMin -= pad;
    aMax += pad;
  }
  aMin = Math.max(0, aMin); // 点数不会为负, 避免右轴出现负刻度
  // 右轴上限取整到友好数值 (1/2/2.5/5/10 × 10^n), 刻度读数更顺
  const niceCeil = (v) => {
    if (!(v > 0)) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / mag;
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
    return step * mag;
  };
  aMax = niceCeil(aMax);
  const Y2 = (v) => padT + (1 - (Number(v) - aMin) / (aMax - aMin)) * plotH;

  // 网格 + Y 轴刻度 (0/25/50/75/100)
  ctx.strokeStyle = th.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = th.text2;
  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  [0, 25, 50, 75, 100].forEach((p) => {
    const y = Y(p);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.stroke();
    ctx.fillText(`${p}%`, padL - 6, y);
  });

  // X 轴时间刻度: 首/中/尾 3 个 (首尾贴边对齐, 避免被画布裁掉)
  // 跨度不足 1 小时时精确到秒, 否则同一分钟内的多个采样会显示成重复标签
  const fmtTick = (t1 - t0 < 3600)
    ? (t) => {
      const d = new Date(t * 1000);
      const p = (n) => String(n).padStart(2, "0");
      return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    : fmtDateTime;
  ctx.textBaseline = "top";
  [
    [t0, "left", padL],
    [(t0 + t1) / 2, "center", padL + plotW / 2],
    [t1, "right", padL + plotW],
  ].forEach(([t, align, x]) => {
    ctx.textAlign = align;
    ctx.fillText(fmtTick(t), x, padT + plotH + 5);
  });

  // 右轴: 点数刻度 (上/中/下 3 个); 单选时用点数线同色, 全部模式用中性色避免"一个轴一个色"的误导
  ctx.fillStyle = showAll ? th.text2 : COLOR_ANLAS;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  [aMax, (aMin + aMax) / 2, aMin].forEach((v) => {
    ctx.fillText(fmtNum(v), padL + plotW + 6, Y2(v));
  });

  // 图例 (顶部)
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.font = "10px system-ui, sans-serif";
  const legendY = padT / 2 + 1;
  let lx = padL;
  const legendChip = (color, text, dashed = false) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    if (dashed) ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(lx, legendY);
    ctx.lineTo(lx + 14, legendY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = th.text2;
    ctx.fillText(text, lx + 18, legendY);
    lx += 18 + ctx.measureText(text).width + 12;
  };
  if (showAll) {
    // 全部模式: 每个 Token 一种颜色, 实线=额度 / 虚线=点数
    series.forEach((s) => legendChip(s.color, `${s.mark}${s.stat.token || "Token " + (s.stat.index + 1)}`));
    // 线型说明用纯文字 (画灰色虚线样例会被误读成"还有第三条灰线")
    ctx.fillStyle = th.text2;
    ctx.fillText("实线=额度% / 虚线=点数", lx, legendY);
  } else {
    legendChip(COLOR_USAGE, "额度 %");
    legendChip(COLOR_ANLAS, "点数");
    if (series.length) {
      ctx.fillStyle = th.text2;
      ctx.fillText(`Token ${series[0].stat.index + 1} ${series[0].stat.token || ""}`, lx + 2, legendY);
    }
  }

  if (!allTs.length) {
    ctx.fillStyle = th.text2;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("暂无采样数据", padL + plotW / 2, padT + plotH / 2);
    return;
  }

  // 折线: 额度%(左轴) + 点数(右轴); 全部模式下点数线用虚线区分
  const drawSeries = (pts, color, yFn, endLabel, dashed, labelOffset) => {
    if (!pts.length) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    if (dashed) ctx.setLineDash([5, 3]);
    ctx.beginPath();
    pts.forEach(([t, v], k) => {
      const x = X(t);
      const y = yFn(Number(v));
      if (k === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);

    // 数据点 (最后一个高亮)
    pts.forEach(([t, v], k) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(X(t), yFn(Number(v)), k === pts.length - 1 ? 3.2 : 2, 0, Math.PI * 2);
      ctx.fill();
    });

    // 末端标签: 按线型/序号错位, 避免多条线数值接近时标签叠在一起; 并钳制在图内
    const [lt, lv] = pts[pts.length - 1];
    const ly = Math.max(padT + 7, Math.min(padT + plotH - 7, yFn(Number(lv)) + labelOffset));
    ctx.fillStyle = color;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(endLabel(lv), X(lt) - 4, ly);
  };

  series.forEach((s, si) => {
    if (showAll) {
      drawSeries(s.usage, s.color, (v) => Y(Number(v)), (v) => `${s.mark}${v}%`, false, -10 + si * 12);
      drawSeries(s.anlas, s.anlasColor, (v) => Y2(v), (v) => `${s.mark}${fmtNum(v)}`, true, 14 + si * 12);
    } else {
      drawSeries(s.usage, s.color, (v) => Y(Number(v)), (v) => `${v}%`, false, -9);
      drawSeries(s.anlas, s.anlasColor, (v) => Y2(v), (v) => `${fmtNum(v)}`, false, 11);
    }
  });

  // 供悬停提示使用: 保存坐标换算与"时刻→数值"映射 (每次重绘刷新)
  canvas._anrGeom = {
    t0, t1, padL, padT, plotW, plotH, showAll,
    times: [...new Set(allTs)].sort((a, b) => a - b),
    series: series.map((s) => ({
      mark: s.mark,
      color: s.color,
      anlasColor: s.anlasColor,
      usage: new Map(s.usage.map((p) => [p[0], p[1]])),
      anlas: new Map(s.anlas.map((p) => [p[0], p[1]])),
    })),
  };

  // 悬停: 竖参考线 + 放大该时刻的数据点
  if (hoverT !== null && canvas._anrGeom.times.includes(hoverT)) {
    const hx = X(hoverT);
    ctx.save();
    ctx.strokeStyle = th.text2;
    ctx.globalAlpha = 0.6;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, padT + plotH);
    ctx.stroke();
    ctx.restore();

    const drawHit = (pts, yFn, color) => {
      const hit = pts.find((p) => p[0] === hoverT);
      if (!hit) return;
      const hy = yFn(Number(hit[1]));
      ctx.beginPath();
      ctx.fillStyle = color;
      ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = th.bg;
      ctx.stroke();
    };
    series.forEach((s) => {
      drawHit(s.usage, (v) => Y(Number(v)), s.color);
      drawHit(s.anlas, (v) => Y2(v), s.anlasColor);
    });
  }
}

/** 额度统计面板 (放在输出图片卡下方) */
export function createAnlasPanel() {
  let stats = [];
  let hours = 24;
  let intervalSec = 1800;
  let selIdx = -1; // -1 = 查看全部 Token (各 Token 一种颜色, 实线=额度 / 虚线=点数)
  let skipMode = "off"; // 额度/点数耗尽时的处理方式
  let health = [];      // 各 Token 的可用性状态

  // Token 切换 (各 Token 额度独立; 图为该 Token 的 额度% + 点数 两条线)
  const tokenSel = el("select", { class: "btn btn-sm", style: "padding:2px 6px;", title: "选择要查看的 Token" });
  tokenSel.addEventListener("change", () => {
    selIdx = Number(tokenSel.value) || 0;
    drawChart(canvas, stats, hours, selIdx);
  });

  const canvas = el("canvas", { style: "display:block;width:100%;height:210px;" });
  const chartWrap = el("div", { style: "position:relative;margin:6px 0 2px;" }, [canvas]);
  const summary = el("div", { class: "anlas-summary", style: "font-size:12px;line-height:1.7;" });
  const meta = el("div", { class: "muted", style: "font-size:11px;margin-top:6px;" });

  // 悬停提示框: 移到采样点附近时显示该时刻各 Token 的额度/点数
  const tip = el("div", {
    style: "position:absolute;display:none;pointer-events:none;z-index:6;padding:6px 9px;border-radius:6px;" +
      "font-size:11px;line-height:1.6;white-space:nowrap;background:var(--panel-solid,#fff);" +
      "border:1px solid var(--border,rgba(0,0,0,.15));box-shadow:0 3px 12px rgba(0,0,0,.18);color:var(--text,inherit);",
  });
  chartWrap.append(tip);

  let hoverT = null; // 当前悬停的采样时刻

  function hideTip() {
    tip.style.display = "none";
    if (hoverT !== null) {
      hoverT = null;
      drawChart(canvas, stats, hours, selIdx);
    }
  }

  function renderTip(t, px) {
    const g = canvas._anrGeom;
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, "0");
    const rows = [`<b>${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}</b>`];
    g.series.forEach((s) => {
      const u = s.usage.has(t) ? `${s.usage.get(t)}%` : "—";
      const a = s.anlas.has(t) ? fmtNum(s.anlas.get(t)) : "—";
      rows.push(
        `<span style="color:${s.color};">■</span> ${g.showAll ? s.mark + " " : ""}额度 ${u} · 点数 ${a}`
      );
    });
    tip.innerHTML = rows.join("<br>");
    tip.style.display = "block";
    const w = tip.offsetWidth;
    const wrapW = chartWrap.clientWidth;
    let left = px + 14;
    if (left + w > wrapW - 4) left = Math.max(4, px - w - 14);
    tip.style.left = left + "px";
    tip.style.top = "4px";
  }

  canvas.addEventListener("mousemove", (e) => {
    const g = canvas._anrGeom;
    if (!g || !g.times.length) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    // 屏幕 x → 时间 → 最近的采样时刻
    const t = g.t0 + ((x - g.padL) / g.plotW) * (g.t1 - g.t0);
    let best = g.times[0];
    let bd = Infinity;
    g.times.forEach((tt) => {
      const d = Math.abs(tt - t);
      if (d < bd) { bd = d; best = tt; }
    });
    const px = g.padL + ((best - g.t0) / (g.t1 - g.t0)) * g.plotW;
    if (Math.abs(px - x) > 70) { hideTip(); return; } // 离采样点太远就不提示
    if (best !== hoverT) {
      hoverT = best;
      drawChart(canvas, stats, hours, selIdx, hoverT);
    }
    renderTip(best, px);
  });
  canvas.addEventListener("mouseleave", hideTip);

  // 控件行
  const rangeSel = el("select", { class: "btn btn-sm", style: "padding:2px 6px;" });
  [["6", "近 6 小时"], ["24", "近 24 小时"], ["72", "近 3 天"], ["168", "近 7 天"], ["0", "全部"]].forEach(([v, t]) => {
    const o = el("option", { value: v, text: t });
    if (v === "24") o.selected = true;
    rangeSel.append(o);
  });
  rangeSel.addEventListener("change", () => {
    hours = Number(rangeSel.value) || 0;
    refresh();
  });

  const intervalInput = el("input", {
    type: "number", min: "1", max: "1440", value: "30",
    style: "width:58px;padding:2px 4px;", title: "自动采样间隔 (分钟)",
  });
  const saveIntervalBtn = el("button", { class: "btn btn-sm", text: "保存间隔" });
  saveIntervalBtn.addEventListener("click", async () => {
    const minutes = Number(intervalInput.value);
    if (!Number.isFinite(minutes) || minutes < 1) {
      toast("间隔至少 1 分钟", "warning");
      return;
    }
    try {
      const r = await post("/api/anlas/interval", { minutes });
      if (r.ok) {
        intervalSec = r.interval;
        toast(`已设为每 ${r.minutes} 分钟采样一次`, "success");
        renderMeta();
      } else {
        toast(r.message || "保存失败", "error");
      }
    } catch (e) {
      toast("保存失败: " + e.message, "error");
    }
  });

  const sampleBtn = el("button", { class: "btn btn-sm", text: "📍 立即采样" });
  sampleBtn.addEventListener("click", async () => {
    sampleBtn.disabled = true;
    const old = sampleBtn.textContent;
    sampleBtn.textContent = "采样中…";
    try {
      const r = await post("/api/anlas/sample", {});
      toast(r.ok ? "已记录一次额度采样" : (r.message || "采样失败"), r.ok ? "success" : "error");
      await refresh();
    } catch (e) {
      toast("采样失败: " + e.message, "error");
    } finally {
      sampleBtn.disabled = false;
      sampleBtn.textContent = old;
    }
  });

  const refreshBtn = el("button", { class: "btn btn-sm", text: "🔄 刷新" });
  refreshBtn.addEventListener("click", async () => {
    // 刷新前先实时检查一次额度 (纯查询, 不消耗): 恢复了的 Token 立刻重新参与生图
    refreshBtn.disabled = true;
    const old = refreshBtn.textContent;
    refreshBtn.textContent = "检查中…";
    try {
      await get("/api/tokens/health?check=1");
    } catch { /* 检查失败也照常刷新本地数据 */ }
    await refresh();
    refreshBtn.disabled = false;
    refreshBtn.textContent = old;
  });

  // 额度/点数耗尽时的处理方式
  const modeSel = el("select", { class: "btn btn-sm", style: "padding:2px 6px;", title: "额度/点数耗尽时是否暂停该 Token 生图" });
  modeSel.addEventListener("change", async () => {
    try {
      const r = await post("/api/tokens/health/mode", { mode: modeSel.value });
      if (r.ok) {
        skipMode = r.mode;
        toast("已设置: " + (r.label || r.mode), "success");
        await refresh();
      } else {
        toast(r.message || "设置失败", "error");
      }
    } catch (e) {
      toast("设置失败: " + e.message, "error");
    }
  });

  const controls = el("div", { style: "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px;" }, [
    el("span", { style: "font-size:12px;", text: "Token" }),
    tokenSel,
    el("span", { style: "font-size:12px;margin-left:4px;", text: "范围" }),
    rangeSel,
    el("span", { style: "font-size:12px;margin-left:4px;", text: "采样间隔(分钟)" }),
    intervalInput,
    saveIntervalBtn,
    sampleBtn,
    refreshBtn,
    el("span", { style: "font-size:12px;margin-left:4px;", text: "额度用尽时" }),
    modeSel,
  ]);

  const card = el("div", { class: "card", style: "margin-top:12px;" }, [
    el("div", { class: "card-title" }, ["📈 额度恢复统计"]),
    summary,
    chartWrap,
    meta,
    controls,
  ]);

  function renderMeta() {
    const mins = Math.round(intervalSec / 60);
    meta.textContent =
      `每 ${mins} 分钟自动采样一次 (后台记录, 关页面也在记) · 已用 ${stats[0]?.samples || 0} 个采样点` +
      (stats.length ? "" : " · 点「立即采样」开始");
  }

  function renderSummary() {
    summary.replaceChildren();
    if (!stats.length) {
      summary.append(el("span", { class: "muted", text: "还没有数据。额度是黑箱的话, 先记一段时间才能算出恢复速度。" }));
      return;
    }
    const marks = ["①", "②", "③", "④", "⑤"];
    stats.forEach((s, i) => {
      const color = LINE_COLORS[i % LINE_COLORS.length];
      const rate = s.measured_per_hour;
      const pred = s.predicted_per_hour;
      const h = health.find((x) => x.index === s.index);
      // 状态: 已暂停(额度/点数耗尽) / 可用 / 尚未采样
      const statusNode = !s.samples
        ? el("span", { style: "color:var(--text-2);", text: "尚无采样" })
        : (h && h.usable === false
          ? el("span", { style: "color:#e5484d;font-weight:600;", text: `⏸ 已暂停生图 (${h.reason || "额度用尽"})` })
          : el("span", { style: "color:#22c55e;", text: "✅ 可用" }));
      const row = el("div", { style: "display:flex;flex-wrap:wrap;gap:6px;align-items:baseline;" }, [
        el("span", { style: `color:${color};font-weight:600;`, text: `${marks[i] || i + 1}${s.token || ""}` }),
        el("span", { text: `当前 ${s.remains ?? "?"}% · 点数 ${s.anlas ?? "?"}` }),
        statusNode,
        el("span", { style: "color:var(--text-2);", text: `实测 ${fmtRate(rate)}` }),
        el("span", { style: "color:var(--text-2);", text: `接口推算 ${fmtRate(pred)}` }),
        s.anlas_delta !== null && s.anlas_delta !== undefined && Number(s.anlas_delta) !== 0
          ? el("span", { style: "color:var(--text-2);", text: `点数区间变化 ${s.anlas_delta > 0 ? "+" : ""}${s.anlas_delta}` })
          : null,
        el("span", { style: "color:var(--text-2);", text: `${s.samples} 点 / ${s.span_hours}h` }),
        s.generated
          ? el("span", {
            style: "color:var(--text-2);",
            text: `本窗口生成 ${s.generated} 张` +
              (s.cost_per_image !== null && s.cost_per_image !== undefined
                ? ` · 每张约 ${Number(s.cost_per_image).toFixed(3)}% 电量`
                : " · 电量变化不足 1%, 暂无法估算"),
          })
          : null,
      ].filter(Boolean));
      summary.append(row);
    });
    if (skipMode !== "off") {
      const paused = stats.filter((s) => {
        const h = health.find((x) => x.index === s.index);
        return h && h.usable === false;
      }).length;
      summary.append(el("div", {
        class: "muted",
        style: "font-size:11px;margin-top:2px;",
        text: paused
          ? `当前 ${paused} 个 Token 已暂停生图: 队列会跳过它们, 额度恢复后自动重新参与 (刷新可立即复查)。`
          : "当前没有 Token 被暂停; 额度用尽时会自动暂停并跳过, 恢复后自动启用。",
      }));
    }
    summary.append(el("div", {
      class: "muted",
      style: "font-size:11px;margin-top:2px;",
      text: "实测 = 对采样点做最小二乘拟合; 接口推算 = 3600 ÷ timeUntilNextPercent。用量为整数 %, 跨度太短时实测值可能为 0。",
    }));
    summary.append(el("div", {
      class: "muted",
      style: "font-size:11px;",
      text: "「每张约 X% 电量」= (纯回血速率 × 时长 − 电量净变化) ÷ 张数; 电量是整数 % 且会自己回血, 需较长跨度才准; 若账号与他人共用(拼车), 别人的消耗会计入 → 该值是上限。",
    }));
  }

  async function refresh() {
    try {
      const d = await get("/api/anlas/history?hours=" + (hours || 0));
      stats = d.stats || [];
      health = d.health || [];
      if (Array.isArray(d.skip_modes) && modeSel.options.length !== d.skip_modes.length) {
        modeSel.replaceChildren();
        d.skip_modes.forEach((m) => modeSel.append(el("option", { value: m.value, text: m.label })));
      }
      if (d.skip_mode) {
        skipMode = d.skip_mode;
        modeSel.value = skipMode;
      }
      if (d.interval) {
        intervalSec = d.interval;
        intervalInput.value = String(Math.round(d.interval / 60));
      }
      renderSummary();
      renderMeta();
      // 重建 Token 下拉: 首项为"全部", 之后逐个 Token
      tokenSel.replaceChildren();
      tokenSel.append(el("option", { value: "-1", text: `全部 Token (${stats.length})` }));
      stats.forEach((s, i) => {
        tokenSel.append(el("option", {
          value: String(i),
          text: `${MARKS[i] || i + 1} ${s.token || "?"} (${s.remains ?? "?"}% · ${s.anlas ?? "?"}点)`,
        }));
      });
      if (selIdx >= stats.length) selIdx = -1;
      tokenSel.value = String(selIdx);
      drawChart(canvas, stats, hours, selIdx);
      // 首次渲染时面板可能还没插入文档 (宽度为 0), 下一帧再画一次
      requestAnimationFrame(() => drawChart(canvas, stats, hours, selIdx));
    } catch (e) {
      summary.textContent = "读取额度历史失败: " + e.message;
    }
  }

  // 尺寸变化时重绘 (面板宽度随布局变化)
  window.addEventListener("resize", () => drawChart(canvas, stats, hours, selIdx));
  try {
    new ResizeObserver(() => drawChart(canvas, stats, hours, selIdx)).observe(chartWrap);
  } catch { /* 旧浏览器忽略 */ }

  refresh();
  // 页面打开期间定期刷新 (后端一直在采样, 这里只是把新点画出来)
  setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, 120000);

  return card;
}
