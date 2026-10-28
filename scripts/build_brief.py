#!/usr/bin/env python3
"""Build brief/brief.json, the data behind chatccp.com/brief.

A Simplified-Chinese morning market briefing for readers in China who can't
easily follow English-language news. It covers US macro and markets plus
whichever sectors are hot that day (chosen from sector ETF moves, stock movers
and the headline flow), retold as plain news copy.

Runs on China trading mornings from .github/workflows/brief.yml. Three parts:

  1. Facts, gathered deterministically: index, sector-ETF and stock moves
     (Yahoo chart and screener APIs) and topical headlines (Google News RSS,
     English edition).
  2. Narrative (theme, TL;DR, sections, trends, calendar) written in Chinese by
     an LLM from those facts only, in several small calls. Free providers only:
     a Google AI Studio free-tier key (GEMINI_API_KEY) or GitHub Models via the
     Actions token. The model never writes URLs or market numbers: it cites
     headlines by id and the script resolves the links, while tiles and charts
     come straight from the facts.
  3. Assembly into brief/brief.json.

Every part degrades on its own. If the market fetch fails the previous tiles
are kept; if the LLM call fails (or no key is set) the previous narrative is
kept and the page says the text is stale. The file is never left half-written.

Usage:
  python scripts/build_brief.py                      # normal run
  python scripts/build_brief.py --facts-only         # print the facts, no LLM
  python scripts/build_brief.py --narrative-file f   # use f instead of the LLM
  python scripts/build_brief.py --force              # ignore trading-day/fresh gates
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"
OUT = ROOT / "brief" / "brief.json"
UA = {"User-Agent": "Mozilla/5.0"}
BJT = timezone(timedelta(hours=8))

MARKETS = [  # key, yahoo symbol, label, kind
    ("spx", "^GSPC", "标普 500", "idx"),
    ("ndx", "^IXIC", "纳斯达克", "idx"),
    ("dji", "^DJI", "道琼斯", "idx"),
    ("sox", "^SOX", "费城半导体指数", "idx"),
    ("us10y", "^TNX", "美国 10 年期国债收益率", "pct"),
    ("brent", "BZ=F", "布伦特原油", "usd"),
    ("gold", "GC=F", "黄金", "usd"),
]
TILE_KEYS = ("spx", "ndx", "sox", "us10y", "brent", "gold")

SECTORS = [  # sector ETF, 中文名, Yahoo screener sector name
    ("XLK", "科技", "Technology"), ("XLC", "通信服务", "Communication Services"),
    ("XLY", "可选消费", "Consumer Cyclical"), ("XLP", "必需消费", "Consumer Defensive"),
    ("XLV", "医疗保健", "Healthcare"), ("XLF", "金融", "Financial Services"),
    ("XLI", "工业", "Industrials"), ("XLE", "能源", "Energy"),
    ("XLU", "公用事业", "Utilities"), ("XLB", "材料", "Basic Materials"),
    ("XLRE", "房地产", "Real Estate"),
]
SECTOR_NAMES = {s[2] for s in SECTORS}
SECTOR_ZH = {s[2]: s[1] for s in SECTORS}

NAME_SUFFIX = re.compile(r",?\s+(Inc\.?|Corporation|Corp\.?|Ltd\.?|Limited|plc|A/S|S\.A\.|N\.V\.|Holdings?|Group|Company|Co\.)\b.*$", re.I)
ZH_NAMES = {"SKHY": "SK 海力士", "ORCL": "甲骨文", "GMAB": "Genmab", "GSK": "葛兰素史克", "BSX": "波士顿科学",
            "BIIB": "渤健", "LOGI": "罗技", "FTNT": "飞塔", "FFIV": "F5", "GRMN": "佳明", "MSFT": "微软",
            "NVDA": "英伟达", "AAPL": "苹果", "AMZN": "亚马逊", "GOOGL": "谷歌 Alphabet", "GOOG": "谷歌 Alphabet",
            "META": "Meta", "TSLA": "特斯拉", "AMD": "超微 AMD", "MU": "美光", "INTC": "英特尔",
            "QCOM": "高通", "AVGO": "博通", "TSM": "台积电", "LLY": "礼来", "NVO": "诺和诺德",
            "PFE": "辉瑞", "MRK": "默沙东", "JNJ": "强生", "XOM": "埃克森美孚", "CVX": "雪佛龙",
            "JPM": "摩根大通", "BAC": "美国银行", "GS": "高盛", "BA": "波音", "NFLX": "奈飞"}

TOPICS = {  # Google News queries; English because that is where the news breaks
    "fed_macro": "Federal Reserve OR inflation OR Treasury yields OR jobs report OR tariffs",
    "econ_data": "GDP OR PCE OR CPI OR consumer confidence OR retail sales OR payrolls report",
    "markets": "Wall Street stocks close S&P 500 Nasdaq",
    "earnings": "earnings after the bell OR beats estimates OR raises guidance OR shares fall guidance",
    "ai_tech": "AI chips OR OpenAI OR Nvidia OR Microsoft OR Alphabet OR Apple OR Meta OR Amazon",
    "ai_models": "Anthropic OR OpenAI OR Google Gemini OR AI regulation OR data center",
    "energy": "oil prices OR OPEC OR natural gas OR Saudi OR Houthi OR Iran",
    "finance": "banks earnings OR private credit OR IPO OR merger OR acquisition OR Wall Street deal",
    "health_biotech": "FDA approval OR biotech OR pharma OR clinical trial OR GLP-1",
    "consumer": "retail earnings OR consumer spending OR auto OR airline OR housing",
    "us_china_tech": "US China tariffs OR chips export controls OR Xi Jinping OR Taiwan",
}

CHINESE_OUTLETS = ("新华", "人民", "中新", "财新", "澎湃", "界面", "第一财经")


def is_china_trading_day(d):
    """True if Beijing date d (YYYY-MM-DD) is an SSE/SZSE session, per data/history.json."""
    days = json.loads(HISTORY.read_text()).get("trading_days") or []
    return d in days if days else datetime.strptime(d, "%Y-%m-%d").weekday() < 5


def log(msg):
    print(f"[build_brief] {msg}", file=sys.stderr, flush=True)


def pct(a, b):
    return (a / b - 1) * 100


# ----------------------------------------------------------------- facts

def fetch_market(symbol, tries=3):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, params={"range": "1mo", "interval": "1d"}, headers=UA, timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            meta = res["meta"]
            off = meta.get("gmtoffset", 0)
            bars = []
            for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c is None:
                    continue
                d = datetime.fromtimestamp(ts + off, timezone.utc).strftime("%Y-%m-%d")
                bars.append({"date": d, "close": round(c, 4)})
            # A session that just closed can still have a null daily bar; the
            # quote in meta already holds its close.
            px, pt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
            if px and pt:
                qd = datetime.fromtimestamp(pt + off, timezone.utc).strftime("%Y-%m-%d")
                if not bars or qd > bars[-1]["date"]:
                    bars.append({"date": qd, "close": round(px, 4)})
            # Yahoo includes today's in-progress session as the last bar. Once a
            # market closes, currentTradingPeriod rolls to the NEXT session, so
            # "partial" means a session is open right now, not merely "not yet
            # at the period end".
            period = meta.get("currentTradingPeriod", {}).get("regular", {})
            now_ts = datetime.now(timezone.utc).timestamp()
            partial = bool(period) and period.get("start", 0) <= now_ts < period.get("end", 0)
            return bars, partial
        except Exception as e:  # network, 429, schema drift
            last = e
    raise RuntimeError(f"{symbol}: {last}")


def summarize(label, kind, bars, partial):
    done = bars[:-1] if partial else bars
    if len(done) < 2:
        raise ValueError("too few bars")
    return {"label": label, "kind": kind, "bars": done[-22:], "last": done[-1], "prev": done[-2],
            "intraday": bars[-1] if partial else None}


def gather_markets():
    out = {}
    for key, sym, label, kind in MARKETS:
        try:
            out[key] = summarize(label, kind, *fetch_market(sym))
        except Exception as e:
            log(f"market {key} failed: {e}")
    return out


def gather_sectors():
    """Daily change of the 11 S&P 500 sector ETFs: the objective read on which sectors are hot."""
    rows = []
    for etf, zh, ysector in SECTORS:
        try:
            s = summarize(zh, "usd", *fetch_market(etf))
            cur = s["intraday"] or s["last"]
            base = s["last"] if s["intraday"] else s["prev"]
            rows.append({"etf": etf, "name": zh, "yahoo_sector": ysector, "date": cur["date"],
                         "change_pct": round(pct(cur["close"], base["close"]), 2)})
        except Exception as e:
            log(f"sector {etf} failed: {e}")
    return sorted(rows, key=lambda r: r["change_pct"], reverse=True)


def clean_name(symbol, raw):
    if symbol in ZH_NAMES:
        return ZH_NAMES[symbol]
    return NAME_SUFFIX.sub("", raw or symbol).strip()[:22]


def screen(sess, crumb, extra, n, order, min_cap):
    body = {"size": n, "offset": 0, "sortField": "percentchange", "sortType": order,
            "quoteType": "EQUITY", "topOperator": "AND",
            "query": {"operator": "AND", "operands": [
                {"operator": "eq", "operands": ["region", "us"]},
                *extra,
                {"operator": "gt", "operands": ["intradaymarketcap", min_cap]},
                {"operator": "gt", "operands": ["avgdailyvol3m", 500_000]},
                {"operator": "or", "operands": [{"operator": "eq", "operands": ["exchange", e]}
                                                 for e in ("NMS", "NYQ", "NGM", "NCM")]}]}}
    r = sess.post("https://query1.finance.yahoo.com/v1/finance/screener",
                  params={"crumb": crumb, "lang": "en-US", "region": "US", "formatted": "false"},
                  json=body, timeout=25)
    r.raise_for_status()
    return [{"symbol": q["symbol"], "name": clean_name(q["symbol"], q.get("shortName")),
             "change_pct": round(q["regularMarketChangePercent"], 2),
             "market_cap_bn": round(q.get("marketCap", 0) / 1e9, 1),
             "date": datetime.fromtimestamp(q["regularMarketTime"], timezone.utc).strftime("%Y-%m-%d")}
            for q in r.json()["finance"]["result"][0]["quotes"]
            if q.get("regularMarketChangePercent") is not None]


def gather_movers(sectors, n_overall=5, n_sector=4, hot=4):
    """Biggest gainers and losers across the market, and inside the sectors with
    the largest ETF moves (up or down), from Yahoo's stock screener. It needs a
    session cookie plus a crumb. Mid and large caps only, so the list is not
    dominated by penny-stock noise."""
    sess = requests.Session()
    sess.headers.update(UA)
    sess.get("https://fc.yahoo.com", timeout=20)
    crumb = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text
    if not crumb or "<" in crumb:
        raise RuntimeError("no Yahoo crumb")
    pair = lambda extra, n, cap: {k: screen(sess, crumb, extra, n, o, cap)
                                  for k, o in (("gainers", "DESC"), ("losers", "ASC"))}
    out = {"overall": pair([], n_overall, 5_000_000_000), "by_sector": {}}
    ranked = sorted(sectors, key=lambda r: abs(r["change_pct"]), reverse=True)[:hot]
    for sec in ranked:
        extra = [{"operator": "eq", "operands": ["sector", sec["yahoo_sector"]]}]
        out["by_sector"][sec["yahoo_sector"]] = pair(extra, n_sector, 1_500_000_000)
    return out


def gather_headlines(per_topic=8, max_age_h=36):
    items, seen, n = [], set(), 0
    now = datetime.now(timezone.utc)
    for topic, q in TOPICS.items():
        url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(q + " when:2d")
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log(f"news {topic} failed: {e}")
            continue
        kept = 0
        for e in feed.entries:
            title = (e.get("title") or "").strip()
            src = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
            if src and title.endswith(" - " + src):
                title = title[: -len(" - " + src)]
            key = re.sub(r"\W+", "", title.lower())[:60]
            if not title or key in seen:
                continue
            try:
                pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
            if (now - pub).total_seconds() > max_age_h * 3600:
                continue
            seen.add(key)
            n += 1
            items.append({"id": f"h{n}", "topic": topic, "title": title, "source": src or "",
                          "url": e.get("link", ""), "published": pub.strftime("%Y-%m-%d %H:%M UTC")})
            kept += 1
            if kept >= per_topic:
                break
    return items


def gather_usdcny(n=30):
    try:
        d = json.loads(HISTORY.read_text())
        return d["categories"]["external"]["series"]["usdcny"]["data"][-n:]
    except Exception:
        return []


def time_note():
    et = datetime.now(ZoneInfo("America/New_York"))
    hours = int((8 * 3600 - et.utcoffset().total_seconds()) // 3600)
    return f"当前美东时间为 UTC{et.utcoffset().total_seconds() / 3600:+.0f}，北京时间比美东时间快 {hours} 小时。"


# ------------------------------------------------------------- narrative

SYSTEM = """你是中文财经新闻编辑，为网站撰写每个交易日早晨发布的美股与宏观市场简报。读者在中国，不便阅读英文，需要了解昨夜至今晨的市场与热门板块动态。
严格遵守：
1. 只使用资料里出现的事实和数字，不得编造数字、日期、时间或公司动作；不确定的写“据报道”或“待确认”。
2. 语言：书面、简洁、通俗的新闻语体，参考通讯社和财经媒体的中文稿；不用口语，不用网络用语，不做教学式解释。
3. 标点：一律使用中文全角标点（，。；：（）“”），日期、数字、百分号中的符号除外。
4. 新闻只给了英文标题，只陈述标题能支持的内容；多个来源口径不一致时，写明口径不一。
5. 每条事实性要点用 refs 引用资料里的新闻编号（1 至 2 个，如 "h12"）。行情与股价数字来自资料中的行情部分，不需要 refs。不要写网址，也不要在正文里写来源名称。
6. 人名、公司名、产品名首次出现时写成“中文名（英文名或股票代码）”，例如“英伟达（Nvidia）”。
7. 每条要点一到两句，不超过 80 个汉字。
8. 行情数字写明日期；标注“未收盘”的数据不要写成收盘价。
9. 新闻源的发布时间不完全可靠，个别旧闻会被标成当天。只有一个来源、且标题像是旧事件的内容，不要当作今日要闻。
10. 不要写利率的具体区间，除非标题中明确给出；可以写“维持不变”。
11. 个股异动只有标题能支持原因时才写原因，否则写“原因待确认”，不要猜测。
12. “分析”组（1 至 3 条，可省略）：基于上文事实的专业解读，例如对利率、盈利、估值或资金面的含义；有事实依据，简短克制，不预测具体价位，不写常识性解释。
13. 日程只列资料或标题明确给出的已排定事件；时间按资料中的时差说明换算为北京时间，并在括号内保留美东时间；拿不准的不列。
14. 中国宏观数据（GDP、PMI、CPI 等）不是本简报内容，不要写。
只输出一个 JSON 对象，不要任何其他文字。"""

PLAN_TASK = """任务：根据下列资料，决定今天简报的结构。第一节固定为“宏观与市场”（markets）；之后根据行业 ETF 涨跌、个股异动和新闻量，挑出当天最热的 2 到 4 个板块各一节，没有固定板块，当天没有新闻的板块不要选。
每节给出 headline_ids：写这一节最需要的新闻编号，最多 12 个，可多节共用。
sector 只能取：Technology、Communication Services、Consumer Cyclical、Consumer Defensive、Healthcare、Financial Services、Industrials、Energy、Utilities、Basic Materials、Real Estate，对应不上则为 null；markets 节为 null。
输出格式：
{"sections": [{"title": "宏观与市场", "sector": null, "headline_ids": ["h1"]}, {"title": "科技与 AI", "sector": "Technology", "headline_ids": ["h2"]}]}"""

SECTION_TASK = """任务：为简报写“{title}”一节。输出格式：
{{"groups": [{{"h": "今日要闻", "items": [{{"t": "要点", "refs": ["h1"]}}]}}, {{"h": "分析", "items": [{{"t": "解读"}}]}}]}}
今日要闻 {lo} 到 {hi} 条，按重要性排序；分析 1 至 3 条。"""

FINISH_TASK = """任务：根据下列已写好的各节要点，写简报的开头与结尾部分。输出格式：
{"theme": "今日核心主题，一句话", "tldr": ["4 到 6 条，每条一句话，最重要的事放最前面，只概括下列要点里已有的事实"], "trends": [{"t": "长期趋势或风险，3 到 5 条", "refs": []}], "calendar": [{"when": "10 月 29 日 20:30（美东 8:30）", "event": "事件", "watch": "关注点", "refs": []}]}
calendar 只列“候选新闻”里明确给出的已排定事件，没有就给空数组。"""


class LLMError(RuntimeError):
    pass


def _post(url, headers, body, tries=4):
    """POST with backoff on rate limits and transient server errors."""
    last = None
    for i in range(tries):
        r = requests.post(url, headers=headers, json=body, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            time.sleep(8 * (i + 1))
            continue
        if not r.ok:
            raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise LLMError(last or "request failed")


def _gemini(system, user, max_tokens):
    model = os.environ.get("BRIEF_GEMINI_MODEL", "gemini-2.5-flash")
    j = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
              {"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
              {"systemInstruction": {"parts": [{"text": system}]},
               "contents": [{"role": "user", "parts": [{"text": user}]}],
               "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                    "maxOutputTokens": max_tokens}})
    return "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])


def _github_models(system, user, max_tokens):
    model = os.environ.get("BRIEF_GH_MODEL", "openai/gpt-4.1")
    j = _post("https://models.github.ai/inference/chat/completions",
              {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
              {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    return j["choices"][0]["message"]["content"]


def llm_providers():
    """Free providers only: a Google AI Studio free-tier key, or GitHub Models via the
    Actions token (needs `permissions: models: read`). Nothing here is billed."""
    out = []
    if os.environ.get("GEMINI_API_KEY"):
        out.append(_gemini)
    if os.environ.get("GITHUB_TOKEN"):
        out.append(_github_models)
    return out


def llm_complete(system, user, max_tokens=2500):
    errors = []
    for provider in llm_providers():
        try:
            return provider(system, user, max_tokens)
        except Exception as e:
            errors.append(f"{provider.__name__}: {e}")
    raise LLMError("; ".join(errors) or "no free LLM provider configured")


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def ask(task, data, max_tokens=2500, validate=None, pause=4):
    """One small model call, JSON out, retried once on bad output. Calls stay small
    on purpose: GitHub Models' free tier caps a request at roughly 8k tokens in and
    4k out, and short calls also keep Gemini's free quota use negligible."""
    last = None
    for attempt in range(2):
        time.sleep(pause)
        try:
            obj = parse_json(llm_complete(SYSTEM, f"{task}\n\n资料：\n{data}", max_tokens))
            if validate:
                validate(obj)
            return obj
        except LLMError:
            raise
        except Exception as e:
            last = e
            log(f"model output rejected (attempt {attempt + 1}): {e}")
    raise LLMError(f"model output kept failing validation: {last}")


