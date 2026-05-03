"""
jpx_fetcher.py — Download and parse the JPX daily stock price PDF (株式相場表).

PDF URL pattern:
  https://www.jpx.co.jp/markets/statistics-equities/daily/t13vrt0000010rks-att/stq_YYYYMMDD.pdf

stq PDF structure (16 columns per row):
  [0]=code  [1]=売買単位+銘柄名(merged)
  [2..5]  = AM  open/high/low/close
  [6..9]  = PM  open/high/low/close  ← [9] is our close price
  [10] = 最終気配  [11] = 前日比  [12] = VWAP
  [13] = 売買高(千株)  [14] = 売買代金(千円)

Market segment and sector are NOT columns — they appear as section headers
between stock rows in the text:

  プライム市場
  水産・農林業 Fishery,Agriculture&Forestry
  1301 100極洋 4,985.00 ... 4,915.00 ...
  1332 100ニッスイ ...

NOTE: This PDF has no 時価総額 (market cap) or 発行済株式数 (shares).
      market_cap is computed later in main.py using EDINET share count.
"""

from __future__ import annotations

import io
import json
import re
import time
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pdfplumber
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JPX_STATS_PAGE = (
    "https://www.jpx.co.jp/markets/statistics-equities/daily/index.html"
)
JPX_DIRECT_BASE = (
    "https://www.jpx.co.jp/markets/statistics-equities/daily/"
    "t13vrt0000010rks-att/stq_{date}.pdf"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.9",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    "Referer": "https://www.jpx.co.jp/markets/statistics-equities/daily/index.html",
}

EXCLUDED_SECTORS = {
    "銀行業", "保険業", "証券、商品先物取引業", "その他金融業",
}

# JPX 33 industry sectors (東証33業種)
JPX_SECTOR_NAMES = {
    "水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙",
    "化学", "医薬品", "石油・石炭製品", "ゴム製品", "ガラス・土石製品",
    "鉄鉢", "非鉄金属", "金属製品", "機械", "電気機器", "輸送用機器",
    "精密機器", "その他製品", "電気・ガス業", "陸運業", "海運業",
    "空運業", "倉庫・運輸関連業", "情報・通信業", "卸売業", "小売業",
    "銀行業", "証券、商品先物取引業", "保険業", "その他金融業",
    "不動産業", "サービス業",
}

# If any of these keywords appear in a non-stock line, we've left the
# domestic equity section and should stop collecting.
NON_EQUITY_SECTION_KEYWORDS = (
    "ETF", "ETN", "インフラ", "不動産投資信託", "外国株式", "外国証券",
    "PRO Market", "転換社債", "新株予約権証券",
)

DIAGNOSTIC_FILE = Path(__file__).parent.parent / "data" / "jpx_diagnostic.txt"


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

def _recent_trading_days(n: int = 7) -> list[str]:
    days: list[str] = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return days


