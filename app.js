/* China Macro Pulse — dashboard renderer
 * Reads data/history.json (self-describing: each series carries its own
 * label/unit/format) and renders KPI tiles + interactive line charts per
 * category tab, plus a news rail. No build step — plain DOM + inline SVG.
 */

const CAT_ORDER = ["growth", "prices_credit", "external", "property_labor"];
const CAT_COLOR_VAR = {
  growth: "--cat-growth",
  prices_credit: "--cat-prices",
  external: "--cat-external",
  property_labor: "--cat-property",
};

const GLOSSARY = {
  GDP: "Gross Domestic Product",
  PMI: "Purchasing Managers' Index",
  CPI: "Consumer Price Index",
  PPI: "Producer Price Index",
  LPR: "Loan Prime Rate",
  RRR: "Reserve Requirement Ratio",
  TSF: "Total Social Financing",
  M2: "Broad money supply — cash, deposits, and near-money",
  YTD: "Year-to-Date",
  NBS: "National Bureau of Statistics of China",
  PBOC: "People's Bank of China",
  GACC: "General Administration of Customs of China",
  SAFE: "State Administration of Foreign Exchange of China",
  ECB: "European Central Bank",
  Caixin: "Caixin Media, publisher of China's private-sector PMI survey",
};
const ACRONYM_RE = new RegExp("\\b(" + Object.keys(GLOSSARY).join("|") + ")\\b", "g");

// Which body sets each series — shown as a small credit line below its chart.
const SOURCE_MAP = {
  gdp_growth: "NBS", industrial_production: "NBS", retail_sales: "NBS",
  fixed_asset_investment: "NBS", official_manufacturing_pmi: "NBS",
  official_non_manufacturing_pmi: "NBS", cpi: "NBS", ppi: "NBS",
  property_investment: "NBS", new_home_prices: "NBS",
  urban_unemployment: "NBS", youth_unemployment: "NBS",
  caixin_manufacturing_pmi: "Caixin/S&P Global",
  lpr_1y: "PBOC", lpr_5y: "PBOC", rrr: "PBOC", m2_growth: "PBOC", tsf_flow: "PBOC",
  exports_yoy: "GACC", imports_yoy: "GACC", trade_balance: "GACC",
  fx_reserves: "SAFE",
  usdcny: "ECB, via Frankfurter",
};

let DATA = null;
let currentRange = 12; // months of history to show; 0 = all
let currentCat = "growth";

init();

async function init() {
  bindFilterRow();
  bindTabs();
  try {
    const res = await fetch("data/history.json", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    DATA = await res.json();
  } catch (err) {
    renderFetchError(err);
    return;
  }
  renderMeta();
  renderPanels();
  renderNews();
}

function renderFetchError(err) {
  const panels = document.getElementById("panels");
  panels.textContent = "";
  const p = document.createElement("p");
  p.className = "no-data";
  p.textContent = "Couldn't load data/history.json (" + err.message + "). If you're opening this file directly from disk, serve it over a local HTTP server instead — browsers block file:// fetches.";
  panels.appendChild(p);
}

function bindFilterRow() {
  const group = document.getElementById("range-group");
  group.addEventListener("click", (e) => {
    const btn = e.target.closest(".range-btn");
    if (!btn) return;
    group.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentRange = Number(btn.dataset.range);
    renderPanels();
  });
}

function bindTabs() {
  const tabs = document.getElementById("tabs");
  tabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    tabs.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentCat = btn.dataset.cat;
    renderPanels();
  });
}

function renderMeta() {
  const el = document.getElementById("asof-date");
  if (DATA.meta && DATA.meta.last_updated) {
    const d = new Date(DATA.meta.last_updated);
    el.textContent = isNaN(d) ? DATA.meta.last_updated : d.toISOString().slice(0, 10);
  }
  const footer = document.getElementById("footer-sources");
  footer.textContent = "";
  const line1 = document.createElement("div");
  line1.textContent = "Refreshed automatically on a schedule via GitHub Actions. Figures are the latest publicly reported prints and are subject to revision. Not investment advice.";
  footer.appendChild(line1);
  if (DATA.meta && Array.isArray(DATA.meta.sources) && DATA.meta.sources.length) {
    const line2 = document.createElement("div");
    line2.style.marginTop = "6px";
    line2.textContent = "Sources: " + DATA.meta.sources.join(" · ");
    footer.appendChild(line2);
  }
}

