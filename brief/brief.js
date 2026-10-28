// Renders brief/brief.json (text, tiles and chart data), which
// scripts/build_brief.py rewrites every China trading-day morning.
"use strict";
const svgNS = "http://www.w3.org/2000/svg";

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  if (text != null) e.textContent = text;
  return e;
}
function svgEl(tag, attrs, text) {
  const e = document.createElementNS(svgNS, tag);
  if (attrs) Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  if (text != null) e.textContent = text;
  return e;
}
function safeHref(u) {
  return /^(https?:\/\/|\.\.?\/)/.test(u || "") ? u : null;
}
function link(label, href) {
  const h = safeHref(href);
  if (!h) return document.createTextNode(label);
  const a = el("a", { href: h }, label);
  if (/^https?:/.test(h)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  return a;
}
function beijingNow() {
  const p = {};
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date()).forEach((x) => { p[x.type] = x.value; });
  return `${p.year}-${p.month}-${p.day}`;
}
function fmtBeijing(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const f = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  return f.format(d);
}

// ---------- charts ----------
const W = 560, H = 210, PL = 38, PR = 14, PT = 14, PB = 26;

function niceTicks(min, max, n) {
  const span = max - min || 1;
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}
function legend(series) {
  const box = el("div", { class: "legend" });
  series.forEach((s, i) => {
    const item = el("span");
    const sw = el("i", { class: "c" + i });
    sw.style.background = "var(--c)";
    item.appendChild(sw);
    item.appendChild(document.createTextNode(s.name));
    box.appendChild(item);
  });
  return box;
}
function cardShell(def) {
  const card = el("div", { class: "chart-card" });
  card.appendChild(el("h4", null, def.title));
  card.appendChild(el("p", { class: "chart-desc" }, def.desc || ""));
  return card;
}
function addSource(card, def) {
  if (!def.src) return;
  const p = el("p", { class: "chart-src" }, "数据:");
  p.appendChild(link(def.src[0], def.src[1]));
  card.appendChild(p);
}

function barChart(def) {
  const card = cardShell(def);
  const cats = def.categories, n = def.series.length;
  const all = def.series.flatMap((s) => s.values).concat(0);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const y = (v) => PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB);
  const slot = (W - PL - PR) / cats.length, bw = Math.min(26, (slot * 0.7) / n);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": def.title });
  niceTicks(lo, hi, 4).forEach((t) => {
    svg.appendChild(svgEl("line", { class: "ax", x1: PL, x2: W - PR, y1: y(t), y2: y(t) }));
    svg.appendChild(svgEl("text", { class: "ax-text", x: PL - 6, y: y(t) + 3, "text-anchor": "end" }, String(t)));
  });
  svg.appendChild(svgEl("line", { class: "ax", x1: PL, x2: W - PR, y1: y(0), y2: y(0), "stroke-width": 1.6 }));
  cats.forEach((c, i) => {
    const cx = PL + slot * (i + 0.5);
    svg.appendChild(svgEl("text", { class: "ax-text", x: cx, y: H - 8, "text-anchor": "middle" }, c));
    def.series.forEach((s, k) => {
      const v = s.values[i], bx = cx - (bw * n) / 2 + bw * k;
      const g = svgEl("g", { class: "c" + k });
      g.appendChild(svgEl("rect", { class: "s-bar", x: bx + 1, width: bw - 2, y: Math.min(y(v), y(0)), height: Math.max(1, Math.abs(y(v) - y(0))), rx: 2 }));
      g.appendChild(svgEl("text", { class: "val-text", x: bx + bw / 2, y: v >= 0 ? y(v) - 4 : y(v) + 12, "text-anchor": "middle" }, (v > 0 ? "+" : "") + v));
      svg.appendChild(g);
    });
  });
  card.appendChild(svg);
  card.appendChild(legend(def.series));
  addSource(card, def);
  return card;
}

