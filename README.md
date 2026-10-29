# China Macro Pulse

An interactive, auto-updating dashboard of Chinese macroeconomic indicators —
growth & activity, prices & credit, external sector, and property & labor —
plus a running feed of related political/economic news.

## How it works

### Caixin / RatingDog PMI recovery (2026-10-21)

The October 21 runs at [11:57 UTC](https://github.com/tonybzzhao/china-macro/actions/runs/39595811169)
and [17:16 UTC](https://github.com/tonybzzhao/china-macro/actions/runs/39642398895)
failed decoding Caixin's response. A direct probe returned HTTP 200 with an HTML
404 page from `yun.ccxe.com.cn/api/index/pro/cxIndexTrendInfo?type=man`.
Inspection confirmed `history.json` stopped at August 2026 (51.5). September
2026 is 52.1, published September 30, and is now included without altering history.

| Source | Assessment |
| --- | --- |
| Caixin endpoint used by AKShare `index_pmi_man_cx` | Currently non-JSON. The adapter calls the same endpoint directly with timeouts and schema validation. |
| [S&P Global](https://www.pmi.spglobal.com/) / RatingDog | Survey producer; public release pages returned 403 during investigation. No unverified release scraper is enabled. |
| [Trading Economics](https://tradingeconomics.com/china/manufacturing-pmi) | Live-accessible September 52.1 headline, sourced to S&P Global. Selected fallback parses only the current observed manufacturing headline and reference month. |
| [FXStreet](https://www.fxstreet.com/news/chinas-september-ratingdog-manufacturing-pmi-leaps-to-521-services-pmi-rises-to-516-202609300148) and [AP](https://apnews.com/article/b53c7e697fc842de4c9a57ac5362cd05) | Publication cross-checks agree on 52.1 and prior 51.5; not runtime dependencies. |
| Legacy Sina widgets | Previously documented as frozen in 2025; unsuitable fallback. NBS PMI and services PMI are different indicators. |

`scripts/caixin_pmi.py` makes at most three attempts per source with 1/2-second
backoff and 5/20-second connect/read timeouts. Empty, malformed, conflicting,
out-of-range, future-month, and stale responses fail closed. A stale primary
response also triggers fallback. Conflicts with retained observations require
manual verification; existing values are never silently revised. Only fully
accepted responses are merged, preserving every prior observation.

The series' `refresh` object records `attempted_at`, `status` (`refreshed` or
`retained`), `expected_month`, `latest_observation`, source, errors, and separate
last-success time/source. The global run timestamp is not PMI refresh evidence.
Freshness uses Beijing time: by day 7 it requires the previous reference month;
before day 7 it allows the month before that. This conservative deadline allows
holiday delays but cannot detect an early month-end release until day 7.
A failed retrieval remains `retained` even if history meets the deadline.

After saving other indicators and refresh metadata, unsuccessful PMI retrieval
returns exit code 1. Actions commits available data, then marks the job failed.
Successful fallback counts as refreshed, with primary errors retained. This
change does not modify economic-calendar retrieval or its status handling.

Run `python -m unittest discover -s tests -v`. Regression tests use mock responses;
a dedicated test workflow runs on pushes and pull requests. The fallback fails
closed if its provider changes the HTML or headline wording.

The older source inventory below describes the August baseline; this section
supersedes its Caixin retrieval and freshness claims.

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
| ✅ Live | `property_investment`, `youth_unemployment` (NBS's own portal via `ak.macro_china_nbs_nation`), `new_home_prices` (`ak.macro_china_nbs_region`, simple average of 70 cities' y/y — NBS publishes no national composite), `gdp_growth` Q2–Q4 (derived from cumulative YTD rows; only Q1 is standalone in akshare), `tsf_flow` Aug+ (backed out of Baidu calendar YTD releases because the MOFCOM mirror lags months) | current through 2026-09 / 2026-Q3 |

**Practical effect:** every series on the dashboard now has an automated source. These were previously labelled "no source exists" — that was wrong; NBS's own data portal (wrapped by `ak.macro_china_nbs_nation` / `_region`) carries real-estate investment, 16–24 unemployment and per-city home prices. Derived or approximated series carry a ⚠ flag on their chart card.

**Contributing a fix:** if you find a working source for any of the broken/
not-wired series, add a `fetch_*()` function following the pattern in
`scripts/update_data.py` and add it to the `FRESH_FETCHERS` list once you've
verified it's actually current.

**Data-quality flags:** A few series carry a visible ⚠ flag in the dashboard
UI itself (hover for the full note) — e.g. fixed asset investment's Feb/Mar
2026 reading looks anomalous, RRR's precise current level is uncertain, and
the 70-city home-price series is y/y (not the m/m originally requested)
because that's what was reliably sourceable.

## Daily brief (chatccp.com/brief)

A Simplified-Chinese morning market briefing for readers in China who can't
easily follow English-language news. It retells the day's English headlines as
plain news copy, covering US macro and markets plus whichever sectors are hot
that day (picked from sector-ETF moves, stock movers and headline flow, not a
fixed list). It is regenerated every China trading morning
(`.github/workflows/brief.yml`, 06:17 Beijing Mon-Fri with two later backup
runs; holidays are skipped using the SSE calendar in `data/history.json`).

Page layout: core theme and TL;DR; market tiles with up/down arrows and colours
(click one for its recent chart); "macro and markets" plus 2 to 4 hot-sector
sections (each: today's news, then a short analysis); long-term trends and
risks; a schedule table in Beijing time; and every source listed once at the
end. Charts: index moves, S&P sector-ETF moves, biggest US movers, and movers
inside the hottest sectors.

- `scripts/build_brief.py` gathers facts (Yahoo chart API for indices, yields,
  oil, gold and the 11 sector ETFs; Yahoo's stock screener for movers; Google
  News RSS in English for headlines across eleven topics) and has an LLM write
  the text **from those facts only**, in five small calls (plan the sections,
  one call per section, then theme/TL;DR/trends/schedule). The model cites
  headlines by id; the script resolves the links, and tiles and charts are
  computed straight from data, so the model never writes a URL or a market
  number.
- **Costs nothing.** Only free providers are wired in, and the paid Anthropic
  API is deliberately not used. Providers are tried best first:
  1. **Google Gemini free tier**: set repo secret `GEMINI_API_KEY`. **A Google
     AI Pro subscription does not make the API free**: a key is only free if its
     Cloud project has no billing account attached (the plan column at
     aistudio.google.com/apikey must say Free tier). The code cannot see this, so
     the key is ignored unless the repo variable `GEMINI_FREE_TIER_OK` is `yes`.
     Model defaults to `gemini-2.5-flash` (`BRIEF_GEMINI_MODEL`), with
     `gemini-2.5-flash-lite` as a fallback when the first model's quota is gone.
     Free quotas are per model, per minute and per day, and recent reports put
     them low (as few as ~20 requests a day), so calls are spaced 13 s apart, a
     run makes 5 calls (hard cap 14), and a 429 is understood: a per-minute limit
     waits the advised delay, a per-day limit stops at once. Over quota means
     Google refuses the call; on the free tier it is never billed. **Do not
     trigger many manual runs in one day**: they use the same daily quota (it
     resets at midnight Pacific time).
  2. **Groq free tier** (optional second provider): set repo secret
     `GROQ_API_KEY`. Defaults to `llama-3.3-70b-versatile` (`BRIEF_GROQ_MODEL`).
  3. **No keyless fallback by default.** The Pollinations endpoint was tried: it
     timed out, returned malformed JSON, mistranslated company names and made up
     a share-price move, so it is off unless `BRIEF_ALLOW_KEYLESS=1`.
  - GitHub Models was tried first and dropped: `models.github.ai` answers
    `200 text/plain OK` from Actions runners, and the older Azure host no longer
    resolves.
  - Generation is split into five small calls (plan the sections, one call per
    section, then theme/TL;DR/trends/schedule) so each fits free-tier request
    size and rate caps.
  - If every provider fails the market tiles and charts still refresh, the text
    is kept from the last good run, the page shows a "text not updated" banner,
    and `brief/status.json` records which providers were tried and the error.
  - Free-tier terms and limits change; check `brief/status.json` after a run.
- Style rules live in `SYSTEM` in that script: news-agency register, no
  commentary outside the short "analysis" group, full-width Chinese
  punctuation (also enforced in code by `cn_punct`), schedule times only if a
  source gives them.
- Known limits: news is retold from headlines only; the news feed sometimes
  mis-dates older stories (the prompt tells the model to be sceptical of
  single-source "breaking" items); Yahoo can rate-limit cloud IPs, in which case
  the previous tiles are kept.
- Manual run: Actions, "Update daily brief", Run workflow (tick "force" to
  regenerate). Locally: `python scripts/build_brief.py --facts-only` to inspect
  the inputs.

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