def _try_direct_url() -> Optional[str]:
    for date_str in _recent_trading_days(7):
        url = JPX_DIRECT_BASE.format(date=date_str)
        try:
            r = requests.head(url, headers=BROWSER_HEADERS, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                logger.info("[JPX] Direct URL found: %s", url)
                return url
        except requests.RequestException:
            pass
    return None


def _try_scrape_index() -> Optional[str]:
    try:
        resp = requests.get(JPX_STATS_PAGE, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("[JPX] Index page not accessible: %s", exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if re.search(r"stq_\d{8}\.pdf", href, re.IGNORECASE):
            url = ("https://www.jpx.co.jp" + href) if href.startswith("/") else href
            logger.info("[JPX] Scraped URL: %s", url)
            return url
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") and "statistics-equities/daily" in href:
            url = ("https://www.jpx.co.jp" + href) if href.startswith("/") else href
            logger.info("[JPX] Scraped URL (fallback): %s", url)
            return url
    logger.warning("[JPX] No stq PDF link found on index page.")
    return None


def get_latest_pdf_url() -> str:
    url = _try_scrape_index() or _try_direct_url()
    if url:
        return url
    raise RuntimeError(
        "Could not locate JPX stq PDF via index scraping or direct URL pattern."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_market(raw: str) -> str:
    if "グロース" in raw: return "グロース"
    if "スタンダード" in raw: return "スタンダード"
    return "プライム"


def _safe_num(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    s = str(text).strip().replace(",", "").replace("，", "")
    s = s.replace("－", "").replace("−", "").replace("▲", "-").replace("△", "-")
    s = s.lstrip("+")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _close_from_parts(parts: list[str]) -> Optional[float]:
    """
    Extract close price from a stock row's token list.
    Layout: [0]=code [1]=unit+name [2..5]=AM OHLC [6..9]=PM OHLC ...
    PM close (parts[9]) is preferred; fall back to AM close (parts[5]).
    """
    if len(parts) > 9:
        v = _safe_num(parts[9])
        if v and v > 0:
            return v
    if len(parts) > 5:
        v = _safe_num(parts[5])
        if v and v > 0:
            return v
    return None


def _name_from_unit_name(raw: str) -> str:
    """Strip the leading trading unit number from '100極洋' → '極洋'."""
    return re.sub(r'^\d+', '', raw).strip()


# ---------------------------------------------------------------------------
# Core parser: extract_text() with section-header context tracking
# ---------------------------------------------------------------------------

def _parse_via_text(pdf: pdfplumber.PDF) -> tuple[dict[str, dict], int]:
    """
    Parse the stq PDF using extract_text(), tracking market segment and sector
    from section headers.  Returns (stocks_dict, pages_with_stocks).
    """
    stocks: dict[str, dict] = {}
    pages_with_stocks = 0
    code_re = re.compile(r"^\d{4}$")

    # Context persists across pages
    current_market: str = ""
    current_sector: str = ""

    for page in pdf.pages:
        text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
        if not text:
            continue

        page_had_stocks = False

        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue

            # --- Market segment detection ---
            if "プライム市場" in s:
                current_market = "プライム"
                continue
            if "スタンダード市場" in s:
                current_market = "スタンダード"
                continue
            if "グロース市場" in s or ("グロース" in s and "市場" in s):
                current_market = "グロース"
                continue

            # --- Non-equity section detection → stop collecting ---
            parts = s.split()
            first = parts[0] if parts else ""
            if not code_re.match(first):
                if any(kw in s for kw in NON_EQUITY_SECTION_KEYWORDS):
                    current_market = ""
                    current_sector = ""
                    continue

            # Only collect from the three domestic equity segments
            if current_market not in ("プライム", "スタンダード", "グロース"):
                continue

            # --- Sector header detection ---
            if first in JPX_SECTOR_NAMES and not code_re.match(first):
                current_sector = first
                continue

            # --- Stock row ---
            if not code_re.match(first):
                continue  # English name, date, header, etc.

            if len(parts) < 10:
                continue

            close = _close_from_parts(parts)
            if close is None:
                continue

            if current_sector in EXCLUDED_SECTORS:
                continue

            code = first
            name = _name_from_unit_name(parts[1]) if len(parts) > 1 else ""

            stocks[code] = {
                "code": code,
                "name": name,
                "price": int(close),
                "market": current_market,
                "sector": current_sector,
            }
            page_had_stocks = True

        if page_had_stocks:
            pages_with_stocks += 1

    return stocks, pages_with_stocks


# ---------------------------------------------------------------------------
# Fallback: extract_words() with context tracking
# ---------------------------------------------------------------------------

def _parse_via_words(pdf: pdfplumber.PDF) -> tuple[dict[str, dict], int]:
    """
    Fallback parser using extract_words() with Y-position row grouping.
    Applies the same context-tracking logic as _parse_via_text.
    """
    from collections import defaultdict

    stocks: dict[str, dict] = {}
    pages_with_stocks = 0
    code_re = re.compile(r"^\d{4}$")

    current_market: str = ""
    current_sector: str = ""

    for page in pdf.pages:
        words = page.extract_words(x_tolerance=3, y_tolerance=3,
                                   keep_blank_chars=False, use_text_flow=False)
        if not words:
            continue

        # Group words into rows by Y position
        buckets: dict[int, list] = defaultdict(list)
        for w in words:
            key = round(w["top"] / 4.0)
            buckets[key].append(w)
        rows = [sorted(v, key=lambda w: w["x0"]) for _, v in sorted(buckets.items())]

        page_had_stocks = False

        for row in rows:
            texts = [w["text"] for w in row]
            if not texts:
                continue
            joined = " ".join(texts)
            first = texts[0]

            # Market segment
            if "プライム市場" in joined:
                current_market = "プライム"
                continue
            if "スタンダード市場" in joined:
                current_market = "スタンダード"
                continue
            if "グロース市場" in joined or ("グロース" in joined and "市場" in joined):
                current_market = "グロース"
                continue

            if not code_re.match(first):
                if any(kw in joined for kw in NON_EQUITY_SECTION_KEYWORDS):
                    current_market = ""
                    current_sector = ""
                if first in JPX_SECTOR_NAMES:
                    current_sector = first
                continue

            if current_market not in ("プライム", "スタンダード", "グロース"):
                continue

            if len(texts) < 10:
                continue

            close = _close_from_parts(texts)
            if close is None:
                continue

            if current_sector in EXCLUDED_SECTORS:
                continue

            code = first
            name = _name_from_unit_name(texts[1]) if len(texts) > 1 else ""

            stocks[code] = {
                "code": code,
                "name": name,
                "price": int(close),
                "market": current_market,
                "sector": current_sector,
            }
            page_had_stocks = True

        if page_had_stocks:
            pages_with_stocks += 1

    return stocks, pages_with_stocks


# ---------------------------------------------------------------------------
# Diagnostic writer
# ---------------------------------------------------------------------------

def _write_diagnostic(diag: dict, path: Path = DIAGNOSTIC_FILE) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("=== JPX PDF Diagnostic ===\n")
            f.write(json.dumps(diag, ensure_ascii=False, indent=2))
        logger.info("[JPX] Diagnostic written to %s", path)
    except Exception as exc:
        logger.warning("[JPX] Could not write diagnostic: %s", exc)


# ---------------------------------------------------------------------------
# Top-level PDF parser
# ---------------------------------------------------------------------------

def parse_pdf(pdf_bytes: bytes) -> tuple[dict[str, dict], dict]:
    """
    Parse the JPX stq PDF.  Returns (stocks_dict, diagnostics_dict).
    Writes data/jpx_diagnostic.txt for post-run inspection.
    """
    diag: dict = {"strategies": {}}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)
        diag["total_pages"] = total_pages
        logger.info("[JPX] PDF opened: %d pages", total_pages)

        # Save raw page-1 content for diagnostics
        if pdf.pages:
            p0 = pdf.pages[0]
            diag["page1_text"] = (p0.extract_text() or "")[:2000]

        # --- Primary: extract_text() with context tracking ---
        stocks, pages = _parse_via_text(pdf)
        diag["strategies"]["extract_text"] = {
            "pages_with_stocks": pages,
            "stocks_found": len(stocks),
        }
        logger.info("[JPX] Strategy 1 (text+context): pages=%d  stocks=%d",
                    pages, len(stocks))

        if stocks:
            diag["strategy_used"] = "extract_text"
            _write_diagnostic(diag)
            return stocks, diag

        # --- Fallback: extract_words() with context tracking ---
        stocks, pages = _parse_via_words(pdf)
        diag["strategies"]["extract_words"] = {
            "pages_with_stocks": pages,
            "stocks_found": len(stocks),
        }
        logger.info("[JPX] Strategy 2 (words+context): pages=%d  stocks=%d",
                    pages, len(stocks))

        if stocks:
            diag["strategy_used"] = "extract_words"
        else:
            diag["strategy_used"] = "none"
            logger.error(
                "[JPX] All strategies returned 0 stocks. "
                "See data/jpx_diagnostic.txt for page1_text."
            )

    _write_diagnostic(diag)
    return stocks, diag


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch() -> tuple[dict[str, dict], dict]:
    """
    Locate, download, and parse the latest JPX stq PDF.
    Returns (stocks_dict, diagnostics_dict).
    stocks_dict values: {code, name, price, market, sector}
    NOTE: market_cap is NOT included — computed in main.py from EDINET shares.
    Raises RuntimeError on download failure.
    """
    pdf_url = get_latest_pdf_url()
    logger.info("[JPX] Downloading: %s", pdf_url)

    for attempt in range(3):
        try:
            resp = requests.get(pdf_url, headers=BROWSER_HEADERS, timeout=120)
            resp.raise_for_status()
            logger.info("[JPX] Downloaded %d bytes", len(resp.content))
            break
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError(f"JPX PDF download failed: {exc}") from exc
            wait = 2 ** attempt
            logger.warning("[JPX] Attempt %d failed, retrying in %ds", attempt + 1, wait)
            time.sleep(wait)

    return parse_pdf(resp.content)


# ---------------------------------------------------------------------------
# CLI helper
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stocks, diag = fetch()
    print("Diagnostics:", json.dumps(
        {k: v for k, v in diag.items() if k != "page1_text"},
        ensure_ascii=False, indent=2,
    ))
    for code, d in list(stocks.items())[:5]:
        print(f"  {code}: {d}")
    print(f"Total: {len(stocks)}")