def market_lines(facts):
    tiles = build_snapshot(facts["markets"], facts.get("usdcny") or [])
    lines = [f"{t['label']}：{t['value']}，{t['change']}（{t['note']}）" for t in tiles]
    lines.append("行业 ETF 当日涨跌：" + "，".join(f"{r['name']}{r['change_pct']:+.2f}%" for r in facts.get("sectors") or []))
    return lines


def mover_lines(movers, label):
    def fmt(rows):
        return "，".join(f"{r['name']}（{r['symbol']}）{r['change_pct']:+.1f}%" for r in rows)
    return [f"{label}涨幅居前：{fmt(movers.get('gainers', []))}", f"{label}跌幅居前：{fmt(movers.get('losers', []))}"]


def headline_lines(headlines, ids=None):
    pick = [h for h in headlines if ids is None or h["id"] in ids]
    return [f"{h['id']} | {h['title'][:120]} | {(h['source'] or '')[:18]}" for h in pick]


def narrate(facts):
    heads = facts["headlines"]
    head_ids = {h["id"] for h in heads}
    header = [f"今天是 {facts['date']}{facts['weekday']}（北京时间）早晨。", facts["time_note"]]
    mk = market_lines(facts)
    movers = facts.get("movers") or {}

    # 1. which sections today
    def check_plan(o):
        secs = o.get("sections") or []
        if not (3 <= len(secs) <= 5) or secs[0].get("sector") is not None:
            raise ValueError("plan needs markets plus 2 to 4 sector sections")
        for sec in secs:
            if sec.get("sector") is not None and sec["sector"] not in SECTOR_NAMES:
                raise ValueError(f"bad sector {sec.get('sector')}")
    overall = mover_lines(movers.get("overall", {}), "全市场")
    plan = ask(PLAN_TASK, "\n".join(header + mk + overall + ["新闻标题（编号 | 标题 | 来源）："] + headline_lines(heads)),
               max_tokens=800, validate=check_plan)["sections"]

    # 2. one small call per section
    sections = []
    for i, sec in enumerate(plan):
        ids = {x for x in sec.get("headline_ids", []) if x in head_ids} or None
        lines = header + mk if i == 0 else header + [l for l in mk if l.startswith("行业")]
        if sec.get("sector") and sec["sector"] in (movers.get("by_sector") or {}):
            lines += mover_lines(movers["by_sector"][sec["sector"]], f"{sec['title']}板块")
        lines += ["新闻标题（编号 | 标题 | 来源）："] + headline_lines(heads, ids)

        def check_groups(o):
            validate_narrative_section({"id": "x", "title": "x", "sector": None, "groups": o.get("groups")}, head_ids)
        task = SECTION_TASK.format(title=sec["title"], lo=4 if i == 0 else 3, hi=7)
        groups = ask(task, "\n".join(lines), max_tokens=2200, validate=check_groups)["groups"]
        sections.append({"id": "markets" if i == 0 else f"s{i}", "title": "宏观与市场" if i == 0 else sec["title"],
                         "sector": sec.get("sector"), "groups": groups})

    # 3. theme, TL;DR, trends and schedule, written from the finished sections
    written = [f"【{s['title']}】" + "；".join(it["t"] for g in s["groups"] for it in g["items"]) for s in sections]
    cand = [h for h in heads if h["topic"] in ("fed_macro", "econ_data", "earnings", "markets")][:40]

    def check_finish(o):
        validate_narrative({**o, "sections": sections}, head_ids)
    tail = ask(FINISH_TASK, "\n".join(header + written + ["候选新闻（编号 | 标题 | 来源）："] + headline_lines(cand)),
               max_tokens=2000, validate=check_finish)
    return {"theme": tail["theme"], "tldr": tail["tldr"], "sections": sections,
            "trends": tail.get("trends") or [], "calendar": tail.get("calendar") or []}


