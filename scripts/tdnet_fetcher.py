"""
tdnet_fetcher.py — Scrape TDnet (適時開示情報閲覧サービス) for recent disclosures
and classify them as positive or negative "surprise" signals.

TDnet has no official API; we scrape the daily disclosure list pages.
Check the last 3 business days (Mon–Fri) to capture any disclosures made
after market hours on previous days.

Returns:
  { "1234": { "surprise": "positive"|"negative"|None, "surprise_pct": int } }
  where surprise_pct is the count of classified disclosures found.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TDNET_BASE = "https://www.release.tdnet.info/inbs/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NetCashScreener/1.0; "
        "+https://github.com/tororo-jp/screening-frontend)"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Referer": "https://www.release.tdnet.info/",
}

# Keywords that mark a disclosure as positive
POSITIVE_KEYWORDS = (
    "上方修正",
    "増配",
    "自社株買い",
    "自己株式の取得",
    "業績の上方修正",
)

# Keywords that mark a disclosure as negative
NEGATIVE_KEYWORDS = (
    "下方修正",
    "業績の下方修正",
    "減配",
    "無配",
    "特別損失",
)

# Earnings announcements are treated as positive catalysts (surprise triggers).
# If earnings are released with a downward revision inside, the NEGATIVE_KEYWORDS
# check will override.
EARNINGS_KEYWORDS = (
    "決算短信",
    "四半期報告",
    "通期業績",
)

DELAY_BETWEEN_DAYS = 0.5   # seconds between page requests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_business_days(n: int = 3) -> list[str]:
    """
    Return the last `n` business days (Mon–Fri) as YYYYMMDD strings,
    most recent first.
    Note: This does not account for Japanese public holidays.
    """
    days: list[str] = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:   # Mon=0 … Fri=4
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return days


def _classify_title(title: str) -> Optional[str]:
    """
    Classify a disclosure title as 'positive', 'negative', or None.
    Negative takes precedence over positive.
    """
    if any(kw in title for kw in NEGATIVE_KEYWORDS):
        return "negative"
    if any(kw in title for kw in POSITIVE_KEYWORDS):
        return "positive"
    if any(kw in title for kw in EARNINGS_KEYWORDS):
        return "positive"
    return None


def _parse_tdnet_page(html: str, target_codes: set[str]) -> dict[str, list[str]]:
    """
    Parse a TDnet daily list page and return disclosures relevant to
    `target_codes`.

    Returns:
      { "1234": ["positive", "negative", ...] }  — list of classifications per stock

    TDnet table structure (as of 2024):
      <table class="M-tableType01">
        <tr>
          <td>...</td>             # 時刻 (time)
          <td>コード</td>          # 4-digit code
          <td>会社名</td>          # company name
          <td>表題</td>            # disclosure title
          <td>PDF</td>             # PDF link
        </tr>
      </table>
    The column indices can vary; we detect the code by regex rather than index.
    """
    soup = BeautifulSoup(html, "lxml")
    results: dict[str, list[str]] = {}

    # TDnet uses several table classes; try common ones
    tables = soup.find_all("table") or []
    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            # Find the cell containing a 4-digit stock code
            code: Optional[str] = None
            title: Optional[str] = None

            for i, cell in enumerate(cells):
                text = cell.get_text(strip=True)
                m = re.match(r"^(\d{4})$", text)
                if m:
                    code = m.group(1)
                    # Title is typically in the next or the cell after next
                    for j in range(i + 1, min(i + 4, len(cells))):
                        candidate = cells[j].get_text(strip=True)
                        if candidate and len(candidate) > 3 and not candidate.isdigit():
                            title = candidate
                            break
                    break

            if not code or not title:
                continue
            if code not in target_codes:
                continue

            classification = _classify_title(title)
            if classification is None:
                continue

            if code not in results:
                results[code] = []
            results[code].append(classification)

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_tdnet_surprises(target_codes: set[str]) -> dict[str, dict]:
    """
    Scrape TDnet for the last 3 business days and return surprise classifications.

    Returns:
      { "1234": { "surprise": "positive"|"negative"|None, "surprise_pct": int } }
    """
    aggregated: dict[str, list[str]] = {}
    business_days = _get_business_days(3)

    for day_str in business_days:
        url = f"{TDNET_BASE}I_list_000_{day_str}.html"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 404:
                # No trading on this date (holiday or weekend edge case)
                logger.debug("TDnet page not found for %s (likely holiday)", day_str)
                time.sleep(DELAY_BETWEEN_DAYS)
                continue
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
        except requests.RequestException as exc:
            logger.warning("Failed to fetch TDnet page for %s: %s", day_str, exc)
            time.sleep(DELAY_BETWEEN_DAYS)
            continue

        day_results = _parse_tdnet_page(resp.text, target_codes)
        for code, classifications in day_results.items():
            if code not in aggregated:
                aggregated[code] = []
            aggregated[code].extend(classifications)

        logger.debug("TDnet %s: %d disclosures for target codes", day_str, len(day_results))
        time.sleep(DELAY_BETWEEN_DAYS)

    # Consolidate per company: negative overrides positive
    output: dict[str, dict] = {}
    for code, classifications in aggregated.items():
        has_negative = "negative" in classifications
        has_positive = "positive" in classifications
        surprise = "negative" if has_negative else ("positive" if has_positive else None)
        output[code] = {
            "surprise": surprise,
            "surprise_pct": len(classifications),
        }

    logger.info("TDnet surprises detected for %d companies", len(output))
    return output


# ---------------------------------------------------------------------------
# CLI helper for local testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    # Test with a broad set
    test_codes = {"6501", "7203", "9984", "3382", "4755", "6758", "7974"}
    result = fetch_tdnet_surprises(test_codes)
    for code, info in result.items():
        print(f"{code}: {info}")
    print(f"\nTotal codes with surprises: {len(result)}")
