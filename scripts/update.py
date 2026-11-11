#!/usr/bin/env python3
"""Fetch latest daily series from FRED and quarterly series from SEC EDGAR,
then update the inline DATA block in index.html if anything changed."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "index.html"

DAILY_START = "2025-11-01"
MAX_DAILY_POINTS = 520
QUARTERLY_START = "2024-10-01"
RPO_START = "2025-01-01"
MAX_QUARTERLY_POINTS = 12

SEC_UA = os.environ.get("SEC_USER_AGENT", "Forecaster 1953531014@qq.com")
FRED_SERIES = {
    "dgs": "DGS10",
    "brent": "DCOILBRENTEU",
    "oas": "BAMLC0A0CM",
}

MARKER_RE = re.compile(
    r"(/\* DATA:START \*/\s*const DATA = )(\{.*?\})(;\s*/\* DATA:END \*/)",
    re.S,
)


def curl_text(url: str, user_agent: str | None = None, timeout: int = 30) -> str:
    cmd = [
        "curl",
        "-fsSL",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--max-time",
        str(timeout),
    ]
    if user_agent:
        cmd.extend(["-H", f"User-Agent: {user_agent}", "--compressed"])
    cmd.append(url)
    out = subprocess.check_output(cmd)
    return out.decode("utf-8", errors="ignore")


def load_current_data(html_text: str) -> dict:
    m = MARKER_RE.search(html_text)
    if not m:
        raise RuntimeError("Could not find /* DATA:START */ ... /* DATA:END */ in index.html")
    return json.loads(m.group(2))


def merge_points(existing: list, fresh: list, max_len: int) -> list:
    by_date = {row[0]: row[1] for row in existing}
    for d, v in fresh:
        by_date[d] = v
    merged = [[d, by_date[d]] for d in sorted(by_date)]
    return merged[-max_len:]


def fetch_fred_series(series_id: str) -> list[list]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={DAILY_START}"
    text = curl_text(url)
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise RuntimeError(f"Unexpected CSV header for {series_id}: {header}")
    for row in reader:
        if len(row) < 2:
            continue
        dt, raw = row[0].strip(), row[1].strip()
        if not raw or raw == "." or dt < DAILY_START:
            continue
        val = round(float(raw), 4)
        rows.append([dt, int(val) if val.is_integer() and series_id == "NONE" else val])
    if not rows:
        raise RuntimeError(f"No rows returned for FRED series {series_id}")
    return rows


def fetch_sec_json(url: str) -> dict:
    time.sleep(0.15)
    return json.loads(curl_text(url, user_agent=SEC_UA, timeout=45))


def fetch_xbrl_rpo_100m(cik: str) -> list[list]:
    """Extract us-gaap:RevenueRemainingPerformanceObligation in 100M USD (亿美元)."""
    facts = fetch_sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    units = (
        facts.get("facts", {})
        .get("us-gaap", {})
        .get("RevenueRemainingPerformanceObligation", {})
        .get("units", {})
        .get("USD", [])
    )
    by_end: dict[str, tuple[str, float]] = {}
    for item in units:
        form = item.get("form")
        end = item.get("end", "")
        if form not in ("10-Q", "10-K") or end < RPO_START:
            continue
        if item.get("start"):
            continue
        filed = item.get("filed", "")
        val_100m = round(item["val"] / 1e8, 1)
        if val_100m.is_integer():
            val_100m = int(val_100m)
        if end not in by_end or filed >= by_end[end][0]:
            by_end[end] = (filed, val_100m)
    return [[end, by_end[end][1]] for end in sorted(by_end)]


def fetch_nvda_dso() -> list[list]:
    """Compute NVIDIA quarterly DSO = AccountsReceivable / QuarterlyRevenue * QuarterDays."""
    facts = fetch_sec_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json")
    gaap = facts["facts"]["us-gaap"]

    ar_by_end: dict[str, dict] = {}
    for u in gaap["AccountsReceivableNetCurrent"]["units"]["USD"]:
        if u.get("form") in ("10-Q", "10-K") and u.get("end", "") >= QUARTERLY_START:
            end = u["end"]
            if end not in ar_by_end or u.get("filed", "") >= ar_by_end[end].get("filed", ""):
                ar_by_end[end] = u

    rev_tag = "Revenues" if "Revenues" in gaap else "RevenueFromContractWithCustomerExcludingAssessedTax"
    rev_by_span: dict[tuple[str, str], dict] = {}
    for u in gaap[rev_tag]["units"]["USD"]:
        if u.get("form") in ("10-Q", "10-K") and u.get("start") and u.get("end", "") >= QUARTERLY_START:
            key = (u["start"], u["end"])
            if key not in rev_by_span or u.get("filed", "") >= rev_by_span[key].get("filed", ""):
                rev_by_span[key] = u

    q_rev: dict[str, tuple[float, int]] = {}
    for (start, end), u in rev_by_span.items():
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        if 80 <= days <= 100:
            q_rev[end] = (u["val"], days)

    for (s_fy, e_fy), u_fy in rev_by_span.items():
        fy_days = (date.fromisoformat(e_fy) - date.fromisoformat(s_fy)).days + 1
        if 350 <= fy_days <= 375 and e_fy not in q_rev:
            for (s_9m, e_9m), u_9m in rev_by_span.items():
                d_9m = (date.fromisoformat(e_9m) - date.fromisoformat(s_9m)).days + 1
                if s_9m == s_fy and 260 <= d_9m <= 285:
                    q4_days = (date.fromisoformat(e_fy) - date.fromisoformat(e_9m)).days
                    q_rev[e_fy] = (u_fy["val"] - u_9m["val"], q4_days)

    points = []
    for end in sorted(ar_by_end):
        if end in q_rev:
            rev, days = q_rev[end]
            if rev > 0:
                dso = round(ar_by_end[end]["val"] / rev * days, 1)
                points.append([end, dso])
    return points


def recent_filings(cik: str, start_date: str = RPO_START) -> list[tuple[str, str]]:
    sub = fetch_sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    rec = sub["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        rdate = rec["reportDate"][i]
        if form in ("10-Q", "10-K") and rdate >= start_date:
            acc = rec["accessionNumber"][i].replace("-", "")
            doc = rec["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
            out.append((rdate, url))
    return sorted(out)


def clean_filing_text(url: str) -> str:
    time.sleep(0.15)
    raw = curl_text(url, user_agent=SEC_UA, timeout=60)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt)


def parse_msft_commercial_rpo_100m(txt: str) -> int | float | None:
    patterns = [
        r"remaining performance obligations related to the commercial portion of revenue was \$\s*([\d,.]+)\s*billion",
        r"of which \$\s*([\d,.]+)\s*billion is related to the commercial portion",
        r"Commercial remaining performance obligation increased \d+%\s*to \$\s*([\d,.]+)\s*billion",
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.I)
        if m:
            val = round(float(m.group(1).replace(",", "")) * 10, 1)
            return int(val) if val.is_integer() else val
    return None


def parse_amzn_commitments_100m(txt: str) -> int | float | None:
    pat = (
        r"exceed one year,\s*those commitments(?:\s+not yet recognized)?\s+"
        r"were(?:\s+estimated to be)?\s+approximately \$\s*([\d,.]+)\s*billion"
    )
    m = re.search(pat, txt, re.I)
    if m:
        val = round(float(m.group(1).replace(",", "")) * 10, 1)
        return int(val) if val.is_integer() else val
    return None


def fetch_filing_text_series(cik: str, existing: list, parser, label: str) -> list[list]:
    known_dates = {row[0] for row in existing}
    fresh = []
    for rdate, url in recent_filings(cik):
        if rdate in known_dates:
            continue
        txt = clean_filing_text(url)
        val = parser(txt)
        if val is not None:
            fresh.append([rdate, val])
        else:
            print(f"[warn] {label}: pattern not matched in {rdate} ({url})", file=sys.stderr)
    return fresh


def main() -> int:
    html_text = INDEX_HTML.read_text(encoding="utf-8")
    data = load_current_data(html_text)
    daily = data.setdefault("daily", {})
    quarterly = data.setdefault("quarterly", {})

    for key, sid in FRED_SERIES.items():
        try:
            fresh = fetch_fred_series(sid)
            daily[key] = merge_points(daily.get(key, []), fresh, MAX_DAILY_POINTS)
            print(f"[ok] daily.{key} ({sid}): {len(daily[key])} pts, latest={daily[key][-1]}")
        except Exception as exc:
            print(f"[warn] daily.{key} ({sid}) failed: {exc}", file=sys.stderr)

    edgar_jobs = [
        ("orcl", lambda: fetch_xbrl_rpo_100m("0001341439")),
        ("googl", lambda: fetch_xbrl_rpo_100m("0001652044")),
        ("nvda", fetch_nvda_dso),
        (
            "msft",
            lambda: fetch_filing_text_series(
                "0000789019", quarterly.get("msft", []), parse_msft_commercial_rpo_100m, "msft"
            ),
        ),
        (
            "amzn",
            lambda: fetch_filing_text_series(
                "0001018724", quarterly.get("amzn", []), parse_amzn_commitments_100m, "amzn"
            ),
        ),
    ]

    for key, fn in edgar_jobs:
        try:
            fresh = fn()
            quarterly[key] = merge_points(quarterly.get(key, []), fresh, MAX_QUARTERLY_POINTS)
            latest = quarterly[key][-1] if quarterly[key] else None
            print(f"[ok] quarterly.{key}: {len(quarterly[key])} pts, latest={latest}")
        except Exception as exc:
            print(f"[warn] quarterly.{key} failed: {exc}", file=sys.stderr)

    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    updated_html = MARKER_RE.sub(lambda m: f"{m.group(1)}{serialized}{m.group(3)}", html_text, count=1)

    if updated_html == html_text:
        print("No data changes.")
        return 0

    INDEX_HTML.write_text(updated_html, encoding="utf-8")
    print("Updated index.html.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