def _check_refs(item, headline_ids):
    bad = [r for r in item.get("refs", []) if r not in headline_ids]
    if bad:
        raise ValueError(f"unknown refs {bad}")


def validate_narrative_section(s, headline_ids):
    groups = s.get("groups") or []
    if not groups or groups[0].get("h") != "今日要闻":
        raise ValueError(f"{s.get('id')} must start with 今日要闻")
    for g in groups:
        if g.get("h") not in ("今日要闻", "分析") or not g.get("items"):
            raise ValueError(f"bad group in {s.get('id')}")
        for it in g["items"]:
            if not isinstance(it.get("t"), str) or not it["t"]:
                raise ValueError("empty item")
            _check_refs(it, headline_ids)


def validate_narrative(obj, headline_ids):
    if not isinstance(obj.get("theme"), str) or not obj["theme"]:
        raise ValueError("bad theme")
    if not (isinstance(obj.get("tldr"), list) and 2 <= len(obj["tldr"]) <= 7
            and all(isinstance(x, str) and x for x in obj["tldr"])):
        raise ValueError("bad tldr")
    secs = obj.get("sections") or []
    if not (3 <= len(secs) <= 6) or secs[0].get("id") != "markets":
        raise ValueError("need markets plus 2 to 5 sector sections")
    for s in secs:
        if not isinstance(s.get("title"), str) or not s["title"]:
            raise ValueError("section without title")
        if s.get("sector") is not None and s["sector"] not in SECTOR_NAMES:
            raise ValueError(f"bad sector {s.get('sector')}")
        validate_narrative_section(s, headline_ids)
    for it in obj.get("trends") or []:
        if not isinstance(it.get("t"), str) or not it["t"]:
            raise ValueError("empty trend")
        _check_refs(it, headline_ids)
    for row in obj.get("calendar") or []:
        if not all(isinstance(row.get(k), str) and row[k] for k in ("when", "event", "watch")):
            raise ValueError("bad calendar row")
        _check_refs(row, headline_ids)


