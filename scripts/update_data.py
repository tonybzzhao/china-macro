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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import akshare as ak

try:
    import feedparser
except ImportError:
    feedparser = None

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "history.json"
MAX_POINTS = 36  # cap per series so the file doesn't grow unbounded


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


def merge_points(existing, new_points):
    """De-dupe by date (new wins), sort, cap to MAX_POINTS."""
    by_date = {p["date"]: p for p in existing}
    for p in new_points:
        if p.get("value") is None:
            continue
        by_date[p["date"]] = p
    merged = sorted(by_date.values(), key=lambda p: p["date"])
    return merged[-MAX_POINTS:]


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
    series["data"] = merge_points(series["data"], new_points)
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

def fetch_m2():
    return simple_series(ak.macro_china_m2_yearly, DATE_COLS, VAL_COLS)

def fetch_urban_unemployment():
    return simple_series(
        ak.macro_china_urban_unemployment,
        DATE_COLS,
        VAL_COLS + ["全国城镇调查失业率"],
    )

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
    df = ak.macro_china_reserve_requirement_ratio()
    date_col = find_col(df, ["日期", "date", "TRADE_DATE"])
    val_col = find_col(df, ["大型金融机构-调整后", "大型金融机构调整后", "value", "今值"])
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    out = []
    for _, r in df.iterrows():
        try:
            out.append({"date": normalize_date(r[date_col]), "value": float(r[val_col])})
        except (TypeError, ValueError):
            continue
    return out

def fetch_usdcny():
    df = ak.macro_china_rmb()
    date_col = find_col(df, ["日期", "date"])
    val_col = find_col(df, ["美元汇率中间价", "value"])
    if date_col is None or val_col is None:
        raise ValueError(f"couldn't find date/value columns in {list(df.columns)}")
    return [{"date": normalize_date(r[date_col]), "value": float(r[val_col])}
            for _, r in df.iterrows() if pd.notna(r[val_col])]


# Verified live against a real run on 2026-08-29 — data current as of that date:
FRESH_FETCHERS = [
    ("prices_credit", "tsf_flow", fetch_tsf_flow),                       # through 2026-04
    ("property_labor", "urban_unemployment", fetch_urban_unemployment),  # through 2026-07
    # lpr handled separately below (returns two series)
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
    ("prices_credit", "m2_growth", fetch_m2),
    ("external", "exports_yoy", fetch_exports),
    ("external", "imports_yoy", fetch_imports),
    ("external", "trade_balance", fetch_trade_balance),
    ("external", "fx_reserves", fetch_fx_reserves),
]

FETCHERS = FRESH_FETCHERS + STALE_FETCHERS

# NOT wired at all, by design:
# - usdcny (ak.macro_china_rmb): verified BROKEN, data stops 2021-05-13.
# - rrr (ak.macro_china_reserve_requirement_ratio): verified BROKEN, data
#   stops in 2007. Both need a replacement source before automating —
#   PBOC's own site or a different akshare function. Left manually-seeded.
# - retail_sales, fixed_asset_investment, property_investment,
#   new_home_prices, youth_unemployment: no confirmed akshare function was
#   found for these at all. Also manually-seeded.


# ---------- News (RSS, no key needed) ----------

# Verified live on 2026-08-29: Reuters' China RSS returns 401 (needs auth,
# discontinued for public use) and Caixin's returns 404 — both dropped.
# SCMP's feed works directly; Google News searches cover the "political"
# half of the ask and catch outlets (Bloomberg, Reuters, WaPo, etc.) that
# don't publish their own public RSS.
NEWS_FEEDS = [
    ("SCMP China", "https://www.scmp.com/rss/318198/feed"),
    ("Google News", "https://news.google.com/rss/search?q=China+economy+when:7d&hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "https://news.google.com/rss/search?q=China+politics+OR+China+trade+when:7d&hl=en-US&gl=US&ceid=US:en"),
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

def fetch_news(limit_per_feed=6):
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

                if label == "Google News":
                    # Google's <summary> is a repeat of the link, not real text.
                    summary = ""
                else:
                    summary = clean_html(entry.get("summary", ""))[:220]

                if not is_econ_political(title, summary):
                    continue

                items.append({
                    "headline": title,
                    "summary": summary,
                    "source": source_name,
                    "date": entry.get("published", "")[:16],
                    "url": entry.get("link", ""),
                })
        except Exception as e:
            log(f"news fetch failed for {label} ({url}): {e}")

    # de-dupe near-identical headlines across feeds (e.g. both Google News
    # queries surfacing the same article), keep first occurrence
    seen, deduped = set(), []
    for item in items:
        key = item["headline"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def main():
    data = load_data()

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

    # rrr and usdcny intentionally not called here — see the "NOT wired"
    # note above fetch_rrr/fetch_usdcny definitions.

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
