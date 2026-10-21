"""Bounded retrieval and conservative refresh of the Caixin/RatingDog PMI.

The fallback reads only the current manufacturing headline, never forecasts,
services, NBS PMI, or calendar release dates. Existing observations are immutable.
"""
import calendar
import math
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import requests

CAIXIN_URL = "https://yun.ccxe.com.cn/api/index/pro/cxIndexTrendInfo"
TE_URL = "https://tradingeconomics.com/china/manufacturing-pmi"
KEY = "caixin_manufacturing_pmi"


def validate(points, today):
    if not points:
        raise ValueError("empty PMI response")
    seen = {}
    for p in points:
        month = p["date"]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ValueError("invalid PMI reference month")
        if month > today.strftime("%Y-%m"):
            raise ValueError("future PMI reference month")
        value = float(p["value"])
        if not math.isfinite(value) or not 0 < value < 100:
            raise ValueError("invalid PMI value")
        if month in seen and seen[month] != value:
            raise ValueError("conflicting PMI rows")
        seen[month] = value
    return [{"date": m, "value": v} for m, v in sorted(seen.items())]


def fetch_caixin(get, today):
    response = get(CAIXIN_URL, params={"type": "man"}, timeout=(5, 20))
    response.raise_for_status()
    rows = response.json()["data"]
    if not isinstance(rows, list):
        raise ValueError("invalid Caixin data schema")
    points = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("invalid Caixin row schema")
        # AKShare index_cx.py: [change, manufacturing PMI, epoch ms].
        stamp = datetime.fromtimestamp(float(row[2]) / 1000, timezone.utc)
        month = (stamp + timedelta(hours=8)).strftime("%Y-%m")
        points.append({"date": month, "value": row[1]})
    return validate(points, today)


def fetch_trading_economics(get, today):
    response = get(TE_URL, timeout=(5, 20), headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    # Restrict parsing to the current observed headline, excluding news archives
    # and forward-looking text elsewhere in the page. Fail closed on redesign.
    match = re.search(r'<h2\b[^>]*\bid=["\']description["\'][^>]*>(.*?)</h2>',
                      response.text, re.S | re.I)
    if not match:
        raise ValueError("Trading Economics headline missing")
    text = unescape(re.sub(r"<[^>]+>", " ", match[1]))
    text = " ".join(text.split())
    match = re.match(
        r"The (?:RatingDog China|Caixin China|Caixin) Manufacturing PMI "
        r"(?:rose|fell|increased|decreased|declined|climbed|dropped|edged|was|remained|stood|held|ticked)\b"
        r"(?![^.]*\b(?:expected|forecast|projected)\b)"
        r"[^.]*?\b(?:to|at) (\d+(?:\.\d+)?) in ([A-Za-z]+) (\d{4})\b", text)
    if not match or match[2] not in list(calendar.month_name):
        raise ValueError("unrecognized observed manufacturing headline")
    month = list(calendar.month_name).index(match[2])
    return validate([{"date": f"{match[3]}-{month:02d}", "value": match[1]}], today)


def expected_month(today):
    """Conservative deadline, not a release calendar: previous month by day 7.

    Early-month holidays/weekends are allowed. An early release (e.g. September
    on September 30) is accepted, but this rule cannot detect it until day 7.
    """
    last = today.replace(day=1) - timedelta(days=1)
    if today.day < 7:
        last = last.replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


def refresh(data, now=None, get=None, sleep=time.sleep):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    get = get or requests.get
    series = data["categories"]["growth"]["series"][KEY]
    prior = series.get("refresh", {})
    existing = {p["date"]: p["value"] for p in series["data"]}
    expected = max(expected_month(today), max(existing, default=""))
    status = {
        "attempted_at": now.isoformat(), "status": "retained",
        "expected_month": expected, "latest_observation": max(existing, default=None),
        "last_success_at": prior.get("last_success_at"),
        "last_success_source": prior.get("last_success_source"), "errors": [],
    }
    for source, fetch in [(CAIXIN_URL, fetch_caixin), (TE_URL, fetch_trading_economics)]:
        for attempt in range(3):
            try:
                points = fetch(get, today)
                if points[-1]["date"] < expected:
                    raise ValueError(f"stale source: {points[-1]['date']} < {expected}")
                for p in points:
                    if p["date"] in existing and existing[p["date"]] != p["value"]:
                        raise ValueError(f"conflict with retained observation: {p['date']}")
                merged = dict(existing)
                merged.update({p["date"]: p["value"] for p in points})
                series["data"] = [{"date": m, "value": v} for m, v in sorted(merged.items())]
                status.update(status="refreshed", source=source,
                              latest_observation=points[-1]["date"],
                              last_success_at=now.isoformat(), last_success_source=source)
                series["refresh"] = status
                return True
            except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
                status["errors"].append(f"{source} attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                if attempt < 2:
                    sleep(2 ** attempt)
    series["refresh"] = status
    return False