# -------------------------------------------------------------- assembly

NUMERALS = "一二三四五六七八九十"
BASE_CHARTS = ["usmoves", "sector_perf", "movers_all"]


def tile(key, label, kind, cur, base, series, note):
    """One market-snapshot tile: value, direction, and the points behind its chart."""
    if kind == "pct":
        diff, change, value, dec = cur - base, f"{cur - base:+.2f} 个百分点", f"{cur:.2f}%", 2
    else:
        diff = pct(cur, base)
        change, value, dec = f"{diff:+.2f}%", f"{cur:,.2f}", 2 if cur < 1000 else 0
        if kind == "fx":
            value, dec = f"{cur:.4f}", 4
    return {"key": key, "label": label, "value": value, "change": change,
            "dir": "up" if diff > 0 else "down" if diff < 0 else "flat", "note": note,
            "dec": dec, "series": series}


CJK = r"㐀-鿿＀-￯"


def cn_punct(text):
    """Full-width punctuation for Chinese text, leaving digit-internal symbols
    (7,743.98 / 2:00 / 10/28) and ASCII abbreviations alone."""
    if not re.search(rf"[{CJK}]", text):
        return text
    t = text.replace("「", "“").replace("」", "”").replace("『", "‘").replace("』", "’")
    t = re.sub(r"(?<!\d),\s*|,\s*(?!\d)", "，", t)
    t = re.sub(r"\s*;\s*", "；", t)
    t = re.sub(r"(?<!\d):\s*|:\s+(?=\d)", "：", t)
    t = re.sub(r"\s*\?", "？", t)
    t = re.sub(r"\s*!", "！", t)
    t = re.sub(r"\.$", "。", t)
    return re.sub(r"\(([^()]*)\)", lambda m: f"（{m.group(1)}）", t)