function renderPanels() {
  const panelsRoot = document.getElementById("panels");
  panelsRoot.textContent = "";

  CAT_ORDER.forEach((catKey) => {
    const cat = DATA.categories && DATA.categories[catKey];
    const panel = document.createElement("div");
    panel.className = "panel" + (catKey === currentCat ? " active" : "");
    panel.id = "panel-" + catKey;

    if (!cat || !cat.series || Object.keys(cat.series).length === 0) {
      const p = document.createElement("p");
      p.className = "no-data";
      p.textContent = "No data yet for this category.";
      panel.appendChild(p);
      panelsRoot.appendChild(panel);
      return;
    }

    const seriesEntries = Object.entries(cat.series);

    // KPI row
    const kpiGrid = document.createElement("div");
    kpiGrid.className = "kpi-grid";
    seriesEntries.forEach(([key, series]) => kpiGrid.appendChild(renderTile(series)));
    panel.appendChild(kpiGrid);

    // Chart grid
    const chartGrid = document.createElement("div");
    chartGrid.className = "chart-grid";
    seriesEntries.forEach(([key, series]) => {
      chartGrid.appendChild(renderChartCard(series, catKey, key));
    });
    panel.appendChild(chartGrid);

    panelsRoot.appendChild(panel);
  });
}

function glossSpan(term) {
  const span = document.createElement("span");
  span.className = "gloss";
  span.textContent = term;
  const def = GLOSSARY[term];
  if (def) {
    span.dataset.def = def;
    span.tabIndex = 0;
    const q = document.createElement("sup");
    q.className = "gloss-q";
    q.textContent = "?";
    span.appendChild(q);
  }
  return span;
}

