import json
import unittest
from unittest.mock import patch

from scripts import build_brief as bb

IDS = {"h1", "h2"}


def narrative(**over):
    n = {"tldr": ["a", "b", "c"],
         "sections": [{"id": i, "groups": [{"h": "今日要闻", "items": [{"t": "x", "refs": ["h1"]}]}]}
                      for i in ("markets", "tech", "biotech", "world")]}
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

    def test_rejects_wrong_section_order_and_empty_tldr(self):
        n = narrative()
        n["sections"].reverse()
        with self.assertRaises(ValueError):
            bb.validate_narrative(n, IDS)
        with self.assertRaises(ValueError):
            bb.validate_narrative(narrative(tldr=[]), IDS)


class Assembly(unittest.TestCase):
    def facts(self, markets=None):
        return {"date": "2026-10-29", "weekday": "周四", "markets": markets or {},
                "headlines": [{"id": "h1", "source": "Reuters", "url": "https://x.test/a"}],
                "usdcny": None}

    def test_refs_become_links_and_direct_src_is_kept(self):
        n = narrative()
        n["sections"][0]["groups"][0]["items"].append(
            {"t": "y", "src": [{"n": "FT", "u": "https://ft.test"}]})
        prev = {"charts": {"usmoves": {"title": "kept"}}, "snapshot": [{"label": "old"}]}
        out = bb.assemble(self.facts(), n, prev, "2026-10-29T06:00:00+08:00", True)
        items = out["sections"][0]["groups"][0]["items"]
        self.assertEqual(items[0]["src"], [{"n": "Reuters(英文)", "u": "https://x.test/a"}])
        self.assertEqual(items[1]["src"][0]["n"], "FT")
        # market fetch failed: previous tiles and US chart are kept
        self.assertEqual(out["snapshot"], [{"label": "old"}])
        self.assertEqual(out["charts"]["usmoves"]["title"], "kept")

    def test_stale_narrative_keeps_old_timestamp(self):
        prev = {"narrative_at": "2026-10-28T06:00:00+08:00", "charts": {"usmoves": {"title": "k"}}, "snapshot": []}
        out = bb.assemble(self.facts(), narrative(), prev, "2026-10-29T06:00:00+08:00", False)
        self.assertEqual(out["narrative_at"], "2026-10-28T06:00:00+08:00")

    def test_chinese_outlets_are_not_marked_english(self):
        by_id = {"h1": {"source": "新华社", "url": "https://x.test"}}
        self.assertEqual(bb.resolve_src({"refs": ["h1"]}, by_id)[0]["n"], "新华社")

    def test_stock_chart_sorts_high_to_low_and_needs_enough_rows(self):
        rows = [{"symbol": "A", "name": "甲", "date": "2026-10-28", "change_pct": v} for v in (-1.0, 2.0, 0.5)]
        chart = bb.build_stock_chart("tech_stocks", rows)
        self.assertEqual([r["value"] for r in chart["rows"]], [2.0, 0.5, -1.0])
        self.assertIsNone(bb.build_stock_chart("tech_stocks", rows[:2]))

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
