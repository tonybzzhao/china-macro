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

**GitHub Actions scheduling gotcha (found 2026-08-30):** the cron was
originally `"0 * * * *"` (top of every hour). Only 1 run fired in the first
~8 hours it was live — GitHub's own docs warn that `:00` is the most
congested slot on their scheduler and jobs there "may be delayed or, in
some cases, dropped." Changed to `"23 * * * *"`. If updates seem to have
stalled again, check the repo's **Actions** tab for run history before
assuming the script itself is broken — scheduled-workflow reliability is
a GitHub-side thing, not something this codebase controls.

**Data-freshness check (last updated 2026-08-31):** every fetcher wired into
`scripts/update_data.py` was run live before shipping, not just guessed from
docs. Results:

| Status | Series | As of last check |
|---|---|---|
| ✅ Live | `tsf_flow`, `m1_growth`, `m2_growth`, `m1_m2_gap` (via `ak.macro_china_supply_of_money`) | current through 2026-07 |
| ✅ Live | `urban_unemployment` | current through 2026-07 |
| ✅ Live | `lpr_1y`, `lpr_5y` | current through 2026-08 |
| ✅ Live | `usdcny` (daily, via Frankfurter/ECB — not akshare) | current through 2026-08-28, back to 2000-01-13 |
| ✅ Live | `retail_sales` (`ak.macro_china_consumer_goods_retail`), `fixed_asset_investment` (`ak.macro_china_gdzctz`, computed as YTD y/y — see flag below) | current through 2026-07 |
| ✅ Live | `trading_days` (`ak.tool_trade_date_hist_sina`) — powers the Beijing-time market-open indicator | through 2026-12-31 |
| ✅ Live | `rrr` — tracks **大型银行 (large banks)** specifically. Two bugs found and fixed in sequence: (1) `ak.macro_china_reserve_requirement_ratio`'s own column names didn't match what the fetcher searched for, so it silently raised on every run and never updated; (2) once fixed, the akshare column it was reading (`大型金融机构-调整后`, "large financial institutions") turned out to be a *different category* than large banks, running ~1.5pp higher (9.0% vs the true 7.5%) — caught by cross-checking against [PBOC's own live current-value page](https://www.pbc.gov.cn/rmyh/4027845/index.html) and CEIC, both independently showing 7.5%. Now scrapes PBOC's page directly for the current value and reconstructs history by shifting akshare's verified cut timing/magnitudes to match. | current value verified exact (7.5%, Aug 2026); history before ~2015 is an offset-based approximation, flagged in the UI |
| ✅ Live | `cpi` (`ak.macro_china_cpi`), `ppi` (`ak.macro_china_ppi`), `official_manufacturing_pmi` + `official_non_manufacturing_pmi` (`ak.macro_china_pmi`, one call for both), `caixin_manufacturing_pmi` (`ak.index_pmi_man_cx`), `gdp_growth` (`ak.macro_china_gdp`, its own `季度` column labels the reference quarter directly — simpler than the old release-date-guessing approach), `industrial_production` (`ak.macro_china_gyzjz`), `exports_yoy` + `imports_yoy` + `trade_balance` (`ak.macro_china_hgjck`, one call for all three — trade balance computed from the raw export/import amount columns, verified unit is thousand-USD), `fx_reserves` (`ak.macro_china_fx_gold`) | **fixed 2026-08-31** — user asked directly why PMI hadn't picked up the Aug 31 release; turned out every one of these was pointed at a different akshare function that scrapes the same dead Sina widget (frozen ~Sep 2025) as the old `rrr` bug, while a fresh alternative function existed the whole time. All verified live: official manufacturing PMI showed August 2026 (49.8) immediately after switching. Current through 2026-07/08 |
| — Not wired, no source exists | `property_investment` (akshare's real-estate function is a climate index, not investment growth), `new_home_prices` (akshare only has raw per-city levels, not NBS's 70-city composite), `youth_unemployment` (checked `ak.macro_china_urban_unemployment`'s full item breakdown directly — no 16-24 split exists anywhere in akshare) | — |

**Practical effect:** as of 2026-08-31, every series with a known akshare
path is genuinely auto-refreshing hourly — the "stale group" that existed
earlier was a wrong-function bug, not a dead upstream source, and has been
fixed. Only the three not-wired series (no source exists at all) stay on
their manually-curated seed values. The dashboard UI still surfaces any
remaining data-quality caveats (e.g. RRR's pre-2015 approximation) via a ⚠
flag, visible on hover, right on its chart
card.

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
