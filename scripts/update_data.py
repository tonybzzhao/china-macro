#!/usr/bin/env python3
"""Refresh data/history.json for the China Macro Pulse dashboard.

Design notes:
- China's monthly indicators don't have a stable free official API (unlike FRED
  for the US), so this script leans on `akshare` — a community-maintained
  library that wraps NBS/PBOC/Sina releases. Because akshare's column names can
  shift between versions, every fetcher is wrapped in try/except: one failing
  indicator logs a warning and leaves that series untouched rather than
  breaking the whole run.
- Series NOT wired here (retail sales, fixed asset investment, property
  investment, 70-city home prices, youth unemployment) don't have a confirmed
  akshare function as of this writing. They stay manually-seeded in
  data/history.json until a verified fetcher is added — see README.
- FX reserves and TSF flow get unit conversions (akshare reports raw units
  that don't match this dashboard's display units); the conversion factors
  are heuristics and worth spot-checking against the dashboard after the
  first live run.
"""
import json
import sys
import traceback
import re
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

import pandas as pd
import akshare as ak
import requests

try:
    import feedparser
except ImportError:
    feedparser = None

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "history.json"
MAX_POINTS = 1000  # effectively "keep everything the source gives" for monthly/quarterly data
# Per-series overrides — usdcny is daily; 6000 covers ~16 years, comfortably
# past Frankfurter's actual CNY history.
MAX_POINTS_OVERRIDE = {"usdcny": 10000}


def log(msg):
    print(f"[update_data] {msg}", file=sys.stderr)


