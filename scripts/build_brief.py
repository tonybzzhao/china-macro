#!/usr/bin/env python3
"""Build brief/brief.json, the data behind chatccp.com/brief.

A Simplified-Chinese morning briefing for readers in China who can't easily
follow English-language news: US markets and policy, tech and AI, biotech and
pharma, and the day's international headlines, rewritten in plain Chinese with
links back to the English originals.

Runs on China trading mornings from .github/workflows/brief.yml. Three parts:

  1. Facts, gathered deterministically: market and stock closes (Yahoo chart
     API) and topical headlines (Google News RSS, English edition).
  2. Narrative (TL;DR and bullets) written in Chinese by Claude from those facts
     only. Needs ANTHROPIC_API_KEY. The model never writes URLs or market
     numbers: it cites headlines by id and the script resolves the links, while
     tiles and charts come straight from the facts.
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
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"
OUT = ROOT / "brief" / "brief.json"
MODEL = os.environ.get("BRIEF_MODEL", "claude-sonnet-5-5")
UA = {"User-Agent": "Mozilla/5.0"}
BJT = timezone(timedelta(hours=8))

MARKETS = [  # key, yahoo symbol, label, kind
    ("spx", "^GSPC", "标普 500", "idx"),
    ("ndx", "^IXIC", "纳斯达克", "idx"),
    ("dji", "^DJI", "道琼斯", "idx"),
    ("sox", "^SOX", "费城半导体指数", "idx"),
    ("xbi", "XBI", "生物科技 ETF（XBI）", "idx"),
    ("us10y", "^TNX", "美国 10 年期国债收益率", "pct"),
    ("brent", "BZ=F", "布伦特原油", "usd"),
    ("gold", "GC=F", "黄金", "usd"),
]

STOCKS = {  # chart id -> (title, [(symbol, 中文名)])
    "tech_stocks": ("科技股单日涨跌幅（%）", [
        ("NVDA", "英伟达"), ("MSFT", "微软"), ("GOOGL", "谷歌 Alphabet"), ("AMZN", "亚马逊"),
        ("META", "Meta"), ("AAPL", "苹果"), ("TSLA", "特斯拉"), ("AMD", "超微 AMD")]),
    "biotech_stocks": ("医药股单日涨跌幅（%）", [
        ("LLY", "礼来"), ("NVO", "诺和诺德"), ("PFE", "辉瑞"), ("MRK", "默沙东"),
        ("JNJ", "强生"), ("ABBV", "艾伯维"), ("AMGN", "安进"), ("REGN", "再生元")]),
}

MOVER_SETS = {  # section -> (Yahoo screener field, values)
    "tech": ("sector", ["Technology"]),
    "biotech": ("industry", ["Biotechnology", "Drug Manufacturers - General",
                              "Drug Manufacturers - Specialty & Generic"]),
}
MOVER_TITLES = {"tech": "科技股异动榜", "biotech": "生物医药股异动榜"}
NAME_SUFFIX = re.compile(r",?\s+(Inc\.?|Corporation|Corp\.?|Ltd\.?|Limited|plc|A/S|S\.A\.|N\.V\.|Holdings?|Group|Company|Co\.)\b.*$", re.I)
ZH_NAMES = {"SKHY": "SK 海力士", "ORCL": "甲骨文", "GMAB": "Genmab", "GSK": "葛兰素史克", "BSX": "波士顿科学",
            "BIIB": "渤健", "LOGI": "罗技", "FTNT": "飞塔", "FFIV": "F5", "GRMN": "佳明", "ICLR": "ICON",
            "ENPH": "Enphase", "CBRS": "Cerebras", "FLEX": "伟创力", "CLS": "Celestica", "MRNA": "莫德纳",
            "VRTX": "福泰制药", "GILD": "吉利德", "BMY": "百时美施贵宝", "AZN": "阿斯利康", "NVS": "诺华",
            "SNY": "赛诺菲", "TSM": "台积电", "MU": "美光", "INTC": "英特尔", "QCOM": "高通", "AVGO": "博通",
            "CRM": "赛富时", "ADBE": "奥多比", "NFLX": "奈飞", "PLTR": "Palantir", "SMCI": "超微电脑"}

TOPICS = {  # Google News queries, English because that is where the news breaks
    "fed_macro": "Federal Reserve OR inflation OR Treasury yields OR jobs report OR tariffs",
    "markets": "Wall Street stocks close S&P 500 Nasdaq",
    "oil_geo": "oil prices OR Iran OR Houthi OR Saudi OR Middle East",
    "ai_tech": "AI chips OR OpenAI OR Nvidia OR Microsoft OR Alphabet OR Apple OR Meta earnings",
    "ai_models": "Anthropic OR OpenAI OR Google Gemini OR AI regulation OR data center",
    "biotech": "FDA approval OR biotech OR pharma OR clinical trial OR GLP-1 OR Eli Lilly OR Novo Nordisk",
    "health_policy": "drug pricing OR Medicare OR FDA OR vaccine OR cancer drug",
    "world": "Ukraine OR Russia OR Israel OR Gaza OR NATO OR European Union OR Japan OR India",
    "us_politics": "Trump OR Congress OR midterm elections OR Supreme Court OR White House",
    "us_china": "US China tariffs OR chips export controls OR Xi Jinping OR Taiwan",
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


def gather_stocks():
    out = {}
    for chart_id, (_, names) in STOCKS.items():
        rows = []
        for sym, name in names:
            try:
                s = summarize(name, "usd", *fetch_market(sym))
                cur = s["intraday"] or s["last"]
                base = s["last"] if s["intraday"] else s["prev"]
                rows.append({"symbol": sym, "name": name, "date": cur["date"],
                             "change_pct": round(pct(cur["close"], base["close"]), 2)})
            except Exception as e:
                log(f"stock {sym} failed: {e}")
        out[chart_id] = rows
    return out


def clean_name(symbol, raw):
    if symbol in ZH_NAMES:
        return ZH_NAMES[symbol]
    return NAME_SUFFIX.sub("", raw or symbol).strip()[:22]


def gather_movers(n=5):
    """Biggest gainers and losers per section from Yahoo's stock screener.

    The screener needs a session cookie plus a crumb. Mid and large caps only
    (market cap over $1.5B, decent volume) so the list is not dominated by
    penny-stock noise."""
    sess = requests.Session()
    sess.headers.update(UA)
    sess.get("https://fc.yahoo.com", timeout=20)
    crumb = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text
    if not crumb or "<" in crumb:
        raise RuntimeError("no Yahoo crumb")
    out = {}
    for section, (field, values) in MOVER_SETS.items():
        picked = {}
        for order, key in (("DESC", "gainers"), ("ASC", "losers")):
            body = {"size": n, "offset": 0, "sortField": "percentchange", "sortType": order,
                    "quoteType": "EQUITY", "topOperator": "AND",
                    "query": {"operator": "AND", "operands": [
                        {"operator": "eq", "operands": ["region", "us"]},
                        {"operator": "or", "operands": [{"operator": "eq", "operands": [field, v]} for v in values]},
                        {"operator": "gt", "operands": ["intradaymarketcap", 1_500_000_000]},
                        {"operator": "gt", "operands": ["avgdailyvol3m", 400_000]},
                        {"operator": "or", "operands": [{"operator": "eq", "operands": ["exchange", e]}
                                                         for e in ("NMS", "NYQ", "NGM", "NCM")]}]}}
            r = sess.post("https://query1.finance.yahoo.com/v1/finance/screener",
                          params={"crumb": crumb, "lang": "en-US", "region": "US", "formatted": "false"},
                          json=body, timeout=25)
            r.raise_for_status()
            quotes = r.json()["finance"]["result"][0]["quotes"]
            picked[key] = [{"symbol": q["symbol"], "name": clean_name(q["symbol"], q.get("shortName")),
                            "change_pct": round(q["regularMarketChangePercent"], 2),
                            "market_cap_bn": round(q.get("marketCap", 0) / 1e9, 1),
                            "date": datetime.fromtimestamp(q["regularMarketTime"], timezone.utc).strftime("%Y-%m-%d")}
                           for q in quotes if q.get("regularMarketChangePercent") is not None]
        out[section] = picked
    return out


def gather_headlines(per_topic=9, max_age_h=36):
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


# ------------------------------------------------------------- narrative

SYSTEM = """你是中文财经新闻编辑，为网站撰写每个交易日早晨发布的市场简报。读者在中国，不便阅读英文，需要了解昨夜至今晨的美国市场、科技、生物医药和国际要闻。
严格遵守：
1. 只使用 FACTS 里出现的事实和数字，不得编造数字、日期或公司动作；不确定的写“据报道”或“待确认”。
2. 只陈述事实。不评论，不解释意义，不预测，不添加背景铺垫，不使用“意味着”“说明”“或将”“值得关注”这类判断性措辞。
3. 语言：书面、简洁、通俗的新闻语体，参考通讯社和财经媒体的中文稿；不用口语，不用网络用语。
4. 标点：一律使用中文全角标点（，。；：（）“”），日期、数字、百分号中的符号除外。
5. 新闻只给了英文标题，只陈述标题能支持的内容；多个来源口径不一致时，写明口径不一。
6. 每条事实性要点用 refs 引用 FACTS.headlines 里的 id（1 至 2 个）。市场与股价数字来自 FACTS.markets、FACTS.stock_moves 与 FACTS.movers，不需要 refs。不要写网址，也不要在正文里写来源名称。
7. 人名、公司名、药名、产品名首次出现时写成“中文名（英文名或股票代码）”，例如“英伟达（Nvidia）”，便于对照英文报道。
8. 每条要点一到两句，不超过 70 个汉字。
9. 市场数字以 FACTS.markets 为准，写明日期；intraday 非空表示该资产最新一根尚未收盘。
10. 新闻源的发布时间不完全可靠，个别旧闻会被标成当天。只有一个来源、且标题像是旧事件的内容，不要当作今日要闻；能被两个以上来源标题印证的事实才放进“今日要闻”。
11. FACTS.movers 是科技与生物医药板块当天涨跌幅最大的个股（市值 15 亿美元以上）。各板块“今日要闻”里用一条写出最突出的 2 到 3 只；只有标题能支持的原因才写，没有依据就写“原因待确认”，不要猜测。
12. 不要写利率的具体区间，除非标题中明确给出；可以写“维持不变”。
13. 中国宏观数据（GDP、PMI、CPI 等）不是本简报内容，不要写；中美关系、芯片出口管制、关税、台海等国际新闻可在“世界头条”简短报道。
只输出一个 JSON 对象，不要任何其他文字。"""

SCHEMA = """{
 "tldr": ["3 到 4 条，每条一句话，最重要的事放最前面"],
 "sections": [
  {"id": "markets", "groups": [{"h": "今日要闻", "items": [{"t": "要点", "refs": ["h1"]}]}, {"h": "接下来关注", "items": [{"t": "已排定的事件与日期，没有则省略该组"}]}]},
  {"id": "tech", "groups": ["同上"]},
  {"id": "biotech", "groups": ["同上"]},
  {"id": "world", "groups": ["同上；今日要闻放 5 到 8 条世界头条，覆盖不同地区与议题，包括美国政治"]}
 ]
}
sections 的 id 依次为 markets、tech、biotech、world，标题由程序添加。今日要闻每板块 4 到 8 条。"""

SECTION_IDS = ["markets", "tech", "biotech", "world"]


def narrate(facts):
    import anthropic  # imported late so --facts-only works without the SDK
    client = anthropic.Anthropic()
    slim = dict(facts, markets={k: dict(m, bars=m["bars"][-5:]) for k, m in facts["markets"].items()},
                usdcny=facts["usdcny"][-3:])
    prompt = (f"今天是 {facts['date']}{facts['weekday']}(北京时间)早晨。读者每个交易日早晨阅读,"
              "请以「昨夜到今晨发生了什么」的视角写作。\n\nFACTS:\n"
              + json.dumps(slim, ensure_ascii=False)
              + f"\n\n按此 JSON 结构输出:\n{SCHEMA}")
    last_err = None
    for attempt in range(2):
        msg = client.messages.create(model=MODEL, max_tokens=9000, system=SYSTEM,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
            validate_narrative(obj, {h["id"] for h in facts["headlines"]})
            return obj
        except Exception as e:
            last_err = e
            log(f"narrative attempt {attempt + 1} rejected: {e}")
    raise RuntimeError(f"narrative failed validation: {last_err}")


def validate_narrative(obj, headline_ids):
    if not (isinstance(obj.get("tldr"), list) and 2 <= len(obj["tldr"]) <= 5
            and all(isinstance(x, str) and x for x in obj["tldr"])):
        raise ValueError("bad tldr")
    ids = [s.get("id") for s in obj.get("sections", [])]
    if ids != SECTION_IDS:
        raise ValueError(f"bad section ids {ids}")
    for s in obj["sections"]:
        if not s.get("groups"):
            raise ValueError(f"{s['id']} has no groups")
        if s["groups"][0].get("h") != "今日要闻":
            raise ValueError(f"{s['id']} must start with 今日要闻")
        for g in s["groups"]:
            if not isinstance(g.get("h"), str) or not g.get("items"):
                raise ValueError(f"bad group in {s['id']}")
            for it in g["items"]:
                if not isinstance(it.get("t"), str) or not it["t"]:
                    raise ValueError("empty item")
                bad = [r for r in it.get("refs", []) if r not in headline_ids]
                if bad:
                    raise ValueError(f"unknown refs {bad}")


# -------------------------------------------------------------- assembly

TITLES = {"markets": "一、美国市场与宏观", "tech": "二、科技与 AI",
          "biotech": "三、生物医药", "world": "四、世界头条"}
SECTION_CHARTS = {"markets": ["usmoves"], "tech": ["tech_stocks", "tech_movers"],
                  "biotech": ["biotech_stocks", "biotech_movers"]}


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


CJK = r"\u3400-\u9fff\uff00-\uffef"


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
    for key in ("spx", "ndx", "sox", "xbi", "us10y", "brent", "gold"):
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
                       {"name": "纳斯达克", "values": [mn[c] for c in cats]}],
            }


def build_stock_chart(chart_id, rows):
    if len(rows) < 3:
        return None
    rows = sorted(rows, key=lambda r: r["change_pct"], reverse=True)
    return {"kind": "hbars", "title": STOCKS[chart_id][0],
            "desc": f"{rows[0]['date'][5:].replace('-', '/')} 美股交易日，按涨跌幅排列。",
            "rows": [{"label": f"{r['name']}（{r['symbol']}）", "value": r["change_pct"]} for r in rows],
            }


def build_movers_chart(section, movers):
    rows = (movers.get("gainers") or []) + (movers.get("losers") or [])
    if len(rows) < 4:
        return None
    rows = sorted({r["symbol"]: r for r in rows}.values(), key=lambda r: r["change_pct"], reverse=True)
    return {"kind": "hbars", "title": MOVER_TITLES[section] + "（涨跌幅前 5）",
            "desc": f"{rows[0]['date'][5:].replace('-', '/')} 美股交易日，市值 15 亿美元以上。",
            "rows": [{"label": f"{r['name']}（{r['symbol']}）", "value": r["change_pct"]} for r in rows],
            }


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


def assemble(facts, narrative, prev, now_iso, narrative_fresh):
    by_id = {h["id"]: h for h in facts["headlines"]}
    prev_charts = (prev or {}).get("charts", {})
    charts = {}
    if facts["markets"]:
        charts["usmoves"] = build_usmoves(facts["markets"])
    for cid, rows in facts.get("stock_moves", {}).items():
        charts[cid] = build_stock_chart(cid, rows)
    for section, m in (facts.get("movers") or {}).items():
        charts[f"{section}_movers"] = build_movers_chart(section, m)
    for cid in ("usmoves", *STOCKS, *(f"{k}_movers" for k in MOVER_SETS)):  # keep yesterday's if today's is missing
        if not charts.get(cid) and prev_charts.get(cid):
            charts[cid] = prev_charts[cid]
    charts = {k: v for k, v in charts.items() if v}

    sections, sources = [], []
    for s in narrative["sections"]:
        groups, seen = [], set()
        src_list = []
        for g in s["groups"]:
            items = []
            for it in g["items"]:
                items.append({"t": cn_punct(it["t"])})
                for r in resolve_src(it, by_id):
                    if r["u"] not in seen:
                        seen.add(r["u"])
                        src_list.append(r)
            groups.append({"h": g["h"], "items": items})
        sections.append({"id": s["id"], "title": TITLES[s["id"]], "groups": groups,
                         "charts": [c for c in SECTION_CHARTS.get(s["id"], []) if c in charts]})
        sources.append({"section": TITLES[s["id"]], "items": src_list})
    snapshot = (build_snapshot(facts["markets"], facts.get("usdcny") or []) if facts["markets"]
                else (prev or {}).get("snapshot", []))
    return {
        "date": facts["date"], "weekday": facts["weekday"],
        "generated_at": now_iso,
        "narrative_at": now_iso if narrative_fresh else (prev or {}).get("narrative_at", now_iso),
        "tldr": [cn_punct(t) for t in narrative["tldr"]], "snapshot": snapshot, "sections": sections,
        "charts": charts, "sources": narrative.get("_sources") or sources,
        "sources_note": "行情与股价数据来自 Yahoo Finance。新闻编译自上列英文媒体报道，仅依据标题整理。",
        "disclaimer": "本简报由 AI 根据公开新闻标题与行情数据自动编译，可能有误，仅供参考，不构成投资建议。",
    }


def prev_narrative(prev):
    """Re-express the previous brief as a narrative so a failed LLM call keeps yesterday's text."""
    return {"tldr": prev["tldr"], "_sources": prev.get("sources"),
            "sections": [{"id": s["id"], "groups": s["groups"]} for s in prev["sections"]]}


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
    try:
        movers = gather_movers()
    except Exception as e:
        log(f"movers failed: {e}")
        movers = {}
    facts = {"date": today, "weekday": "周" + "一二三四五六日"[now.weekday()],
             "markets": gather_markets(), "stock_moves": gather_stocks(), "movers": movers,
             "headlines": gather_headlines(), "usdcny": gather_usdcny()}
    log(f"facts: {len(facts['markets'])} markets, {len(facts['headlines'])} headlines")
    if args.facts_only:
        print(json.dumps(facts, ensure_ascii=False, indent=1))
        return 0

    fresh = True
    try:
        if args.narrative_file:
            narrative = json.loads(Path(args.narrative_file).read_text())
        elif os.environ.get("ANTHROPIC_API_KEY"):
            narrative = narrate(facts)
        else:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
    except Exception:
        log(f"narrative unavailable, keeping previous text:\n{traceback.format_exc()}")
        if not prev or [s["id"] for s in prev.get("sections", [])] != SECTION_IDS:
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
    log(f"wrote {OUT.relative_to(ROOT)} (narrative {'fresh' if fresh else 'STALE'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