def build_snapshot(markets, usdcny):
    tiles = []
    for key in TILE_KEYS:
        m = markets.get(key)
        if not m:
            continue
        cur = m["intraday"] or m["last"]
        base = m["last"] if m["intraday"] else m["prev"]
        series = [[b["date"], b["close"]] for b in m["bars"]]
        if m["intraday"] and series[-1][0] != cur["date"]:
            series.append([cur["date"], cur["close"]])
        d = cur["date"][5:].replace("-", "/")
        tiles.append(tile(key, m["label"], m["kind"], cur["close"], base["close"], series,
                          f"{d} {'最新（未收盘）' if m['intraday'] else '收盘'}"))
    if len(usdcny) >= 2:
        last, prev = usdcny[-1], usdcny[-2]
        tiles.append(tile("usdcny", "美元兑人民币", "fx", last["value"], prev["value"],
                          [[p["date"], p["value"]] for p in usdcny],
                          f"{last['date'][5:].replace('-', '/')}（欧洲央行参考汇率）"))
    return tiles


def build_usmoves(markets):
    spx, ndx = markets.get("spx"), markets.get("ndx")
    if not spx or not ndx:
        return None
    common = [b["date"] for b in spx["bars"] if b["date"] in {x["date"] for x in ndx["bars"]}][-6:]

    def moves(m):
        close = {b["date"]: b["close"] for b in m["bars"]}
        ordered = [b["date"] for b in m["bars"]]
        return {d: round(pct(close[d], close[ordered[ordered.index(d) - 1]]), 2)
                for d in common if ordered.index(d) > 0}
    ms, mn = moves(spx), moves(ndx)
    cats = [d for d in common if d in ms and d in mn][-5:]
    return {"kind": "bars", "title": "美股单日涨跌幅（%）",
            "desc": f"标普 500 与纳斯达克，最近 {len(cats)} 个交易日。",
            "categories": [c[5:].replace("-", "/") for c in cats],
            "series": [{"name": "标普 500", "values": [ms[c] for c in cats]},
                       {"name": "纳斯达克", "values": [mn[c] for c in cats]}]}