function hbarChart(def) {
  const card = cardShell(def);
  const rows = def.rows, rowH = 24, top = 8, labelW = 150, right = 54;
  const vals = rows.map((r) => r.value).concat(0);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const height = top * 2 + rows.length * rowH;
  const x = (v) => labelW + ((v - lo) / (hi - lo)) * (W - labelW - right);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": def.title });
  svg.appendChild(svgEl("line", { class: "ax", x1: x(0), x2: x(0), y1: top, y2: height - top, "stroke-width": 1.6 }));
  rows.forEach((r, i) => {
    const cy = top + rowH * i + rowH / 2;
    const up = r.value >= 0;
    svg.appendChild(svgEl("text", { class: "ax-text lbl", x: labelW - 8, y: cy + 3.5, "text-anchor": "end" }, r.label));
    const g = svgEl("g", { class: up ? "c-up" : "c-down" });
    g.appendChild(svgEl("rect", { class: "s-bar", x: Math.min(x(r.value), x(0)), y: cy - 7, width: Math.max(1.5, Math.abs(x(r.value) - x(0))), height: 14, rx: 2 }));
    g.appendChild(svgEl("text", { class: "val-text", x: up ? x(r.value) + 5 : x(r.value) - 5, y: cy + 3.5, "text-anchor": up ? "start" : "end" }, (up ? "+" : "") + r.value.toFixed(2)));
    svg.appendChild(g);
  });
  card.appendChild(svg);
  addSource(card, def);
  return card;
}

// ---------- page ----------
function renderSection(sec, brief) {
  const s = el("section", { class: "bsec", id: sec.id });
  s.appendChild(el("h2", null, sec.title));
  sec.groups.forEach((g, gi) => {
    const box = el("div", { class: "bgroup" });
    box.appendChild(el("h3", null, g.h));
    const ul = el("ul");
    g.items.forEach((it) => {
      const li = el("li", null, it.t);
      if (it.src && it.src.length) {
        const span = el("span", { class: "src" });
        it.src.slice(0, 2).forEach((r) => span.appendChild(link(r.n, r.u)));
        li.appendChild(span);
      }
      ul.appendChild(li);
    });
    box.appendChild(ul);
    s.appendChild(box);
    if (gi === 0) {
      (sec.charts || []).forEach((id) => {
        const def = brief.charts[id];
        if (!def) return;
        let card = null;
        try { card = def.kind === "hbars" ? hbarChart(def) : barChart(def); } catch (e) { console.error(id, e); }
        if (card) s.appendChild(card);
      });
    }
  });
  return s;
}

async function main() {
  const brief = await fetch("brief.json", { cache: "no-store" }).then((r) => r.json());
  const [by, bm, bd] = brief.date.split("-");
  document.getElementById("brief-date").textContent = `${by}年${+bm}月${+bd}日 ${brief.weekday}`;
  document.getElementById("brief-updated").textContent = `更新于北京时间 ${fmtBeijing(brief.generated_at)} · 每个交易日早晨更新`;

  const today = beijingNow(), stale = document.getElementById("stale");
  const msgs = [];
  if (brief.date < today) msgs.push(`今天(${today})的简报尚未生成,以下为 ${brief.date} 的内容;非交易日不更新。`);
  if ((brief.narrative_at || "").slice(0, 10) < (brief.generated_at || "").slice(0, 10)) msgs.push("文字部分本次未能更新,仅刷新了行情数据。");
  if (msgs.length) { stale.textContent = msgs.join(" "); stale.hidden = false; }

  const ol = document.getElementById("tldr-list");
  brief.tldr.forEach((t) => ol.appendChild(el("li", null, t)));

  const snap = document.getElementById("snapshot");
  brief.snapshot.forEach((t) => {
    const d = el("div", { class: "tile" });
    d.appendChild(el("div", { class: "tile-label" }, t.label));
    d.appendChild(el("div", { class: "tile-value" }, t.value));
    d.appendChild(el("div", { class: "tile-note" }, t.note));
    snap.appendChild(d);
  });

  const toc = document.getElementById("toc"), main = document.getElementById("sections");
  brief.sections.forEach((sec) => {
    toc.appendChild(el("a", { href: "#" + sec.id }, sec.title.replace(/^[一二三四]、/, "")));
    main.appendChild(renderSection(sec, brief));
  });
  document.getElementById("sources-note").textContent = brief.sources_note || "";
  document.getElementById("disclaimer").textContent = brief.disclaimer || "";
}
main().catch((e) => {
  document.getElementById("brief-date").textContent = "简报加载失败";
  console.error(e);
});