def load_data():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        # allow_nan=False: fail loudly instead of writing an invalid `NaN`
        # token that would silently break the dashboard's fetch().json().
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def find_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def normalize_date(raw):
    """Best-effort normalize an akshare date value to YYYY-MM."""
    if isinstance(raw, (pd.Timestamp, datetime)):
        return raw.strftime("%Y-%m")
    s = str(raw).strip()
    m = re.match(r"(\d{4})[年\-/]?(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def merge_points(existing, new_points, max_points):
    """De-dupe by date (new wins), sort, cap to max_points."""
    by_date = {p["date"]: p for p in existing}
    for p in new_points:
        if p.get("value") is None:
            continue
        by_date[p["date"]] = p
    merged = sorted(by_date.values(), key=lambda p: p["date"])
    return merged[-max_points:]


def update_series(data, category, key, new_points):
    if not new_points:
        log(f"SKIP {category}.{key}: fetcher returned no points")
        return
    try:
        series = data["categories"][category]["series"][key]
    except KeyError:
        log(f"SKIP {category}.{key}: not present in data/history.json schema")
        return
    before = len(series["data"])
    cap = MAX_POINTS_OVERRIDE.get(key, MAX_POINTS)
    series["data"] = merge_points(series["data"], new_points, cap)
    log(f"OK {category}.{key}: {before} -> {len(series['data'])} points")


def clean_float(raw):
    """float(raw), rejecting NaN/inf — a bare NaN is not valid JSON and would
    break the dashboard's fetch().json() call in the browser."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # v != v is the NaN check
        return None
    return v


def simple_series(fetch_fn, date_candidates, val_candidates):
    """Common pattern: akshare returns a DataFrame with one date col + one value col."""
    df = fetch_fn()
    date_col = find_col(df, date_candidates)
    val_col = find_col(df, val_candidates)
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    out = []
    for _, r in df.iterrows():
        v = clean_float(r[val_col])
        if v is None:
            continue
        out.append({"date": normalize_date(r[date_col]), "value": v})
    return out


# ---------- akshare-backed fetchers (confirmed function names) ----------

DATE_COLS = ["日期", "date", "月份", "TRADE_DATE"]
VAL_COLS = ["今值", "value", "现值"]

def fetch_cpi():
    return simple_series(ak.macro_china_cpi_monthly, DATE_COLS, VAL_COLS)

def fetch_ppi():
    return simple_series(ak.macro_china_ppi_yearly, DATE_COLS, VAL_COLS)

def fetch_pmi():
    return simple_series(ak.macro_china_pmi_yearly, DATE_COLS, VAL_COLS)

def fetch_cx_pmi():
    return simple_series(ak.macro_china_cx_pmi_yearly, DATE_COLS, VAL_COLS)

def fetch_non_man_pmi():
    return simple_series(ak.macro_china_non_man_pmi, DATE_COLS, VAL_COLS)

def fetch_gdp():
    """GDP releases mid-quarter-following-month (Q1->Apr, Q2->Jul, Q3->Oct,
    Q4->Jan of next year); map the release date to the quarter it reports on
    so it's comparable with this dashboard's hand-seeded 'YYYY-Q#' points."""
    df = ak.macro_china_gdp_yearly()
    date_col = find_col(df, DATE_COLS)
    val_col = find_col(df, VAL_COLS)
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    release_to_quarter = {1: (-1, 4), 4: (0, 1), 7: (0, 2), 10: (0, 3)}
    out = []
    for _, r in df.iterrows():
        v = clean_float(r[val_col])
        if v is None:
            continue
        raw = str(r[date_col])
        m = re.match(r"(\d{4})[年\-/]?(\d{1,2})", raw)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        if month not in release_to_quarter:
            continue
        year_offset, quarter = release_to_quarter[month]
        out.append({"date": f"{year + year_offset}-Q{quarter}", "value": v})
    return out

def fetch_industrial_production():
    return simple_series(ak.macro_china_industrial_production_yoy, DATE_COLS, VAL_COLS)

def fetch_exports():
    return simple_series(ak.macro_china_exports_yoy, DATE_COLS, VAL_COLS)

def fetch_imports():
    return simple_series(ak.macro_china_imports_yoy, DATE_COLS, VAL_COLS)

def fetch_trade_balance():
    return simple_series(ak.macro_china_trade_balance, DATE_COLS, VAL_COLS)

def _yyyy_dot_m_to_month(raw):
    """'2026.7' -> '2026-07'."""
    s = str(raw).strip()
    m = re.match(r"(\d{4})\.(\d{1,2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else s


def fetch_money_supply():
    """M1 and M2 y/y growth in one call — fresher than the old
    ak.macro_china_m2_yearly (which was verified stale, frozen ~Sep 2025).
    Also derives the M1-M2 'scissors gap', a widely-watched liquidity/
    sentiment signal in Chinese markets."""
    df = ak.macro_china_supply_of_money()
    date_col = find_col(df, ["统计时间"])
    m2_col = find_col(df, ["货币和准货币（广义货币M2）同比增长"])
    m1_col = find_col(df, ["货币(狭义货币M1)同比增长"])
    if not all([date_col, m2_col, m1_col]):
        raise ValueError(f"couldn't find expected columns in {list(df.columns)}")

    m1_pts, m2_pts, gap_pts = [], [], []
    for _, r in df.iterrows():
        m1v, m2v = clean_float(r[m1_col]), clean_float(r[m2_col])
        if m1v is None and m2v is None:
            continue
        d = _yyyy_dot_m_to_month(r[date_col])
        if m1v is not None:
            m1_pts.append({"date": d, "value": m1v})
        if m2v is not None:
            m2_pts.append({"date": d, "value": m2v})
        if m1v is not None and m2v is not None:
            gap_pts.append({"date": d, "value": round(m1v - m2v, 2)})
    return m1_pts, m2_pts, gap_pts

def fetch_urban_unemployment():
    return simple_series(
        ak.macro_china_urban_unemployment,
        DATE_COLS,
        VAL_COLS + ["全国城镇调查失业率"],
    )

def fetch_retail_sales():
    """Verified live 2026-08-29: latest row is 2026-07, matches the
    -6.7%-YTD-FAI-era news narrative (retail sales near-stagnant)."""
    df = ak.macro_china_consumer_goods_retail()
    date_col, val_col = find_col(df, ["月份"]), find_col(df, ["同比增长"])
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    out = []
    for _, r in df.iterrows():
        v = clean_float(r[val_col])
        if v is None:
            continue
        m = re.match(r"(\d{4})年(\d{1,2})月", str(r[date_col]))
        if not m:
            continue
        out.append({"date": f"{m.group(1)}-{int(m.group(2)):02d}", "value": v})
    return out

def fetch_fixed_asset_investment():
    """China only ever quotes FAI as cumulative year-to-date y/y growth (the
    single-month figure is noisy/rarely cited) — akshare's raw table gives
    the YTD RMB level (自年初累计), not the growth rate, so this computes
    the YTD y/y growth itself: this-year's YTD-through-month-M vs
    last-year's YTD-through-month-M. Verified 2026-08-29: matches the
    -6.7% figure from the Aug 2026 CNBC July-activity-data story."""
    df = ak.macro_china_gdzctz()
    date_col, cum_col = find_col(df, ["月份"]), find_col(df, ["自年初累计"])
    if date_col is None or cum_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")

    by_ym = {}
    for _, r in df.iterrows():
        m = re.match(r"(\d{4})年(\d{1,2})月", str(r[date_col]))
        v = clean_float(r[cum_col])
        if not m or v is None or v == 0:
            continue
        by_ym[(int(m.group(1)), int(m.group(2)))] = v

    out = []
    for (year, month), cum_this_year in by_ym.items():
        cum_last_year = by_ym.get((year - 1, month))
        if cum_last_year is None or cum_last_year == 0:
            continue
        growth = round((cum_this_year / cum_last_year - 1) * 100, 1)
        out.append({"date": f"{year}-{month:02d}", "value": growth})
    return out

def fetch_fx_reserves():
    pts = simple_series(ak.macro_china_fx_reserves_yearly, DATE_COLS, VAL_COLS)
    # akshare reports this series in 亿美元 ($100mn) units, e.g. 32920 -> $3.292trn.
    # Verified against a live run on 2026-08-29: raw 32920 for 2025-08 matches
    # the reported ~$3.29trn reserves level for that month.
    for p in pts:
        if p["value"] > 1000:
            p["value"] = round(p["value"] / 10000, 3)
    return pts

def fetch_tsf_flow():
    df = ak.macro_china_shrzgm()
    date_col = find_col(df, ["月份", "日期", "date"])
    val_col = find_col(df, ["社会融资规模增量", "value", "今值"])
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    out = []
    for _, r in df.iterrows():
        v = clean_float(r[val_col])
        if v is None:
            continue
        v = v / 10000  # akshare units: 亿元 (100mn RMB) -> RMB trillion
        out.append({"date": normalize_date(r[date_col]), "value": round(v, 2)})
    return out

def fetch_lpr():
    df = ak.macro_china_lpr()
    date_col = find_col(df, ["TRADE_DATE", "日期", "date"])
    col_1y = find_col(df, ["LPR1Y", "LPR_1Y", "1年"])
    col_5y = find_col(df, ["LPR5Y", "LPR_5Y", "5年"])
    if date_col is None:
        raise ValueError(f"couldn't find date column in {list(df.columns)}")
    out_1y, out_5y = [], []
    for _, r in df.iterrows():
        d = normalize_date(r[date_col])
        if col_1y and pd.notna(r.get(col_1y)):
            out_1y.append({"date": d, "value": float(r[col_1y])})
        if col_5y and pd.notna(r.get(col_5y)):
            out_5y.append({"date": d, "value": float(r[col_5y])})
    return out_1y, out_5y

def fetch_rrr():
    """Reserve Requirement Ratio for LARGE BANKS (大型银行) specifically.

    IMPORTANT: this is NOT the same category as what
    ak.macro_china_reserve_requirement_ratio's '大型金融机构' column tracks
    ("large financial institutions" — a different, ~1.5pp-HIGHER series).
    Verified directly on 2026-08-29 against two independent sources — PBOC's
    own live current-value page and CEIC — both showing large banks at
    7.5%, while akshare's own column showed 9.0% for the same date. This
    was flagged by the user recognizing the akshare category was wrong,
    not a data-freshness problem.

    Strategy: scrape PBOC's page for the authoritative CURRENT value (this
    is the part that must be exactly right), then reconstruct history by
    taking akshare's own cut TIMING and MAGNITUDES (verified correct — same
    dates/sizes reported in the press) and shifting the whole series by a
    constant offset so the latest point matches the verified current value
    exactly. The offset has been confirmed constant across the last several
    cuts (each PBOC action applies the same magnitude to every tier) but
    isn't independently verified before the modern tiered system was
    established (~2015) — earlier history is a reasonable approximation,
    not independently confirmed.
    """
    resp = requests.get(
        "https://www.pbc.gov.cn/rmyh/4027845/index.html",
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    m = re.search(r"大型银行.*?([\d.]+)\s*%", resp.text, re.S)
    if not m:
        raise ValueError("couldn't find 大型银行 RRR figure on PBOC page")
    current_large_bank_rrr = float(m.group(1))

    df = ak.macro_china_reserve_requirement_ratio()
    date_col = find_col(df, ["生效时间", "公布时间"])
    val_col = find_col(df, ["大型金融机构-调整后"])
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    raw = []
    for _, r in df.iterrows():
        v = clean_float(r[val_col])
        if v is None:
            continue
        raw.append({"date": normalize_date(r[date_col]), "value": v})
    raw.sort(key=lambda p: p["date"])
    if not raw:
        raise ValueError("akshare RRR table returned no rows")

    offset = current_large_bank_rrr - raw[-1]["value"]
    out = [{"date": p["date"], "value": round(p["value"] + offset, 3)} for p in raw]
    out[-1]["value"] = current_large_bank_rrr  # exact, not offset-rounded

    # Forward-fill a "this month" point at the current (unchanged) rate —
    # RRR only has a real data point on the rare month PBOC acts, which
    # otherwise makes the chart's rightmost point look stale for the many
    # months nothing happens (verified: MacroMicro's equivalent chart does
    # the same forward-fill, showing "2026-07" for a rate that last
    # actually changed in 2025-05). Harmless to run every time — it just
    # overwrites the same current-month key until the month rolls over.
    this_month = date.today().strftime("%Y-%m")
    if out[-1]["date"] != this_month:
        out.append({"date": this_month, "value": current_large_bank_rrr})
    return out

def fetch_trading_calendar():
    """SSE/SZSE official trading-day calendar (accounts for holidays AND
    the 调休 substitute workdays that make Chinese holiday weeks irregular
    — e.g. a Saturday can be a trading day, a plain Friday can be closed).
    Used client-side for the Beijing-time market-open indicator instead of
    a naive Mon-Fri check, which is wrong on ~13+ days a year."""
    df = ak.tool_trade_date_hist_sina()
    dates = df["trade_date"].astype(str).tolist()
    # Keep last 2 years -> ~1.5 years ahead (whatever Sina has published);
    # the widget only ever checks "today", no need to ship the full
    # history back to the 1990s.
    cutoff = (date.today() - timedelta(days=730)).isoformat()
    return sorted(d for d in dates if d >= cutoff)

def fetch_usdcny_daily(start="1999-01-04"):
    """Daily USD/CNY via Frankfurter (ECB reference rates) — free, no key,
    genuinely live. Replaces ak.macro_china_rmb, which is dead (stops 2021).
    1999-01-04 is the first date ECB reference rates are published for."""
    end = date.today().isoformat()
    resp = requests.get(
        f"https://api.frankfurter.app/{start}..{end}",
        params={"from": "USD", "to": "CNY"},
        timeout=20,
    )
    resp.raise_for_status()
    rates = resp.json().get("rates", {})
    return [{"date": d, "value": v["CNY"]} for d, v in sorted(rates.items()) if "CNY" in v]


# Verified live against a real run on 2026-08-29 — data current as of that date:
FRESH_FETCHERS = [
    ("prices_credit", "tsf_flow", fetch_tsf_flow),                       # through 2026-04
    ("property_labor", "urban_unemployment", fetch_urban_unemployment),  # through 2026-07
    ("external", "usdcny", fetch_usdcny_daily),                          # daily, via Frankfurter
    ("growth", "retail_sales", fetch_retail_sales),                      # through 2026-07
    ("growth", "fixed_asset_investment", fetch_fixed_asset_investment),  # through 2026-07
    # lpr and money supply (m1/m2/gap) handled separately below — each
    # returns more than one series from a single call.
]

# Wired to a real akshare function, but verified STALE on 2026-08-29 — all of
# these scrape the same Sina Finance macro widget, which was frozen around
# Sep 2025 (~1 year behind) at last check. Kept wired because akshare/Sina may
# fix the upstream feed at any time — a fixed upstream will start flowing
# through automatically — but do not represent these as live without
# re-checking. See README "Known limitations".
STALE_FETCHERS = [
    ("growth", "official_manufacturing_pmi", fetch_pmi),
    ("growth", "caixin_manufacturing_pmi", fetch_cx_pmi),
    ("growth", "official_non_manufacturing_pmi", fetch_non_man_pmi),
    ("growth", "industrial_production", fetch_industrial_production),
    ("growth", "gdp_growth", fetch_gdp),
    ("prices_credit", "cpi", fetch_cpi),
    ("prices_credit", "ppi", fetch_ppi),
    ("external", "exports_yoy", fetch_exports),
    ("external", "imports_yoy", fetch_imports),
    ("external", "trade_balance", fetch_trade_balance),
    ("external", "fx_reserves", fetch_fx_reserves),
]

FETCHERS = FRESH_FETCHERS + STALE_FETCHERS

# (rrr used to be here — earlier marked BROKEN, but that was a
# misdiagnosis: find_col() was searching for column names that don't exist
# in this table, so it raised on every run and the series never updated.
# ak.macro_china_reserve_requirement_ratio is actually current — fixed and
# called separately in main(), see fetch_rrr()'s docstring.)
# (usdcny used to be here too — ak.macro_china_rmb is dead since 2021-05-13 —
# but now runs on Frankfurter's daily feed instead, see FRESH_FETCHERS above.)
# - property_investment: ak.macro_china_real_estate exists but is a real-
#   estate *climate index* (景气指数), a different metric from YTD
#   investment growth — not a valid substitute. No matching source found.
# - new_home_prices: ak.macro_china_new_house_price exists but is raw
#   per-city index levels, not NBS's official 70-city composite — building
#   a defensible aggregate from it is out of scope for now.
# - youth_unemployment: ak.macro_china_urban_unemployment's item breakdown
#   was checked directly — it only has 25-59/local-registration/migrant
#   splits, no 16-24 youth figure. No akshare source exists for this.
# All three stay manually-seeded.


# ---------- News (RSS, no key needed) ----------

# Verified live on 2026-08-29: Reuters' China RSS returns 401 (needs auth,
# discontinued for public use) and Caixin's returns 404 — both dropped.
# SCMP's feed works directly; Google News searches cover the "political"
# half of the ask and catch outlets (Bloomberg, Reuters, WaPo, etc.) that
# don't publish their own public RSS.
NEWS_FEEDS = [
    ("SCMP China", "https://www.scmp.com/rss/318198/feed"),
    ("Google News", "https://news.google.com/rss/search?q=China+economy+when:30d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "https://news.google.com/rss/search?q=China+politics+OR+China+trade+when:30d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "https://news.google.com/rss/search?q=China+PBOC+OR+China+yuan+OR+China+stocks+when:30d&hl=en-US&gl=US&ceid=US:en"),
]

TAG_RE = re.compile(r"<[^>]+>")

def clean_html(s):
    return TAG_RE.sub("", s or "").strip()

ECON_POLITICAL_KEYWORDS = [
    "econom", "trade", "tariff", "gdp", "inflation", "cpi", "ppi", "pmi",
    "yuan", "renminbi", "rmb", "pboc", "central bank", "rate cut", "stimulus",
    "export", "import", "deflation", "debt", "property", "real estate",
    "unemployment", "politburo", "beijing", "xi jinping", "ccp", "party congress",
    "sanction", "chip", "semiconductor", "tech war", "investment", "stock",
    "market", "bond", "fiscal", "policy", "regulat", "diplomat", "geopolit",
]

def is_econ_political(title, summary):
    text = (title + " " + summary).lower()
    return any(kw in text for kw in ECON_POLITICAL_KEYWORDS)

# Collapses variant labels for the same outlet (our own feed label vs. the
# real publisher name Google News resolves, ".com"-suffixed domain names,
# etc.) to one canonical, recognizable name.
SOURCE_ALIASES = {
    "scmp china": "South China Morning Post",
    "south china morning post": "South China Morning Post",
    "scmp": "South China Morning Post",
    "bloomberg.com": "Bloomberg",
    "bloomberg": "Bloomberg",
    "the wall street journal": "WSJ",
    "wsj": "WSJ",
    "reuters.com": "Reuters",
    "reuters": "Reuters",
    "ap news": "AP",
    "associated press": "AP",
    "the washington post": "The Washington Post",
    "washingtonpost.com": "The Washington Post",
    "cnbc": "CNBC",
    "cnbc.com": "CNBC",
}

def canonical_source(name):
    if not name:
        return name
    return SOURCE_ALIASES.get(name.strip().lower(), name.strip())

def fetch_news(limit_per_feed=40):
    if feedparser is None:
        log("feedparser not installed, skipping news refresh")
        return []
    items = []
    for label, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            if getattr(feed, "bozo", False) and not feed.entries:
                log(f"news feed unreadable for {label} ({url}): {feed.bozo_exception}")
                continue
            for entry in feed.entries[:limit_per_feed]:
                title = entry.get("title", "").strip()
                source_obj = entry.get("source")
                source_name = None
                if isinstance(source_obj, dict):
                    source_name = source_obj.get("title")
                if source_name:
                    # Google News titles carry a redundant " - <Publisher>" suffix.
                    suffix = " - " + source_name
                    if title.endswith(suffix):
                        title = title[: -len(suffix)]
                else:
                    source_name = label
                source_name = canonical_source(source_name)

                if label == "Google News":
                    # Google's <summary> is a repeat of the link, not real text.
                    summary = ""
                else:
                    summary = clean_html(entry.get("summary", ""))[:220]

                if not is_econ_political(title, summary):
                    continue

                parsed = entry.get("published_parsed")
                date_iso = (
                    datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
                    if parsed else entry.get("published", "")
                )

                items.append({
                    "headline": title,
                    "summary": summary,
                    "source": source_name,
                    "date": date_iso,
                    "url": entry.get("link", ""),
                })
        except Exception as e:
            log(f"news fetch failed for {label} ({url}): {e}")

    # de-dupe near-identical headlines across feeds (e.g. both Google News
    # queries surfacing the same article), keep first occurrence
    seen, deduped = set(), []
    for item in sorted(items, key=lambda i: i["date"], reverse=True):
        key = item["headline"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def main():
    data = load_data()

    try:
        cal = fetch_trading_calendar()
        if cal:
            data["trading_days"] = cal
            log(f"OK trading_days: {len(cal)} days")
    except Exception:
        log(f"FAIL trading_days:\n{traceback.format_exc()}")

    for category, key, fetcher in FETCHERS:
        try:
            update_series(data, category, key, fetcher())
        except Exception:
            log(f"FAIL {category}.{key}:\n{traceback.format_exc()}")

    try:
        lpr1y, lpr5y = fetch_lpr()
        update_series(data, "prices_credit", "lpr_1y", lpr1y)
        update_series(data, "prices_credit", "lpr_5y", lpr5y)
    except Exception:
        log(f"FAIL prices_credit.lpr_*:\n{traceback.format_exc()}")

    try:
        m1_pts, m2_pts, gap_pts = fetch_money_supply()
        update_series(data, "prices_credit", "m1_growth", m1_pts)
        update_series(data, "prices_credit", "m2_growth", m2_pts)
        update_series(data, "prices_credit", "m1_m2_gap", gap_pts)
    except Exception:
        log(f"FAIL prices_credit.m1_m2_gap:\n{traceback.format_exc()}")

    try:
        update_series(data, "prices_credit", "rrr", fetch_rrr())
    except Exception:
        log(f"FAIL prices_credit.rrr:\n{traceback.format_exc()}")

    try:
        news = fetch_news()
        if news:
            data["news"] = news
    except Exception:
        log(f"FAIL news:\n{traceback.format_exc()}")

    data["meta"]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_data(data)
    log("done")


if __name__ == "__main__":
    main()