def build_sector_chart(sectors):
    if len(sectors) < 5:
        return None
    return {"kind": "hbars", "title": "标普 500 行业板块单日涨跌幅（%）",
            "desc": f"{sectors[0]['date'][5:].replace('-', '/')} 美股交易日，按行业 ETF 计算。",
            "rows": [{"label": f"{r['name']}（{r['etf']}）", "value": r["change_pct"]} for r in sectors]}


def build_movers_chart(title, movers, n=5):
    rows = (movers.get("gainers") or [])[:n] + (movers.get("losers") or [])[:n]
    if len(rows) < 4:
        return None
    rows = sorted({r["symbol"]: r for r in rows}.values(), key=lambda r: r["change_pct"], reverse=True)
    return {"kind": "hbars", "title": f"{title}（涨跌幅前 {n}）",
            "desc": f"{rows[0]['date'][5:].replace('-', '/')} 美股交易日，市值 {'50' if title.startswith('美股') else '15'} 亿美元以上。",
            "rows": [{"label": f"{r['name']}（{r['symbol']}）", "value": r["change_pct"]} for r in rows]}


def resolve_src(item, by_id):
    if item.get("src"):
        return item["src"]
    out = []
    for r in item.get("refs", [])[:2]:
        h = by_id.get(r)
        if h and h["url"]:
            name = (h["source"] or "来源").split(" - ")[0].strip()  # e.g. "ABC News - Breaking News, ..."
            out.append({"n": name if name.startswith(CHINESE_OUTLETS) else f"{name}（英文）",
                        "u": h["url"], "t": h["title"]})
    return out


