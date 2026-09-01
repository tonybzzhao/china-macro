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
  M1: "Narrow money supply — cash in circulation plus readily-spendable demand deposits",
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

// Which body sets each series — shown as a clickable credit line below its
// chart, linking to that body's own site.
const SOURCES = {
  NBS: { label: "NBS", url: "https://www.stats.gov.cn/english/", def: "National Bureau of Statistics of China" },
  PBOC: { label: "PBOC", url: "http://www.pbc.gov.cn/en/3688006/index.html", def: "People's Bank of China" },
  GACC: { label: "GACC", url: "http://english.customs.gov.cn/", def: "General Administration of Customs of China" },
  SAFE: { label: "SAFE", url: "https://www.safe.gov.cn/en/", def: "State Administration of Foreign Exchange of China" },
  CAIXIN: { label: "Caixin / S&P Global", url: "https://www.caixinglobal.com/", def: "Caixin Media, publisher of China's private-sector PMI survey (compiled with S&P Global)" },
  FRANKFURTER: { label: "ECB, via Frankfurter", url: "https://www.frankfurter.app/", def: "European Central Bank reference rate, served via the free Frankfurter API" },
};
const SOURCE_MAP = {
  gdp_growth: "NBS", industrial_production: "NBS", retail_sales: "NBS",
  fixed_asset_investment: "NBS", official_manufacturing_pmi: "NBS",
  official_non_manufacturing_pmi: "NBS", cpi: "NBS", ppi: "NBS",
  property_investment: "NBS", new_home_prices: "NBS",
  urban_unemployment: "NBS", youth_unemployment: "NBS",
  caixin_manufacturing_pmi: "CAIXIN",
  lpr_1y: "PBOC", lpr_5y: "PBOC", rrr: "PBOC", m1_growth: "PBOC", m2_growth: "PBOC",
  m1_m2_gap: "PBOC", tsf_flow: "PBOC",
  exports_yoy: "GACC", imports_yoy: "GACC", trade_balance: "GACC",
  fx_reserves: "SAFE",
  usdcny: "FRANKFURTER",
};

let DATA = null;
let currentRange = 12; // months of history to show; 0 = all
let currentCat = "growth";
let searchQuery = "";

init();
initBeijingClock();

// SSE/SZSE trading hours (9:30-11:30, 13:00-15:00 China Standard Time).
// Whether a given DAY is a trading day comes from DATA.trading_days — the
// actual exchange-published calendar (fetched via akshare's
// tool_trade_date_hist_sina), which correctly handles statutory holidays
// AND the 调休 substitute workdays that swap a weekend for a weekday or
// vice versa. A plain Mon-Fri check gets this wrong on 13+ days a year.
// Falls back to a weekday guess only in the brief window before DATA has
// loaded, or if the calendar is ever missing/empty.
function getBeijingParts(date) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai", weekday: "short",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const out = {};
  fmt.formatToParts(date).forEach((p) => { out[p.type] = p.value; });
  out.isoDate = `${out.year}-${out.month}-${out.day}`;
  return out;
}

function isMarketOpen(parts) {
  const mins = parseInt(parts.hour, 10) * 60 + parseInt(parts.minute, 10);
  const morning = mins >= 9 * 60 + 30 && mins < 11 * 60 + 30;
  const afternoon = mins >= 13 * 60 && mins < 15 * 60;
  const inHours = morning || afternoon;

  const calendar = DATA && Array.isArray(DATA.trading_days) ? DATA.trading_days : null;
  const isTradingDay = calendar && calendar.length
    ? calendar.includes(parts.isoDate)
    : parts.weekday !== "Sat" && parts.weekday !== "Sun"; // pre-DATA fallback

  return isTradingDay && inHours;
}

function initBeijingClock() {
  const timeEl = document.getElementById("beijing-time");
  const badgeEl = document.getElementById("market-status");
  const labelEl = badgeEl ? badgeEl.querySelector(".market-label") : null;
  if (!timeEl || !badgeEl || !labelEl) return;

  function tick() {
    const parts = getBeijingParts(new Date());
    timeEl.textContent = `${parts.hour}:${parts.minute}:${parts.second}`;
    const open = isMarketOpen(parts);
    labelEl.textContent = open ? "Market open" : "Market closed";
    badgeEl.classList.toggle("open", open);
    badgeEl.classList.toggle("closed", !open);
  }
  tick();
  setInterval(tick, 1000);
}

