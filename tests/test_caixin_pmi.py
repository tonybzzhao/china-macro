import copy
import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import requests
from scripts import caixin_pmi as pmi


NOW = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)


def response(payload=None, text="", error=None):
    r = Mock(text=text)
    r.raise_for_status.side_effect = error
    r.json.return_value = payload
    return r


def headline(value="52.1", month="September", year="2026"):
    return response(text=f'<h2 id="description">The RatingDog China Manufacturing PMI '
                         f'increased to {value} in {month} {year} from 51.5 in August.</h2>')


def primary(month=9, value=52.1):
    stamp = datetime(2026, month, 1, tzinfo=timezone.utc).timestamp() * 1000
    return response({"data": [[0.6, value, stamp]]})


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.series = {"data": [{"date": "2026-08", "value": 51.5}]}
        self.data = {"categories": {"growth": {"series": {pmi.KEY: self.series}}}}
        self.sleep = Mock()

    def refresh(self, get):
        return pmi.refresh(self.data, NOW, get=get, sleep=self.sleep)

    def test_malformed_json_falls_back_and_recovers_next_run(self):
        bad = response(text="<html>404</html>")
        bad.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        get = Mock(side_effect=[bad, bad, bad, headline()])
        self.assertTrue(self.refresh(get))
        self.assertEqual(self.series["data"][-1], {"date": "2026-09", "value": 52.1})
        self.assertEqual(self.series["refresh"]["source"], pmi.TE_URL)
        self.assertEqual(len(self.series["refresh"]["errors"]), 3)
        self.assertTrue(self.refresh(Mock(return_value=primary())))
        self.assertEqual(self.series["refresh"]["source"], pmi.CAIXIN_URL)
        self.assertEqual(self.series["refresh"]["errors"], [])
        self.assertEqual(len(self.series["data"]), 2)

    def test_transient_failure_retries_primary(self):
        get = Mock(side_effect=[requests.Timeout(), primary()])
        self.assertTrue(self.refresh(get))
        self.assertEqual(get.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))

    def test_outage_retains_history_and_success_timestamp_then_recovers(self):
        self.assertTrue(self.refresh(Mock(return_value=primary())))
        before = copy.deepcopy(self.series)
        get = Mock(side_effect=requests.Timeout("offline"))
        self.assertFalse(self.refresh(get))
        self.assertEqual(get.call_count, 6)
        self.assertEqual(self.series["data"], before["data"])
        self.assertEqual(self.series["refresh"]["status"], "retained")
        self.assertEqual(self.series["refresh"]["last_success_at"], before["refresh"]["last_success_at"])
        self.assertTrue(self.refresh(Mock(return_value=primary())))

    def test_stale_primary_uses_fresh_fallback(self):
        get = Mock(side_effect=[primary(8, 51.5)] * 3 + [headline()])
        self.assertTrue(self.refresh(get))
        self.assertEqual(self.series["refresh"]["source"], pmi.TE_URL)

    def test_stale_fallback_is_not_a_success(self):
        before = copy.deepcopy(self.series["data"])
        get = Mock(side_effect=[primary(8, 51.5)] * 3 + [headline("51.5", "August")] * 3)
        self.assertFalse(self.refresh(get))
        self.assertEqual(self.series["data"], before)
        self.assertEqual(self.series["refresh"]["expected_month"], "2026-09")

    def test_conflict_never_rewrites_verified_history(self):
        r = primary()
        r.json.return_value["data"].append([0, 49, datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000])
        get = Mock(side_effect=[r] * 3 + [headline()])
        self.assertTrue(self.refresh(get))
        self.assertEqual(self.series["data"][0]["value"], 51.5)

    def test_invalid_payloads_fail_closed(self):
        for payload in [{}, {"data": []}, {"data": {}}, {"data": [[1, 2]]},
                        {"data": [[1, None, 0]]}, {"data": [[1, float('nan'), 0]]}]:
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    pmi.fetch_caixin(Mock(return_value=response(payload)), NOW.date())

    def test_wrong_series_and_forecasts_rejected(self):
        for text in ['<html>blocked</html>',
                     '<h2 id="description">The NBS China Manufacturing PMI increased to 50.1 in September 2026.</h2>',
                     '<h2 id="description">The RatingDog China Services PMI increased to 51.6 in September 2026.</h2>',
                     '<h2 id="description">The RatingDog China Manufacturing PMI is expected to rise to 53 in November 2026.</h2>']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                pmi.fetch_trading_economics(Mock(return_value=response(text=text)), NOW.date())

    def test_http_error_checked_before_body(self):
        r = response(error=requests.HTTPError("503"))
        with self.assertRaises(requests.HTTPError):
            pmi.fetch_caixin(Mock(return_value=r), NOW.date())
        r.json.assert_not_called()

    def test_shanghai_month_not_utc_or_release_month(self):
        stamp = datetime(2026, 8, 31, 16, tzinfo=timezone.utc).timestamp() * 1000
        r = response({"data": [[0.6, 52.1, stamp]]})
        self.assertEqual(pmi.fetch_caixin(Mock(return_value=r), NOW.date())[0]["date"], "2026-09")

    def test_future_and_invalid_values(self):
        for point in [{"date": "2026-13", "value": 52}, {"date": "2027-01", "value": 52},
                      {"date": "2026-09", "value": float("inf")}, {"date": "2026-09", "value": 101}]:
            with self.subTest(point=point), self.assertRaises(ValueError):
                pmi.validate([point], NOW.date())

    def test_deadline_year_rollover(self):
        self.assertEqual(pmi.expected_month(date(2027, 1, 3)), "2026-11")
        self.assertEqual(pmi.expected_month(date(2027, 1, 7)), "2026-12")

    def test_main_saves_other_updates_before_failure_exit(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import update_data as update
        data = copy.deepcopy(self.data)
        data["meta"] = {}
        def failed(d):
            d["categories"]["growth"]["series"][pmi.KEY]["refresh"] = {"status": "retained"}
            return False
        with patch.multiple(update, load_data=Mock(return_value=data), save_data=Mock(),
                            refresh_caixin_pmi=failed, FETCHERS=[],
                            fetch_trading_calendar=Mock(return_value=["2026-10-21"]),
                            fetch_lpr=Mock(return_value=([], [])),
                            fetch_money_supply=Mock(return_value=([], [], [])),
                            fetch_rrr=Mock(return_value=[]), fetch_pmi_official=Mock(return_value=([], [])),
                            fetch_trade=Mock(return_value=([], [], [])), fetch_news=Mock(return_value=[]),
                            fetch_upcoming_releases=Mock(return_value=[])):
            self.assertEqual(update.main(), 1)
            self.assertEqual(data["trading_days"], ["2026-10-21"])
            update.save_data.assert_called_once_with(data)


if __name__ == "__main__":
    unittest.main()