// Appends `text` into `container` as DOM nodes, wrapping any recognized
// acronym (GDP, PMI, CPI, ...) in a hoverable glossary span.
function appendGlossedText(container, text) {
  ACRONYM_RE.lastIndex = 0;
  let lastIndex = 0;
  let match;
  while ((match = ACRONYM_RE.exec(text))) {
    if (match.index > lastIndex) {
      container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    container.appendChild(glossSpan(match[0]));
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    container.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

function fmtValue(value, series) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const decimals = series.decimals ?? 1;
  const sign = series.show_sign && value > 0 ? "+" : "";
  return sign + value.toFixed(decimals);
}

function seriesRange(series) {
  const pts = series.data || [];
  if (currentRange === 0) return pts;
  let pointsPerMonth = 1;
  if (series.freq === "quarterly") pointsPerMonth = 1 / 3;
  else if (series.freq === "daily") pointsPerMonth = 21; // trading days/month
  const n = Math.max(2, Math.round(currentRange * pointsPerMonth));
  return pts.slice(Math.max(0, pts.length - n));
}

function renderTile(series) {
  const tile = document.createElement("div");
  tile.className = "tile";

  const label = document.createElement("div");
  label.className = "label";
  label.textContent = series.label;
  tile.appendChild(label);

  const pts = series.data || [];
  const last = pts[pts.length - 1];
  const prev = pts[pts.length - 2];

  const row = document.createElement("div");
  row.className = "value-row";

  const val = document.createElement("span");
  val.className = "value";
  val.textContent = last ? fmtValue(last.value, series) : "—";
  row.appendChild(val);

  if (series.unit) {
    const unit = document.createElement("span");
    unit.className = "chip neutral";
    unit.textContent = series.unit;
    row.appendChild(unit);
  }

  if (last && prev && typeof last.value === "number" && typeof prev.value === "number") {
    const delta = last.value - prev.value;
    if (Math.abs(delta) > 1e-9) {
      const chip = document.createElement("span");
      // Default convention: up = green, down = red. Only flipped when a
      // series is explicitly marked higher_is_better === false (e.g.
      // unemployment, where a rise is bad).
      const cls = (series.higher_is_better === false)
        ? (delta > 0 ? "bad" : "good")
        : (delta > 0 ? "good" : "bad");
      chip.className = "chip " + cls;
      chip.textContent = (delta > 0 ? "▲ " : "▼ ") + Math.abs(delta).toFixed(series.decimals ?? 1);
      row.appendChild(chip);
    }
  }
  tile.appendChild(row);

  const period = document.createElement("div");
  period.className = "period";
  period.textContent = last ? last.date : "no data";
  tile.appendChild(period);

  return tile;
}

function renderChartCard(series, catKey, seriesKey) {
  const card = document.createElement("div");
  card.className = "chart-card";

  const pts = seriesRange(series).filter((p) => typeof p.value === "number");

  const head = document.createElement("div");
  head.className = "chart-head";

  const h3 = document.createElement("h3");
  appendGlossedText(h3, series.label);
  head.appendChild(h3);

  const latest = document.createElement("div");
  latest.className = "chart-latest";
  latest.textContent = pts.length ? fmtValue(pts[pts.length - 1].value, series) : "";
  head.appendChild(latest);

  card.appendChild(head);

  const sub = document.createElement("div");
  sub.className = "chart-sub";
  sub.textContent = (series.unit || "") + (series.freq ? " · " + series.freq : "");
  card.appendChild(sub);

  const svgWrap = document.createElement("div");
  svgWrap.className = "chart-svg-wrap";

  if (pts.length < 2) {
    const p = document.createElement("p");
    p.className = "no-data";
    p.textContent = "Not enough data points yet.";
    svgWrap.appendChild(p);
  } else {
    const colorVar = CAT_COLOR_VAR[catKey] || "--accent";
    const svg = buildLineChart(pts, colorVar, series);
    svgWrap.appendChild(svg.el);

    const tooltip = document.createElement("div");
    tooltip.className = "tooltip";
    svgWrap.appendChild(tooltip);

    wireHover(svg, svgWrap, tooltip, pts, series);
  }
  card.appendChild(svgWrap);

  const source = SOURCE_MAP[seriesKey];
  if (source) {
    const sourceLine = document.createElement("div");
    sourceLine.className = "chart-source";
    sourceLine.appendChild(document.createTextNode("Source: "));
    appendGlossedText(sourceLine, source);
    card.appendChild(sourceLine);
  }

  return card;
}

const CHART_W = 560, CHART_H = 130, PAD_L = 4, PAD_R = 4, PAD_T = 10, PAD_B = 18;

function buildLineChart(pts, colorVar, series) {
  const values = pts.map((p) => p.value);
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.12; max += span * 0.12;

  const plotW = CHART_W - PAD_L - PAD_R;
  const plotH = CHART_H - PAD_T - PAD_B;
  const x = (i) => PAD_L + (pts.length === 1 ? plotW / 2 : (i / (pts.length - 1)) * plotW);
  const y = (v) => PAD_T + plotH - ((v - min) / (max - min)) * plotH;

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "chart-svg");
  svg.setAttribute("viewBox", `0 0 ${CHART_W} ${CHART_H}`);
  svg.setAttribute("preserveAspectRatio", "none");

  // gridlines (3 horizontal)
  [0.0, 0.5, 1.0].forEach((t) => {
    const gy = PAD_T + plotH * t;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("class", "chart-grid-line");
    line.setAttribute("x1", PAD_L); line.setAttribute("x2", CHART_W - PAD_R);
    line.setAttribute("y1", gy); line.setAttribute("y2", gy);
    svg.appendChild(line);
  });

  // area fill
  const areaPts = pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" L ");
  const area = document.createElementNS(svgNS, "path");
  area.setAttribute("class", "chart-area");
  area.setAttribute("fill", `var(${colorVar})`);
  area.setAttribute("d", `M ${x(0)},${PAD_T + plotH} L ${areaPts} L ${x(pts.length - 1)},${PAD_T + plotH} Z`);
  svg.appendChild(area);

  // line
  const line = document.createElementNS(svgNS, "path");
  line.setAttribute("class", "chart-line");
  line.setAttribute("stroke", `var(${colorVar})`);
  line.setAttribute("d", `M ${areaPts}`);
  svg.appendChild(line);

  // end dot
  const endDot = document.createElementNS(svgNS, "circle");
  endDot.setAttribute("class", "chart-dot-end");
  endDot.setAttribute("cx", x(pts.length - 1));
  endDot.setAttribute("cy", y(pts[pts.length - 1].value));
  endDot.setAttribute("r", 3.5);
  endDot.setAttribute("fill", `var(${colorVar})`);
  endDot.setAttribute("stroke", "var(--surface-1)");
  svg.appendChild(endDot);

  // x-axis first/last date labels
  [0, pts.length - 1].forEach((i) => {
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("class", "chart-axis-label");
    t.setAttribute("x", x(i));
    t.setAttribute("y", CHART_H - 3);
    t.setAttribute("text-anchor", i === 0 ? "start" : "end");
    t.textContent = pts[i].date;
    svg.appendChild(t);
  });

  // crosshair
  const crossLine = document.createElementNS(svgNS, "line");
  crossLine.setAttribute("class", "crosshair-line");
  crossLine.setAttribute("y1", PAD_T); crossLine.setAttribute("y2", PAD_T + plotH);
  svg.appendChild(crossLine);

  const crossDot = document.createElementNS(svgNS, "circle");
  crossDot.setAttribute("class", "crosshair-dot");
  crossDot.setAttribute("r", 4.5);
  crossDot.setAttribute("fill", "var(--surface-1)");
  crossDot.setAttribute("stroke", `var(${colorVar})`);
  svg.appendChild(crossDot);

  // hover capture (bigger than plot, per interaction.md hit-target rule)
  const capture = document.createElementNS(svgNS, "rect");
  capture.setAttribute("class", "hover-capture");
  capture.setAttribute("x", 0); capture.setAttribute("y", 0);
  capture.setAttribute("width", CHART_W); capture.setAttribute("height", CHART_H);
  svg.appendChild(capture);

  return { el: svg, x, y, capture, crossLine, crossDot, plotW, pts };
}

