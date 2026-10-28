import json
import unittest
from unittest.mock import patch

from scripts import build_brief as bb

IDS = {"h1", "h2"}


def narrative(**over):
    sec = lambda i, sector: {"id": i, "title": "板块", "sector": sector,
                             "groups": [{"h": "今日要闻", "items": [{"t": "x", "refs": ["h1"]}]}]}
    n = {"theme": "主题", "tldr": ["a", "b", "c"],
         "sections": [sec("markets", None), sec("s1", "Technology"), sec("s2", "Energy")],
         "trends": [{"t": "趋势", "refs": ["h1"]}],
         "calendar": [{"when": "10 月 29 日 20:30（美东 8:30）", "event": "GDP", "watch": "增长"}]}
    n.update(over)
    return n


def bars(closes):
    return [{"date": f"2026-10-{20 + i}", "close": c} for i, c in enumerate(closes)]


class ValidateNarrative(unittest.TestCase):
    def test_accepts_well_formed(self):
        bb.validate_narrative(narrative(), IDS)

    def test_rejects_unknown_headline_ref(self):
        n = narrative()
        n["sections"][0]["groups"][0]["items"][0]["refs"] = ["h99"]
        with self.assertRaisesRegex(ValueError, "unknown refs"):
            bb.validate_narrative(n, IDS)

    def test_first_group_must_be_todays_news(self):
        n = narrative()
        n["sections"][0]["groups"][0]["h"] = "接下来关注"
        with self.assertRaisesRegex(ValueError, "今日要闻"):
            bb.validate_narrative(n, IDS)

    def test_rejects_wrong_section_order_and_empty_tldr(self):
        n = narrative()
        n["sections"].reverse()
        with self.assertRaises(ValueError):
            bb.validate_narrative(n, IDS)
        with self.assertRaises(ValueError):
            bb.validate_narrative(narrative(tldr=[]), IDS)

    def test_sector_must_be_a_known_yahoo_sector_or_null(self):
        n = narrative()
        n["sections"][1]["sector"] = "Crypto"
        with self.assertRaisesRegex(ValueError, "bad sector"):
            bb.validate_narrative(n, IDS)

    def test_trend_and_calendar_refs_are_checked(self):
        with self.assertRaisesRegex(ValueError, "unknown refs"):
            bb.validate_narrative(narrative(trends=[{"t": "x", "refs": ["h99"]}]), IDS)
        with self.assertRaises(ValueError):
            bb.validate_narrative(narrative(calendar=[{"when": "x", "event": "y"}]), IDS)


class Assembly(unittest.TestCase):
    def facts(self, markets=None):
        return {"date": "2026-10-29", "weekday": "周四", "markets": markets or {},
                "headlines": [{"id": "h1", "source": "Reuters", "title": "T", "url": "https://x.test/a"}],
                "usdcny": None}

    def test_sources_are_collected_at_the_end_and_items_stay_clean(self):
        n = narrative()
        n["sections"][0]["groups"][0]["items"].append(
            {"t": "y", "src": [{"n": "FT", "u": "https://ft.test"}]})
        prev = {"charts": {"usmoves": {"title": "kept"}}, "snapshot": [{"label": "old"}]}
        facts = self.facts()
        facts["headlines"][0]["title"] = "Fed holds"
        out = bb.assemble(facts, n, prev, "2026-10-29T06:00:00+08:00", True)
        items = out["sections"][0]["groups"][0]["items"]
        self.assertTrue(all(set(i) == {"t"} for i in items))  # no inline sources
        srcs = out["sources"]  # one flat list for the whole page, de-duplicated
        self.assertEqual(srcs[0], {"n": "Reuters（英文）", "u": "https://x.test/a", "t": "Fed holds"})
        self.assertEqual(srcs[1]["n"], "FT")
        self.assertEqual(len(srcs), 2)
        self.assertEqual(out["sections"][0]["title"], "一、板块")
        # market fetch failed: previous tiles and US chart are kept
        self.assertEqual(out["snapshot"], [{"label": "old"}])
        self.assertEqual(out["charts"]["usmoves"]["title"], "kept")

    def test_full_width_punctuation_but_numbers_untouched(self):
        self.assertEqual(bb.cn_punct("美股涨 0.28%, 报 7,743.98 点;英伟达(Nvidia)下跌."),
                         "美股涨 0.28%，报 7,743.98 点；英伟达（Nvidia）下跌。")
        self.assertEqual(bb.cn_punct("美东 10/28 下午 2:00 公布"), "美东 10/28 下午 2:00 公布")
        self.assertEqual(bb.cn_punct("Nvidia (NVDA) rose 2.5%, a record."), "Nvidia (NVDA) rose 2.5%, a record.")

    def test_tile_direction_and_series(self):
        t = bb.tile("spx", "标普 500", "idx", 110.0, 100.0, [["10-27", 100.0], ["10-28", 110.0]], "n")
        self.assertEqual((t["dir"], t["change"]), ("up", "+10.00%"))
        y = bb.tile("us10y", "10Y", "pct", 4.9, 5.0, [["a", 5.0], ["b", 4.9]], "n")
        self.assertEqual((y["dir"], y["change"]), ("down", "-0.10 个百分点"))

    def test_stale_narrative_keeps_old_timestamp(self):
        prev = {"narrative_at": "2026-10-28T06:00:00+08:00", "charts": {"usmoves": {"title": "k"}}, "snapshot": []}
        out = bb.assemble(self.facts(), narrative(), prev, "2026-10-29T06:00:00+08:00", False)
        self.assertEqual(out["narrative_at"], "2026-10-28T06:00:00+08:00")

    def test_chinese_outlets_are_not_marked_english(self):
        by_id = {"h1": {"source": "新华社", "url": "https://x.test"}}
        by_id["h1"]["title"] = "t"
        self.assertEqual(bb.resolve_src({"refs": ["h1"]}, by_id)[0]["n"], "新华社")

    def test_movers_chart_merges_gainers_and_losers_sorted(self):
        mk = lambda sym, v: {"symbol": sym, "name": sym, "date": "2026-10-28", "change_pct": v}
        chart = bb.build_movers_chart("科技板块异动股", {"gainers": [mk("A", 5), mk("B", 2)],
                                                       "losers": [mk("C", -7), mk("D", -1)]})
        self.assertEqual([r["value"] for r in chart["rows"]], [5, 2, -1, -7])
        self.assertIsNone(bb.build_movers_chart("x", {"gainers": [mk("A", 5)], "losers": []}))

    def test_sector_chart_needs_enough_sectors(self):
        rows = [{"etf": "XLK", "name": "科技", "date": "2026-10-28", "change_pct": 1.0}] * 4
        self.assertIsNone(bb.build_sector_chart(rows))

    def test_usmoves_percent_changes(self):
        m = {"spx": {"bars": bars([100, 110, 99])}, "ndx": {"bars": bars([200, 200, 220])}}
        chart = bb.build_usmoves(m)
        self.assertEqual(chart["series"][0]["values"], [10.0, -10.0])
        self.assertEqual(chart["series"][1]["values"], [0.0, 10.0])


class TradingDayGate(unittest.TestCase):
    def test_uses_exchange_calendar_not_weekdays(self):
        cal = json.dumps({"trading_days": ["2026-10-29"]})
        with patch.object(bb.HISTORY.__class__, "read_text", return_value=cal):
            self.assertTrue(bb.is_china_trading_day("2026-10-29"))
            self.assertFalse(bb.is_china_trading_day("2026-10-30"))  # a weekday, but not listed


if __name__ == "__main__":
    unittest.main()