def sector_chart_id(sector):
    return f"movers:{sector}"


def assemble(facts, narrative, prev, now_iso, narrative_fresh):
    by_id = {h["id"]: h for h in facts["headlines"]}
    prev_charts = (prev or {}).get("charts", {})
    charts = {}
    if facts["markets"]:
        charts["usmoves"] = build_usmoves(facts["markets"])
    charts["sector_perf"] = build_sector_chart(facts.get("sectors") or [])
    movers = facts.get("movers") or {}
    if movers.get("overall"):
        charts["movers_all"] = build_movers_chart("美股异动榜", movers["overall"])
    for sector, m in (movers.get("by_sector") or {}).items():
        charts[sector_chart_id(sector)] = build_movers_chart(f"{SECTOR_ZH.get(sector, sector)}板块异动股", m, 4)

    collected, seen = [], set()

    def cite(item):
        for r in resolve_src(item, by_id):
            if r["u"] not in seen:
                seen.add(r["u"])
                collected.append(r)

    sections = []
    for i, s in enumerate(narrative["sections"]):
        groups = []
        for g in s["groups"]:
            for it in g["items"]:
                cite(it)
            groups.append({"h": g["h"], "items": [{"t": cn_punct(it["t"])} for it in g["items"]]})
        want = BASE_CHARTS if s["id"] == "markets" else ([sector_chart_id(s["sector"])] if s.get("sector") else [])
        for c in want:
            if not charts.get(c) and prev_charts.get(c):
                charts[c] = prev_charts[c]
        sections.append({"id": s["id"], "title": f"{NUMERALS[i]}、{s['title']}", "groups": groups,
                         "charts": [c for c in want if charts.get(c)]})
    trends = []
    for it in narrative.get("trends") or []:
        cite(it)
        trends.append(cn_punct(it["t"]))
    calendar = []
    for row in narrative.get("calendar") or []:
        cite(row)
        calendar.append({k: cn_punct(row[k]) for k in ("when", "event", "watch")})
    snapshot = (build_snapshot(facts["markets"], facts.get("usdcny") or []) if facts["markets"]
                else (prev or {}).get("snapshot", []))
    return {
        "date": facts["date"], "weekday": facts["weekday"],
        "generated_at": now_iso,
        "narrative_at": now_iso if narrative_fresh else (prev or {}).get("narrative_at", now_iso),
        "theme": cn_punct(narrative["theme"]),
        "tldr": [cn_punct(t) for t in narrative["tldr"]], "snapshot": snapshot,
        "sections": sections, "trends": trends, "calendar": calendar,
        "charts": {k: v for k, v in charts.items() if v},
        "sources": narrative.get("_sources") or collected,
        "sources_note": "行情与股价数据来自 Yahoo Finance。新闻编译自上列英文媒体报道，仅依据标题整理。",
        "disclaimer": "本简报由 AI 根据公开新闻标题与行情数据自动编译，可能有误，仅供参考，不构成投资建议。",
    }