async function init() {
  bindThemeSwitch();
  bindFilterRow();
  bindTabs();
  bindSearch();
  bindCollapseSections();
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
  renderUpcomingReleases();
}

function bindCollapseSections() {
  document.querySelectorAll(".collapse-header").forEach((btn) => {
    const key = "collapse:" + btn.dataset.collapse;
    const wrap = btn.closest(".collapse-section").querySelector(".collapse-body-wrap");
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(key) === "1";
    } catch (e) {}
    if (collapsed) {
      wrap.classList.add("collapsed");
      btn.setAttribute("aria-expanded", "false");
    }
    btn.addEventListener("click", () => {
      const nowCollapsed = wrap.classList.toggle("collapsed");
      btn.setAttribute("aria-expanded", String(!nowCollapsed));
      try {
        localStorage.setItem(key, nowCollapsed ? "1" : "0");
      } catch (e) {}
    });
  });
}

function findSeries(seriesKey) {
  for (const catKey of CAT_ORDER) {
    const cat = DATA.categories[catKey];
    if (cat && cat.series && cat.series[seriesKey]) return cat.series[seriesKey];
  }
  return null;
}

function findCategoryFor(seriesKey) {
  return CAT_ORDER.find((catKey) => DATA.categories[catKey]?.series?.[seriesKey]);
}

// Switches to the series' tab (clearing any active search first), scrolls
// the chart into view, and flashes it — used by Upcoming Releases items to
// link straight to their chart.
function jumpToChart(seriesKey) {
  if (searchQuery) {
    searchQuery = "";
    document.getElementById("search-input").value = "";
  }
  const catKey = findCategoryFor(seriesKey);
  if (catKey) currentCat = catKey;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.cat === currentCat));
  renderPanels();

  const card = document.getElementById("chart-" + seriesKey);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("linked-flash");
  // restart the CSS animation even if it was just played
  void card.offsetWidth;
  card.classList.add("linked-flash");
}

