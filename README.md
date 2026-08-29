# China Macro Pulse

An interactive, auto-updating dashboard of Chinese macroeconomic indicators —
growth & activity, prices & credit, external sector, and property & labor —
plus a running feed of related political/economic news.

## How it works

- **`index.html` / `app.js` / `style.css`** — a static, no-build front end.
  It fetches `data/history.json` and renders interactive charts (hover for a
  crosshair + tooltip, switch time ranges, switch category tabs).
- **`data/history.json`** — the single source of truth. Each series carries
  its own label/unit/formatting so the front end needs no per-indicator code.
- **`scripts/update_data.py`** — refreshes `data/history.json` from live
  sources (mainly [`akshare`](https://github.com/akfamily/akshare), a
  community-maintained library that wraps NBS/PBOC/Sina data) and RSS news
  feeds.
- **`.github/workflows/update.yml`** — runs the script hourly via GitHub
  Actions and commits the updated JSON if anything changed. Because the page
  just reads a static JSON file, GitHub Pages serves whatever the bot last
  committed — that's what makes it "live" without any server to run. Most
  series are monthly/quarterly releases and won't actually change most
  hours; the point of hourly (over daily) is that a new release, or fresh
  news/FX, shows up within an hour instead of within a day.

## ⚠️ Known limitations — read before relying on this

**Data-freshness check (2026-08-29):** every akshare fetcher wired into
`scripts/update_data.py` was run live before shipping, not just guessed from
docs. Results:

| Status | Series | As of last check |
|---|---|---|
| ✅ Live | `tsf_flow` (Total Social Financing) | current through 2026-04 |
| ✅ Live | `urban_unemployment` | current through 2026-07 |
| ✅ Live | `lpr_1y`, `lpr_5y` | current through 2026-08 |
| ⚠️ Stale (~1yr behind) | `cpi`, `ppi`, `official_manufacturing_pmi`, `caixin_manufacturing_pmi`, `official_non_manufacturing_pmi`, `gdp_growth`, `industrial_production`, `exports_yoy`, `imports_yoy`, `trade_balance`, `fx_reserves`, `m2_growth` | frozen around Sep 2025 |
| ❌ Broken, not called | `usdcny` (`ak.macro_china_rmb`) | data stops 2021-05-13 |
| ❌ Broken, not called | `rrr` (`ak.macro_china_reserve_requirement_ratio`) | data stops in **2007** |
| — Not wired at all | `retail_sales`, `fixed_asset_investment`, `property_investment`, `new_home_prices`, `youth_unemployment` | no confirmed akshare function found |

The "stale" group all scrape the same Sina Finance macro widget, which
stopped updating upstream around September 2025 — an akshare/Sina issue,
not something this script can fix directly. They're kept wired (rather than
removed) because a future akshare release may fix the upstream feed, at
which point fresh data starts flowing through automatically with no code
change needed here. If a series looks frozen, `pip install -U akshare` and
re-run before assuming the workflow itself is broken.

**Practical effect:** right now, only TSF, urban unemployment, and LPR are
genuinely auto-refreshing day to day. Everything else on the dashboard is a
manually-curated snapshot (or, for the stale akshare group, effectively
frozen at whatever last flowed through) until either akshare's upstream
sources catch up or better sources are wired in. The dashboard UI surfaces
this itself — every series with a data-quality caveat shows a ⚠ flag,
visible on hover, right on its chart card.

**Contributing a fix:** if you find a working source for any of the broken/
not-wired series, add a `fetch_*()` function following the pattern in
`scripts/update_data.py` and add it to the `FRESH_FETCHERS` list once you've
verified it's actually current.

**Data-quality flags:** A few series carry a visible ⚠ flag in the dashboard
UI itself (hover for the full note) — e.g. fixed asset investment's Feb/Mar
2026 reading looks anomalous, RRR's precise current level is uncertain, and
the 70-city home-price series is y/y (not the m/m originally requested)
because that's what was reliably sourceable.

## Setup

```bash
# 1. Push this repo to GitHub (public or private — GitHub Pages needs public
#    unless you're on GitHub Enterprise / have Pages for private repos).
git init
git add .
git commit -m "Initial China Macro Pulse dashboard"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main

# 2. Enable GitHub Pages: repo Settings → Pages → Source: "Deploy from a
#    branch" → Branch: main, folder: / (root).

# 3. Let the workflow push commits: repo Settings → Actions → General →
#    Workflow permissions → "Read and write permissions". Without this the
#    scheduled job can update data/history.json but can't push the commit.

# 4. (Optional) trigger the first live refresh manually instead of waiting
#    for the next hourly run: Actions tab → "Update China macro data" →
#    Run workflow.
```

Once Pages is live, the site is at `https://<you>.github.io/<repo>/` and
will reflect whatever `data/history.json` the bot last committed — genuinely
up to date whenever you load it, bounded by the hourly refresh cadence.

## Local development

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

(Opening `index.html` directly via `file://` won't work — browsers block
`fetch()` against local files.)

To test the update script locally:

```bash
pip install -r requirements.txt
python scripts/update_data.py
```