def prev_narrative(prev):
    """Re-express the previous brief as a narrative so a failed LLM call keeps yesterday's text."""
    return {"theme": prev["theme"], "tldr": prev["tldr"], "_sources": prev.get("sources"),
            "sections": [{"id": s["id"], "title": s["title"].split("、", 1)[-1], "sector": None,
                          "groups": s["groups"]} for s in prev["sections"]],
            "trends": [{"t": t} for t in prev.get("trends", [])], "calendar": prev.get("calendar", [])}


def write_status(status, fresh):
    """Small public record of the last run. GitHub's run logs need a login to read, so this
    is how a failed free-model call can be diagnosed from the repo itself. It never holds
    secrets: errors carry HTTP status and response text only, keys travel in headers."""
    status["narrative"] = "fresh" if fresh else "stale"
    (OUT.parent / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts-only", action="store_true")
    ap.add_argument("--narrative-file")
    ap.add_argument("--force", action="store_true", help="run even on a non-trading day or if today's brief is fresh")
    args = ap.parse_args()

    now = datetime.now(BJT)
    today = now.strftime("%Y-%m-%d")
    prev = json.loads(OUT.read_text()) if OUT.exists() else None
    if not (args.force or args.facts_only or args.narrative_file):
        if not is_china_trading_day(today):
            log(f"{today} is not a China trading day; leaving the brief as is")
            return 0
        if prev and prev.get("narrative_at", "")[:10] == today:
            log(f"today's brief already generated at {prev['narrative_at']}; nothing to do")
            return 0
    sectors = gather_sectors()
    try:
        movers = gather_movers(sectors) if sectors else {}
    except Exception as e:
        log(f"movers failed: {e}")
        movers = {}
    facts = {"date": today, "weekday": "周" + "一二三四五六日"[now.weekday()], "time_note": time_note(),
             "markets": gather_markets(), "sectors": sectors, "movers": movers,
             "headlines": gather_headlines(), "usdcny": gather_usdcny()}
    log(f"facts: {len(facts['markets'])} markets, {len(sectors)} sectors, {len(facts['headlines'])} headlines")
    if args.facts_only:
        print(json.dumps(facts, ensure_ascii=False, indent=1))
        return 0

    fresh, status = True, {"run_at": now.isoformat(timespec="seconds"), "provider": None, "error": None}
    try:
        if args.narrative_file:
            narrative = json.loads(Path(args.narrative_file).read_text())
        elif llm_providers():
            status["provider"] = [p.__name__.lstrip("_") for p in llm_providers()]
            narrative = narrate(facts)
        else:
            raise LLMError("no free LLM provider configured (set GEMINI_API_KEY, or run in Actions with models: read)")
    except Exception as e:
        log(f"narrative unavailable, keeping previous text:\n{traceback.format_exc()}")
        status["error"] = f"{type(e).__name__}: {e}"[:600]
        if not prev or "theme" not in prev:
            write_status(status, fresh=False)
            return 1
        narrative, fresh = prev_narrative(prev), False

    if not facts["markets"] and not prev:
        log("no market data and no previous brief; refusing to publish")
        return 1
    brief = assemble(facts, narrative, prev, now.isoformat(timespec="seconds"), fresh)
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(brief, ensure_ascii=False, indent=1, allow_nan=False) + "\n")
    tmp.replace(OUT)
    write_status(status, fresh)
    log(f"wrote {OUT.relative_to(ROOT)} (narrative {'fresh' if fresh else 'STALE'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