function fmtReleaseDate(iso) {
  const d = new Date(iso + "T00:00:00");
  if (isNaN(d)) return iso;
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function renderUpcomingReleases() {
  const list = document.getElementById("releases-list");
  list.textContent = "";
  // The backend already drops past events at fetch time, but the GitHub
  // Actions schedule runs unreliably (documented above — gaps of several
  // hours are common), so data/history.json can go stale between runs.
  // Re-filter here against the live Beijing clock so a same-day release
  // that has already happened never lingers as "upcoming" just because
  // the next scheduled refresh hasn't landed yet.
  const nowParts = getBeijingParts(new Date());
  const nowKey = `${nowParts.isoDate} ${nowParts.hour}:${nowParts.minute}`;
  const items = (DATA.upcoming_releases || []).filter(
    (item) => `${item.date} ${item.time || "00:00"}` >= nowKey
  );
  if (!items.length) {
    const p = document.createElement("p");
    p.className = "no-data";
    p.textContent = "No upcoming releases found.";
    list.appendChild(p);
    return;
  }

  // Group by date so the date renders once per group instead of once per
  // item — several releases (e.g. CPI + PPI, or the mid-month batch of
  // IP/retail/FAI/unemployment) always land on the same day.
  let lastDate = null;
  let group = null;
  items.forEach((item) => {
    if (item.date !== lastDate) {
      lastDate = item.date;
      const heading = document.createElement("div");
      heading.className = "release-date-heading";
      heading.textContent = fmtReleaseDate(item.date);
      list.appendChild(heading);
      group = document.createElement("div");
      group.className = "release-group";
      list.appendChild(group);
    }

    const series = findSeries(item.series_key);

    const btn = document.createElement("button");
    btn.className = "release-item";
    btn.addEventListener("click", () => jumpToChart(item.series_key));

    const main = document.createElement("div");
    main.className = "release-main";
    const eventEl = document.createElement("div");
    eventEl.className = "r-event";
    eventEl.textContent = series ? series.label : item.series_key;
    main.appendChild(eventEl);

    const figs = document.createElement("div");
    figs.className = "r-figures";
    const bits = [];
    if (item.time) bits.push(item.time + " CST");
    if (item.consensus != null) bits.push("cons " + item.consensus);
    if (item.previous != null) bits.push("prev " + item.previous);
    figs.textContent = bits.join(" · ");
    main.appendChild(figs);

    btn.appendChild(main);
    group.appendChild(btn);
  });
}

function renderFetchError(err) {
  const panels = document.getElementById("panels");
  panels.textContent = "";
  const p = document.createElement("p");
  p.className = "no-data";
  p.textContent = "Couldn't load data/history.json (" + err.message + "). If you're opening this file directly from disk, serve it over a local HTTP server instead — browsers block file:// fetches.";
  panels.appendChild(p);
}

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

function bindThemeSwitch() {
  const buttons = document.querySelectorAll(".theme-btn");
  let stored = "system";
  try {
    stored = localStorage.getItem("theme") || "system";
  } catch (e) {}

  buttons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeChoice === stored);
    btn.addEventListener("click", () => {
      const choice = btn.dataset.themeChoice;
      applyTheme(choice);
      try {
        localStorage.setItem("theme", choice);
      } catch (e) {}
      buttons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });
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

function bindSearch() {
  const input = document.getElementById("search-input");
  input.addEventListener("input", () => {
    searchQuery = input.value.trim();
    renderPanels();
    renderNews();
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
  const tabsEl = document.getElementById("tabs");

  if (searchQuery) {
    tabsEl.style.display = "none";
    panelsRoot.appendChild(renderSearchResults());
    return;
  }
  tabsEl.style.display = "";

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

// Search mode ignores the active tab and shows matching indicators from
// every category in one flat list.
function renderSearchResults() {
  const panel = document.createElement("div");
  panel.className = "panel active";

  const q = searchQuery.toLowerCase();
  const matches = [];
  CAT_ORDER.forEach((catKey) => {
    const cat = DATA.categories && DATA.categories[catKey];
    if (!cat || !cat.series) return;
    Object.entries(cat.series).forEach(([key, series]) => {
      if (series.label.toLowerCase().includes(q)) matches.push([catKey, key, series]);
    });
  });

  const heading = document.createElement("p");
  heading.className = "section-label";
  heading.textContent = matches.length
    ? `${matches.length} indicator${matches.length === 1 ? "" : "s"} matching "${searchQuery}"`
    : `No indicators matching "${searchQuery}"`;
  panel.appendChild(heading);

  if (matches.length) {
    const kpiGrid = document.createElement("div");
    kpiGrid.className = "kpi-grid";
    matches.forEach(([, , series]) => kpiGrid.appendChild(renderTile(series)));
    panel.appendChild(kpiGrid);

    const chartGrid = document.createElement("div");
    chartGrid.className = "chart-grid";
    matches.forEach(([catKey, key, series]) => chartGrid.appendChild(renderChartCard(series, catKey, key)));
    panel.appendChild(chartGrid);
  }

  return panel;
}

function glossSpan(term) {
  const span = document.createElement("span");
  span.className = "gloss";
  span.textContent = term;
  const def = GLOSSARY[term];
  if (def) {
    span.dataset.def = def;
    span.tabIndex = 0;
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
  card.id = "chart-" + seriesKey;
  card.dataset.seriesKey = seriesKey;
  card.dataset.cat = catKey;

  const pts = seriesRange(series).filter((p) => typeof p.value === "number");

  const head = document.createElement("div");
  head.className = "chart-head";

  const h3 = document.createElement("h3");
  appendGlossedText(h3, series.label);
  head.appendChild(h3);

  // Grouped into one flex item so the figures always sit flush-right,
  // regardless of how long/short the title is (previously a 3-way
  // space-between let short titles leave the value floating mid-row).
  const figures = document.createElement("div");
  figures.className = "chart-figures";

  const valueBlock = document.createElement("div");
  valueBlock.className = "chart-value-block";

  const latest = document.createElement("span");
  latest.className = "chart-latest-value";
  latest.style.color = `var(${CAT_COLOR_VAR[catKey] || "--accent"})`;
  latest.textContent = pts.length ? fmtValue(pts[pts.length - 1].value, series) : "";
  valueBlock.appendChild(latest);

  if (pts.length > 1) {
    const prevVal = pts[pts.length - 2].value;
    const prev = document.createElement("span");
    prev.className = "chart-latest-prev";
    prev.textContent = `prev ${fmtValue(prevVal, series)}`;
    valueBlock.appendChild(prev);
  }
  figures.appendChild(valueBlock);

  if (series.desc) {
    const chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.setAttribute("class", "chart-chevron");
    chevron.setAttribute("viewBox", "0 0 16 16");
    chevron.setAttribute("aria-hidden", "true");
    chevron.innerHTML = '<path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>';
    figures.appendChild(chevron);
  }
  head.appendChild(figures);

  card.appendChild(head);

  const sub = document.createElement("div");
  sub.className = "chart-sub";
  sub.textContent = (series.unit || "") + (series.freq ? " · " + series.freq : "");
  card.appendChild(sub);

  if (series.desc) {
    head.classList.add("chart-head-toggle");
    head.tabIndex = 0;
    head.setAttribute("role", "button");
    head.setAttribute("aria-expanded", "false");

    const descWrap = document.createElement("div");
    descWrap.className = "chart-desc-wrap";
    const descInner = document.createElement("div");
    descInner.className = "chart-desc-inner";
    const descP = document.createElement("p");
    descP.className = "chart-desc";
    descP.textContent = series.desc;
    descInner.appendChild(descP);
    descWrap.appendChild(descInner);
    card.appendChild(descWrap);

    const toggle = () => {
      const expanded = descWrap.classList.toggle("expanded");
      head.setAttribute("aria-expanded", String(expanded));
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });
  }

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

  const sourceKey = SOURCE_MAP[seriesKey];
  const source = sourceKey && SOURCES[sourceKey];
  if (source) {
    const sourceLine = document.createElement("div");
    sourceLine.className = "chart-source";
    sourceLine.appendChild(document.createTextNode("Source: "));
    const link = document.createElement("a");
    // Both a real link (click -> the source's own site) and a glossary
    // term (hover/focus -> full name), via the shared .gloss popup styles.
    link.className = "gloss source-link";
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = source.label;
    link.dataset.def = source.def;
    sourceLine.appendChild(link);
    card.appendChild(sourceLine);
  }

  return card;
}

const CHART_W = 560, CHART_H = 130, PAD_L = 4, PAD_R = 4, PAD_T = 10, PAD_B = 18;

// Converts a series date string to a sortable numeric ordinal so tick
// spacing can be computed by actual elapsed time rather than array index —
// matters whenever a series has gaps (e.g. a skipped Jan/Feb combined
// month), where evenly-spaced *indices* would not be evenly-spaced *time*.
function dateToOrdinal(dateStr) {
  let m = dateStr.match(/^(\d{4})-Q(\d)$/);
  if (m) return Number(m[1]) * 4 + (Number(m[2]) - 1);
  m = dateStr.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 86400000;
  m = dateStr.match(/^(\d{4})-(\d{2})$/);
  if (m) return Number(m[1]) * 12 + (Number(m[2]) - 1);
  return NaN;
}

function buildLineChart(pts, colorVar, series) {
  const values = pts.map((p) => p.value);
  let min = Math.min(...values), max = Math.max(...values);
  if (typeof series.threshold === "number") {
    min = Math.min(min, series.threshold);
    max = Math.max(max, series.threshold);
  }
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
  // Uniform scaling (no independent X/Y stretch) — safe because the CSS
  // aspect-ratio on .chart-svg is locked to match CHART_W/CHART_H exactly.
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

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

  // threshold reference line (e.g. PMI's 50 expansion/contraction line) —
  // drawn in the accent color, after the area fill but under the data
  // line so the series itself stays the most prominent element.
  if (typeof series.threshold === "number") {
    const ty = y(series.threshold);
    const thresholdLine = document.createElementNS(svgNS, "line");
    thresholdLine.setAttribute("class", "chart-threshold-line");
    thresholdLine.setAttribute("x1", PAD_L);
    thresholdLine.setAttribute("x2", CHART_W - PAD_R);
    thresholdLine.setAttribute("y1", ty);
    thresholdLine.setAttribute("y2", ty);
    svg.appendChild(thresholdLine);

    const thresholdLabel = document.createElementNS(svgNS, "text");
    thresholdLabel.setAttribute("class", "chart-threshold-label");
    thresholdLabel.setAttribute("x", CHART_W - PAD_R);
    thresholdLabel.setAttribute("y", ty - 3);
    thresholdLabel.setAttribute("text-anchor", "end");
    thresholdLabel.textContent = String(series.threshold);
    svg.appendChild(thresholdLabel);
  }

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

  // x-axis date labels — spaced at even TIME intervals (not just evenly
  // across array indices, which would misrepresent gappy series — e.g. a
  // series missing a combined Jan/Feb month would otherwise show ticks
  // that look evenly spaced but skip unevenly through actual time).
  const tickCount = Math.min(6, pts.length);
  const ordinals = pts.map((p) => dateToOrdinal(p.date));
  const validOrdinals = ordinals.every((o) => !Number.isNaN(o));
  let tickIndices;
  if (validOrdinals && pts.length > 1) {
    const lo = ordinals[0], hi = ordinals[ordinals.length - 1];
    tickIndices = [...new Set(
      Array.from({ length: tickCount }, (_, i) => {
        const target = lo + (i / (tickCount - 1)) * (hi - lo);
        let best = 0, bestDiff = Infinity;
        for (let j = 0; j < ordinals.length; j++) {
          const diff = Math.abs(ordinals[j] - target);
          if (diff < bestDiff) { bestDiff = diff; best = j; }
        }
        return best;
      })
    )].sort((a, b) => a - b);
  } else {
    tickIndices = [...new Set(
      Array.from({ length: tickCount }, (_, i) =>
        Math.round((i / (tickCount - 1)) * (pts.length - 1))
      )
    )];
  }
  tickIndices.forEach((i) => {
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("class", "chart-axis-label");
    t.setAttribute("x", x(i));
    t.setAttribute("y", CHART_H - 3);
    let anchor = "middle";
    if (i === 0) anchor = "start";
    else if (i === pts.length - 1) anchor = "end";
    t.setAttribute("text-anchor", anchor);
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

const NEWS_BATCH_SIZE = 12;
let newsQueue = [];
let newsShownCount = 0;
let newsObserver = null;

function renderNewsItem(item) {
  const a = document.createElement(item.url ? "a" : "div");
  a.className = "news-item";
  if (item.url) { a.href = item.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }

  const head = document.createElement("div");
  head.className = "n-head";
  head.textContent = item.headline || "";
  a.appendChild(head);

  const meta = document.createElement("div");
  meta.className = "n-meta";
  meta.textContent = [item.source, fmtNewsDate(item.date)].filter(Boolean).join(" · ");
  a.appendChild(meta);

  return a;
}

function renderNews() {
  const list = document.getElementById("news-list");
  list.textContent = "";
  if (newsObserver) { newsObserver.disconnect(); newsObserver = null; }

  let items = [...(DATA.news || [])].sort((a, b) => new Date(b.date) - new Date(a.date));
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    items = items.filter((i) =>
      (i.headline || "").toLowerCase().includes(q) ||
      (i.summary || "").toLowerCase().includes(q) ||
      (i.source || "").toLowerCase().includes(q)
    );
  }
  if (!items.length) {
    const p = document.createElement("p");
    p.className = "no-data";
    p.textContent = searchQuery ? `No headlines matching "${searchQuery}".` : "No headlines yet.";
    list.appendChild(p);
    return;
  }

  newsQueue = items;
  newsShownCount = 0;
  loadMoreNews();
}

// Renders the next batch and, if more remain, arms an IntersectionObserver
// on a sentinel row so scrolling near the bottom of the (independently
// scrollable) news list keeps pulling in more of the already-fetched pool.
function loadMoreNews() {
  const list = document.getElementById("news-list");
  const oldSentinel = list.querySelector(".news-sentinel");
  if (oldSentinel) oldSentinel.remove();
  const oldEnd = list.querySelector(".news-end");
  if (oldEnd) oldEnd.remove();

  const next = newsQueue.slice(newsShownCount, newsShownCount + NEWS_BATCH_SIZE);
  next.forEach((item) => list.appendChild(renderNewsItem(item)));
  newsShownCount += next.length;

  if (newsShownCount < newsQueue.length) {
    const sentinel = document.createElement("div");
    sentinel.className = "news-sentinel";
    list.appendChild(sentinel);
    if (!newsObserver) {
      newsObserver = new IntersectionObserver(
        (entries) => {
          if (entries[0].isIntersecting) loadMoreNews();
        },
        { root: document.getElementById("section-news"), rootMargin: "200px" }
      );
    }
    newsObserver.observe(sentinel);
  } else {
    const end = document.createElement("p");
    end.className = "no-data news-end";
    end.textContent = "You're all caught up.";
    list.appendChild(end);
  }
}