function wireHover(svg, wrap, tooltip, pts, series) {
  const moveTo = (clientX) => {
    const rect = svg.capture.ownerSVGElement.getBoundingClientRect();
    const relX = ((clientX - rect.left) / rect.width) * CHART_W;
    let idx = Math.round(((relX - PAD_L) / svg.plotW) * (pts.length - 1));
    idx = Math.max(0, Math.min(pts.length - 1, idx));
    const px = svg.x(idx), py = svg.y(pts[idx].value);

    svg.crossLine.setAttribute("x1", px); svg.crossLine.setAttribute("x2", px);
    svg.crossLine.style.opacity = 1;
    svg.crossDot.setAttribute("cx", px); svg.crossDot.setAttribute("cy", py);
    svg.crossDot.style.opacity = 1;

    tooltip.style.opacity = 1;
    const wrapRect = wrap.getBoundingClientRect();
    const svgRect = svg.capture.ownerSVGElement.getBoundingClientRect();
    const leftPx = svgRect.left - wrapRect.left + (px / CHART_W) * svgRect.width;
    const topPx = svgRect.top - wrapRect.top + (py / CHART_H) * svgRect.height;
    tooltip.style.left = leftPx + "px";
    tooltip.style.top = (topPx - 8) + "px";

    tooltip.textContent = "";
    const dateEl = document.createElement("div");
    dateEl.className = "tt-date";
    dateEl.textContent = pts[idx].date;
    const valEl = document.createElement("div");
    valEl.className = "tt-value";
    valEl.textContent = fmtValue(pts[idx].value, series) + (series.unit ? " " + series.unit : "");
    tooltip.appendChild(dateEl);
    tooltip.appendChild(valEl);
  };

  const hide = () => {
    svg.crossLine.style.opacity = 0;
    svg.crossDot.style.opacity = 0;
    tooltip.style.opacity = 0;
  };

  svg.capture.addEventListener("pointermove", (e) => moveTo(e.clientX));
  svg.capture.addEventListener("pointerleave", hide);
  svg.capture.addEventListener("pointerdown", (e) => moveTo(e.clientX));
}

function fmtNewsDate(raw) {
  if (!raw) return "";
  const d = new Date(raw);
  if (isNaN(d)) return raw;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function renderNews() {
  const list = document.getElementById("news-list");
  list.textContent = "";
  const items = [...(DATA.news || [])].sort((a, b) => new Date(b.date) - new Date(a.date));
  if (!items.length) {
    const p = document.createElement("p");
    p.className = "no-data";
    p.textContent = "No headlines yet.";
    list.appendChild(p);
    return;
  }
  items.forEach((item) => {
    const a = document.createElement(item.url ? "a" : "div");
    a.className = "news-item";
    if (item.url) { a.href = item.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }

    const head = document.createElement("div");
    head.className = "n-head";
    head.textContent = item.headline || "";
    a.appendChild(head);

    if (item.summary) {
      const sum = document.createElement("div");
      sum.className = "n-sum";
      sum.textContent = item.summary;
      a.appendChild(sum);
    }

    const meta = document.createElement("div");
    meta.className = "n-meta";
    meta.textContent = [item.source, fmtNewsDate(item.date)].filter(Boolean).join(" · ");
    a.appendChild(meta);

    list.appendChild(a);
  });
}
