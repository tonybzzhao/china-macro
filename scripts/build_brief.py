#!/usr/bin/env python3
"""Build brief/brief.json, the data behind chatccp.com/brief.

Runs daily from .github/workflows/brief.yml. Three parts:

  1. Facts, gathered deterministically: market closes (Yahoo chart API),
     topical headlines (Google News RSS), China macro (data/history.json).
  2. Narrative (TL;DR, bullets, glossary), written in Simplified Chinese by
     Claude from those facts only. Needs ANTHROPIC_API_KEY. The model never
     writes URLs or market numbers: it cites headlines by id and the script
     resolves the links; tiles and charts come straight from the facts.
  3. Assembly into brief/brief.json.

Every part degrades on its own. If the market fetch fails the previous tiles
are kept; if the LLM call fails (or no key is set) the previous narrative is
kept and the page shows that the text is stale. The file is never left
half-written.

Usage:
  python scripts/build_brief.py                      # normal run
  python scripts/build_brief.py --facts-only         # print the facts, no LLM
  python scripts/build_brief.py --narrative-file f   # use f instead of the LLM
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

MARKETS = [  # key, yahoo symbol, label, unit/format
    ("sse", "000001.SS", "上证指数", "idx"),
    ("hsi", "^HSI", "恒生指数", "idx"),
    ("spx", "^GSPC", "标普 500", "idx"),
    ("ndx", "^IXIC", "纳斯达克", "idx"),
    ("dji", "^DJI", "道琼斯", "idx"),
    ("rut", "^RUT", "罗素 2000", "idx"),
    ("us10y", "^TNX", "美国 10 年期国债收益率", "pct"),
    ("brent", "BZ=F", "布伦特原油", "usd"),
    ("wti", "CL=F", "WTI 原油", "usd"),
    ("gold", "GC=F", "黄金", "usd"),
    ("dxy", "DX-Y.NYB", "美元指数", "idx"),
]

TOPICS = {  # Google News queries, English because the best sources are
    "fed_macro": "Federal Reserve OR inflation OR Treasury yields OR jobs report",
    "markets": "Wall Street stocks close S&P 500 Nasdaq",
    "oil_geo": "oil prices OR Iran OR Houthi OR Saudi OR Middle East",
    "ai_tech": "AI chips OR OpenAI OR Nvidia OR Microsoft OR Alphabet earnings",
    "china": "China economy OR China tariffs OR US-China trade OR PBOC OR yuan",
    "china_geo": "Taiwan OR South China Sea OR Xi Jinping OR Beijing",
}


def is_china_trading_day(d):
    """True if Beijing date d (YYYY-MM-DD) is an SSE/SZSE session, per data/history.json."""
    days = json.loads(HISTORY.read_text()).get("trading_days") or []
    return d in days if days else datetime.strptime(d, "%Y-%m-%d").weekday() < 5


def log(msg):
    print(f"[build_brief] {msg}", file=sys.stderr, flush=True)


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
            # Yahoo's daily bar for a session that just closed can still be null
            # (China/HK at Beijing dawn); the quote in meta already holds the close.
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


def gather_markets():
    out = {}
    for key, sym, label, kind in MARKETS:
        try:
            bars, partial = fetch_market(sym)
            done = bars[:-1] if partial else bars
            if len(done) < 2:
                raise ValueError("too few bars")
            out[key] = {"label": label, "kind": kind, "bars": done[-8:],
                        "last": done[-1], "prev": done[-2],
                        "intraday": bars[-1] if partial else None}
        except Exception as e:
            log(f"market {key} failed: {e}")
    return out


ZH_TOPICS = {  # Google News China edition, for what readers in China are waking up to
    "cn_markets": "A股 OR 港股 OR 沪指 OR 恒生指数 收盘",
    "cn_policy": "央行 OR 国务院 OR 人民币 OR 发改委 经济",
}


def gather_headlines(per_topic=10, max_age_h=36):
    items, seen, n = [], set(), 0
    now = datetime.now(timezone.utc)
    jobs = [(t, q, "hl=en-US&gl=US&ceid=US:en") for t, q in TOPICS.items()]
    jobs += [(t, q, "hl=zh-CN&gl=CN&ceid=CN:zh-Hans") for t, q in ZH_TOPICS.items()]
    for topic, q, edition in jobs:
        url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(q + " when:2d")
               + "&" + edition)
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


def gather_china():
    d = json.loads(HISTORY.read_text())
    out = {}
    for cat in d["categories"].values():
        for key, s in cat["series"].items():
            pts = s["data"]
            if not pts:
                continue
            out[key] = {"label": s["label"], "unit": s["unit"],
                        "latest": pts[-1], "previous": pts[-2] if len(pts) > 1 else None}
    today = (datetime.now(BJT)).strftime("%Y-%m-%d")
    upcoming = [r for r in d.get("upcoming_releases", []) if r["date"] >= today][:10]
    return {"series": out, "upcoming_releases": upcoming}


# ------------------------------------------------------------- narrative

SYSTEM = """你是一位面向金融入门读者的中文财经编辑,为网站撰写每日简报。
严格遵守:
1. 只使用 FACTS 里出现的事实和数字。不得编造数字、日期、公司动作;不确定就写「据报道」或「待确认」。
2. 新闻只给了标题,因此只陈述标题能支持的内容,不要推测细节。多个来源标题相互冲突时,指出口径不一。
3. 每条事实性要点用 refs 引用 FACTS.headlines 里的 id(1 至 2 个)。中国宏观数据、市场数据来自 FACTS 本身,不需要 refs。不要写任何网址。
4. 语言:简体中文,简洁要点,面向初学者,专业词汇首次出现要用括号简短解释。不用破折号。
5. 不构成投资建议,不给出买卖指令;「可操作的分析思路」只能是分析框架。
6. 市场数字(指数、收益率、油价)以 FACTS.markets 为准,并写明日期。
只输出一个 JSON 对象,不要任何其他文字。"""

SCHEMA = """{
 "tldr": ["3 条,每条一句话,最重要的事放最前面"],
 "sections": [
  {"id": "markets", "groups": [{"h": "今日要闻", "items": [{"t": "要点", "refs": ["h1"]}]}, {"h": "怎么看(新手版)", "items": []}, {"h": "长期趋势", "items": []}, {"h": "风险", "items": []}, {"h": "关注事项", "items": []}], "terms": [{"k": "词", "v": "解释"}]},
  {"id": "tech", "groups": ["同上五组"], "terms": []},
  {"id": "china", "groups": ["同上五组,今日要闻要用 FACTS.china 的最新值与前值"], "terms": []},
  {"id": "finance", "groups": ["同上五组,其中第二组 h 为「可操作的分析思路(不构成投资建议)」"], "terms": []}
 ]
}
每组 2 至 5 条要点,每条不超过 60 个汉字。sections 的 id 依次为 markets、tech、china、finance,标题由程序添加。"""


def narrate(facts):
    import anthropic  # imported late so --facts-only works without the SDK
    client = anthropic.Anthropic()
    prompt = (f"今天是 {facts['date']}{facts['weekday']}(北京时间)早晨。读者在中国,每个交易日早晨阅读:"
              "请以「昨夜美股与全球市场」「今日 A 股、港股开盘前需要关注什么」的视角写作,"
              "并留意对人民币、A 股、港股的可能影响(只写 FACTS 能支持的内容)。\n\nFACTS:\n"
              + json.dumps(facts, ensure_ascii=False)
              + f"\n\n按此 JSON 结构输出:\n{SCHEMA}")
    last_err = None
    for attempt in range(2):
        msg = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM,
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
    if ids != ["markets", "tech", "china", "finance"]:
        raise ValueError(f"bad section ids {ids}")
    for s in obj["sections"]:
        if not s.get("groups"):
            raise ValueError(f"{s['id']} has no groups")
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

TITLES = {"markets": "一、市场与宏观", "tech": "二、AI 与科技",
          "china": "三、中国与地缘政治", "finance": "四、金融与投资"}
SECTION_CHARTS = {"markets": ["usmoves"], "china": ["pmi", "prices", "trade"]}

CHART_DEFS = {
    "pmi": {"title": "中国 PMI(近 24 个月)", "desc": "红色虚线为荣枯线 50,线在 50 上方表示扩张。",
            "series": [{"name": "官方制造业", "cat": "growth", "key": "official_manufacturing_pmi"},
                       {"name": "官方非制造业", "cat": "growth", "key": "official_non_manufacturing_pmi"},
                       {"name": "财新制造业", "cat": "growth", "key": "caixin_manufacturing_pmi"}],
            "threshold": 50, "src": ["国家统计局、财新(数据来自本站)", "../"]},
    "prices": {"title": "中国 CPI 与 PPI 同比(%,近 24 个月)", "desc": "CPI 衡量居民消费品价格,PPI 衡量工厂出厂价。",
               "series": [{"name": "CPI", "cat": "prices_credit", "key": "cpi"},
                          {"name": "PPI", "cat": "prices_credit", "key": "ppi"}],
               "threshold": 0, "src": ["国家统计局(数据来自本站)", "../"]},
    "trade": {"title": "中国进出口同比(%,美元计价,近 24 个月)", "desc": "出口与进口的同比增速。",
              "series": [{"name": "出口", "cat": "external", "key": "exports_yoy"},
                         {"name": "进口", "cat": "external", "key": "imports_yoy"}],
              "threshold": 0, "src": ["海关总署(数据来自本站)", "../"]},
}


def pct(a, b):
    return (a / b - 1) * 100


def build_snapshot(markets, china):
    tiles = []
    for key in ("spx", "ndx", "sse", "hsi", "us10y", "brent", "gold"):
        m = markets.get(key)
        if not m:
            continue
        cur = m["intraday"] or m["last"]
        base = m["last"] if m["intraday"] else m["prev"]
        d = cur["date"][5:].replace("-", "/")
        if m["kind"] == "pct":
            chg = f"较前收 {cur['close'] - base['close']:+.2f} 个百分点"
            val = f"{cur['close']:.2f}%"
        else:
            chg = f"较前收 {pct(cur['close'], base['close']):+.1f}%"
            val = f"{cur['close']:,.2f}"
        tiles.append({"label": m["label"], "value": val,
                      "note": f"{d} {'最新(未收盘)' if m['intraday'] else '收盘'},{chg}"})
    cny = china["series"].get("usdcny")
    if cny:
        tiles.append({"label": "美元兑人民币", "value": f"{cny['latest']['value']:.4f}",
                      "note": f"{cny['latest']['date'][5:].replace('-', '/')}(ECB 参考汇率)"})
    return tiles


def build_usmoves(markets):
    spx, ndx = markets.get("spx"), markets.get("ndx")
    if not spx or not ndx:
        return None
    dates = [b["date"] for b in spx["bars"]]
    common = [d for d in dates if d in {b["date"] for b in ndx["bars"]}][-6:]

    def moves(m):
        close = {b["date"]: b["close"] for b in m["bars"]}
        ordered = [b["date"] for b in m["bars"]]
        return {d: round(pct(close[d], close[ordered[ordered.index(d) - 1]]), 2)
                for d in common if ordered.index(d) > 0}
    ms, mn = moves(spx), moves(ndx)
    cats = [d for d in common if d in ms and d in mn][-5:]
    return {"title": "美股单日涨跌幅(%)", "desc": f"标普 500 与纳斯达克,最近 {len(cats)} 个交易日。",
            "categories": [c[5:].replace("-", "/") for c in cats],
            "series": [{"name": "标普 500", "values": [ms[c] for c in cats]},
                       {"name": "纳斯达克", "values": [mn[c] for c in cats]}],
            "src": ["Yahoo Finance", "https://finance.yahoo.com/quote/%5EGSPC/"]}


def resolve_src(item, by_id):
    if item.get("src"):
        return item["src"]
    out = []
    for r in item.get("refs", [])[:2]:
        h = by_id.get(r)
        if h and h["url"]:
            out.append({"n": h["source"] or "来源", "u": h["url"]})
    return out


def assemble(facts, narrative, prev, now_iso, narrative_fresh):
    by_id = {h["id"]: h for h in facts["headlines"]}
    sections = []
    for s in narrative["sections"]:
        groups = []
        for g in s["groups"]:
            items = []
            for it in g["items"]:
                row = {"t": it["t"]}
                srcs = resolve_src(it, by_id)
                if srcs:
                    row["src"] = srcs
                items.append(row)
            groups.append({"h": g["h"], "items": items})
        sections.append({"id": s["id"], "title": TITLES[s["id"]], "groups": groups,
                         "charts": SECTION_CHARTS.get(s["id"], []), "terms": s.get("terms", [])})
    charts = dict(CHART_DEFS)
    usm = build_usmoves(facts["markets"]) if facts["markets"] else None
    charts["usmoves"] = usm or (prev or {}).get("charts", {}).get("usmoves")
    if not charts["usmoves"]:
        del charts["usmoves"]
        for s in sections:
            s["charts"] = [c for c in s["charts"] if c != "usmoves"]
    snapshot = build_snapshot(facts["markets"], facts["china"]) if facts["markets"] else (prev or {}).get("snapshot", [])
    return {
        "date": facts["date"], "weekday": facts["weekday"],
        "generated_at": now_iso,
        "narrative_at": now_iso if narrative_fresh else (prev or {}).get("narrative_at", now_iso),
        "tldr": narrative["tldr"], "snapshot": snapshot, "sections": sections, "charts": charts,
        "sources_note": "每条要闻后附来源链接;新闻只依据标题整理,口径冲突处已在正文标注。市场数字来自 Yahoo Finance,中国宏观数据来自本站自动更新管线。",
        "disclaimer": "本简报由 AI 根据公开新闻标题与数据自动整理,可能有误,仅供学习参考,不构成任何投资建议。",
    }


def prev_narrative(prev):
    """Re-express the previous brief as a narrative so a failed LLM call keeps yesterday's text."""
    return {"tldr": prev["tldr"],
            "sections": [{"id": s["id"], "groups": s["groups"], "terms": s.get("terms", [])}
                         for s in prev["sections"]]}


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
    facts = {"date": now.strftime("%Y-%m-%d"), "weekday": "周" + "一二三四五六日"[now.weekday()],
             "markets": gather_markets(), "headlines": gather_headlines(), "china": gather_china()}
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
        if not prev:
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
