#!/usr/bin/env python3
"""
main.py — Orchestration script for the weekly net cash ratio screener.

Steps:
  1. Fetch JPX stock price PDF → universe of stocks with prices & market caps
  2. Build EDINET code map for all companies in the universe
  3. Fetch / use cached EDINET financial data per company
  4. Scrape TDnet for recent disclosure surprises
  5. Calculate metrics, VT signals, and scores
  6. Filter to stocks with NC ratio >= 0.1x
  7. Write data/results.json

Run:
  EDINET_API_KEY=<key> python scripts/main.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

# Add the scripts directory to path so modules are importable
sys.path.insert(0, str(Path(__file__).parent))

import calculator
import edinet_fetcher
import jpx_fetcher
import tdnet_fetcher

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).parent.parent
RESULTS_JSON = ROOT_DIR / "data" / "results.json"
EDINET_CACHE_JSON = ROOT_DIR / "data" / "edinet_cache.json"
EDINET_MAP_CACHE_JSON = ROOT_DIR / "data" / "edinet_map_cache.json"
ALL_STOCKS_JSON = ROOT_DIR / "data" / "all_stocks.json"
STOCK_HISTORY_DIR = ROOT_DIR / "data" / "stock_history"

JST = pytz.timezone("Asia/Tokyo")

# Screening thresholds
MIN_NC_RATIO = 0.7           # Net cash ratio ≥ 0.7x
MIN_MARKET_CAP_OKU = 3.0    # 3億円 minimum
MAX_MARKET_CAP_OKU = 1000.0  # 1000億円 maximum
MAX_PER = 15.0               # PER ≤ 15 (loss-making stocks with PER=None are allowed)

# Stop fetching new EDINET data after this many seconds (to stay within CI timeout)
MAX_EDINET_RUNTIME_SECONDS = 50 * 60  # 50 minutes

# How many days back to scan EDINET for document listings.
# 400 days covers a full annual reporting cycle (有価証券報告書は決算後3ヶ月以内に提出、
# 最長の3月決算企業は6〜7月提出 → 翌年4月実行時に約9〜10ヶ月前) + バッファ。
# 半期報告書（160）は6ヶ月ごとに提出されるため、400日あれば直近回が必ず含まれる。
EDINET_SCAN_DAYS = 400

# Max companies to fetch from EDINET (0 = unlimited). Set via EDINET_FETCH_LIMIT env var.
# Use a small number (e.g. 30) for quick verification runs.
EDINET_FETCH_LIMIT = int(os.environ.get("EDINET_FETCH_LIMIT", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_friday_18_jst() -> str:
    """Return ISO timestamp of next Friday 18:00 JST."""
    now = datetime.now(JST)
    days_ahead = 4 - now.weekday()  # Friday = 4
    if days_ahead <= 0:
        days_ahead += 7
    next_friday = (now + timedelta(days=days_ahead)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    return next_friday.isoformat()


def _load_previous_results() -> dict:
    """Load the existing results.json for delta tracking (passed_prev, surprise_prev)."""
    try:
        with open(RESULTS_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_error_results(error_msg: str, debug: dict) -> None:
    """Write a minimal results.json that makes the failure reason visible in the UI."""
    now_jst = datetime.now(JST)
    output = {
        "generated_at": now_jst.isoformat(),
        "next_update_at": _next_friday_18_jst(),
        "screened_count": 0,
        "passed_count": 0,
        "passed_prev": None,
        "surprise_count": 0,
        "surprise_prev": None,
        "error": error_msg,
        "debug": debug,
        "stocks": [],
    }
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(RESULTS_JSON, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info("Error results written to %s", RESULTS_JSON)
    except Exception as exc:
        logger.warning("Failed to write error results: %s", exc)


def _save_all_stocks(all_stocks_data: list[dict], now_jst: datetime) -> None:
    """Save full metrics for all computed stocks and update per-stock history."""
    date_str = now_jst.strftime("%Y-%m-%d")

    try:
        all_out = {
            "generated_at": now_jst.isoformat(),
            "count": len(all_stocks_data),
            "stocks": sorted(
                all_stocks_data,
                key=lambda x: x.get("net_cash_ratio") or -999,
                reverse=True,
            ),
        }
        ALL_STOCKS_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(ALL_STOCKS_JSON, "w", encoding="utf-8") as f:
            json.dump(all_out, f, ensure_ascii=False, indent=2)
        logger.info("Saved all_stocks.json (%d stocks)", len(all_stocks_data))
    except Exception as exc:
        logger.warning("Failed to save all_stocks.json: %s", exc)

    try:
        STOCK_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        for s in all_stocks_data:
            history_path = STOCK_HISTORY_DIR / f"{s['code']}.json"
            if history_path.exists():
                with open(history_path, encoding="utf-8") as f:
                    history = json.load(f)
            else:
                history = []
            history = [h for h in history if h.get("date") != date_str]
            history.append({
                "date": date_str,
                "price": s["price"],
                "market_cap": s["market_cap"],
                "net_cash_ratio": s["net_cash_ratio"],
                "pbr": s["pbr"],
                "per": s["per"],
                "roe": s["roe"],
                "roa": s["roa"],
                "div_yield": s["div_yield"],
            })
            history.sort(key=lambda x: x["date"])
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        logger.info("Updated stock history files for %d stocks", len(all_stocks_data))
    except Exception as exc:
        logger.warning("Failed to save stock history: %s", exc)


def _validate_stock_record(s: dict) -> bool:
    """Ensure all required fields are present before writing to results.json."""
    required = (
        "code", "name", "price", "market_cap", "sector", "market",
        "net_cash", "net_cash_ratio", "score", "rank",
        "surprise", "surprise_pct", "vtrap_signals", "note",
    )
    for field in required:
        if field not in s:
            return False
    # code and name must be non-empty strings
    if not s["code"] or not s["name"]:
        return False
    # price must be a positive number
    if not isinstance(s["price"], (int, float)) or s["price"] <= 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    api_key = os.environ.get("EDINET_API_KEY")
    if not api_key:
        logger.error("EDINET_API_KEY environment variable is not set.")
        sys.exit(1)

    prev_results = _load_previous_results()
    passed_prev: Optional[int] = prev_results.get("passed_count")
    surprise_prev: Optional[int] = prev_results.get("surprise_count")
    prev_rank_map: dict[str, int] = {s["code"]: s["rank"] for s in prev_results.get("stocks", [])}

    # ---- Step 1: JPX universe -----------------------------------------------
    logger.info("=== Step 1: Fetching JPX stock price PDF ===")
    jpx_diag: dict = {}
    try:
        jpx_stocks, jpx_diag = jpx_fetcher.fetch()
    except Exception as exc:
        logger.error("JPX fetch failed: %s — cannot continue without price data.", exc)
        _write_error_results(str(exc), jpx_diag)
        sys.exit(1)

    screened_count = len(jpx_stocks)
    logger.info("JPX universe: %d stocks (after sector/market filters)", screened_count)

    if screened_count == 0:
        logger.error(
            "=== STEP 1 FAILED: JPX returned 0 stocks. "
            "Strategies tried: %s. First-page text sample: %s ===",
            list(jpx_diag.get("strategies", {}).keys()),
            jpx_diag.get("first_page_text_sample", "")[:200],
        )
        _write_error_results("JPX PDF parsed 0 stocks", jpx_diag)
        sys.exit(1)

    # ---- Step 2: EDINET document map ----------------------------------------
    logger.info("=== Step 2: Building EDINET code map ===")

    # Quick connectivity test: fetch one day to verify API key works
    _edinet_api_ok = False
    _edinet_api_msg = ""
    try:
        from datetime import date as _date
        _test_date = (_date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        _test_resp = edinet_fetcher._edinet_request(
            f"documents.json?date={_test_date}&type=2", api_key, timeout=15
        )
        _test_data = _test_resp.json()
        _n = len(_test_data.get("results", []))
        _edinet_api_ok = True
        _edinet_api_msg = f"OK: {_n} docs on {_test_date}"
        logger.info("[EDINET] API test: %s", _edinet_api_msg)
    except Exception as _exc:
        _edinet_api_msg = f"FAILED: {_exc}"
        logger.error("[EDINET] API test FAILED — key may be invalid or API unreachable: %s", _exc)

    all_codes = list(jpx_stocks.keys())
    try:
        edinet_map = edinet_fetcher.build_code_to_edinet_map(
            api_key, all_codes, days_back=EDINET_SCAN_DAYS,
            map_cache_path=str(EDINET_MAP_CACHE_JSON),
        )
    except Exception as exc:
        logger.error("EDINET document map build failed: %s", exc)
        edinet_map = {}

    logger.info("EDINET map covers %d / %d companies", len(edinet_map), screened_count)

    # ---- Step 3: EDINET financial data (with caching) -----------------------
    logger.info("=== Step 3: Fetching EDINET financial data ===")
    cache = edinet_fetcher.load_cache(str(EDINET_CACHE_JSON))
    updated_cache = dict(cache)
    financials: dict[str, dict] = {}
    edinet_start = time.time()
    fetch_count = 0
    cache_hit_count = 0

    for code, edinet_info in edinet_map.items():
        # Time-limit guard: stop fetching new data if we're running low on CI time
        if time.time() - edinet_start > MAX_EDINET_RUNTIME_SECONDS:
            logger.warning("Approaching time limit — stopping EDINET fetch early.")
            break
        # Fetch-limit guard: for quick verification runs
        if EDINET_FETCH_LIMIT > 0 and fetch_count >= EDINET_FETCH_LIMIT:
            logger.info("EDINET_FETCH_LIMIT=%d reached — stopping early.", EDINET_FETCH_LIMIT)
            break

        edinet_code = edinet_info["edinet_code"]
        doc_id = edinet_info["doc_id"]
        submit_date = edinet_info["submit_date"]

        cached = cache.get(edinet_code)

        if not edinet_fetcher.needs_refresh(cached, doc_id, submit_date):
            # Use cached data
            financials[code] = cached
            cache_hit_count += 1
            continue

        # Fetch fresh data from EDINET
        try:
            fin = edinet_fetcher.fetch_financials(api_key, doc_id)
            fin["doc_id"] = doc_id
            fin["submit_date"] = submit_date
            fin["fetched_at"] = datetime.now(JST).isoformat()
            updated_cache[edinet_code] = fin
            financials[code] = fin
            fetch_count += 1
        except Exception as exc:
            logger.warning("EDINET fetch failed for %s (docID=%s): %s", code, doc_id, exc)
            if cached:
                logger.info("  → using stale cache for %s", code)
                financials[code] = cached
            # else: company will be skipped during metric calculation

    logger.info(
        "EDINET: %d fetched fresh, %d from cache, %d companies with data",
        fetch_count, cache_hit_count, len(financials),
    )
    # Log a sample financials entry for debugging
    for _sample_code, _sample_fin in list(financials.items())[:3]:
        logger.info("  Sample fin[%s]: cash=%s shares=%s net_assets=%s",
                    _sample_code, _sample_fin.get("cash"), _sample_fin.get("shares"),
                    _sample_fin.get("net_assets"))

    # Persist updated cache
    try:
        edinet_fetcher.save_cache(str(EDINET_CACHE_JSON), updated_cache)
        logger.info("EDINET cache saved (%d entries)", len(updated_cache))
    except Exception as exc:
        logger.warning("Failed to save EDINET cache: %s", exc)

    # ---- Step 4: TDnet surprises --------------------------------------------
    logger.info("=== Step 4: Fetching TDnet surprise signals ===")
    try:
        surprises = tdnet_fetcher.fetch_tdnet_surprises(set(jpx_stocks.keys()))
    except Exception as exc:
        logger.warning("TDnet fetch failed: %s — all stocks will have surprise=None", exc)
        surprises = {}

    # ---- Step 5 & 6: Calculate metrics and filter ---------------------------
    logger.info("=== Step 5: Calculating metrics and filtering ===")
    stocks_out: list[dict] = []
    all_stocks_data: list[dict] = []
    skipped_no_data = 0
    skipped_low_nc = 0

    skipped_small_cap = 0

    for code, jpx_data in jpx_stocks.items():
        fin = financials.get(code)
        if fin is None:
            skipped_no_data += 1
            continue

        # Compute market_cap from EDINET shares (stq PDF does not include it)
        price = jpx_data["price"]
        shares = fin.get("shares")
        if not shares or shares <= 0:
            skipped_no_data += 1
            continue
        market_cap_oku = round(price * shares / 1e8, 1)

        if market_cap_oku < MIN_MARKET_CAP_OKU:
            skipped_small_cap += 1
            continue
        if market_cap_oku > MAX_MARKET_CAP_OKU:
            skipped_small_cap += 1
            continue

        jpx_data_full = {**jpx_data, "market_cap": market_cap_oku}

        try:
            metrics = calculator.calc_metrics(jpx_data_full, fin)
        except Exception as exc:
            logger.warning("Metrics calc failed for %s: %s", code, exc)
            skipped_no_data += 1
            continue

        ncr = metrics.get("net_cash_ratio")
        vt_signals = calculator.detect_vt_signals(fin, metrics)

        # Collect all stocks with valid metrics (before NC ratio / PER filters)
        all_stocks_data.append({
            "code": code,
            "name": jpx_data["name"],
            "market": jpx_data["market"],
            "sector": jpx_data["sector"],
            "price": jpx_data["price"],
            "market_cap": market_cap_oku,
            "net_cash": metrics["net_cash"],
            "net_cash_ratio": ncr,
            "pbr": metrics.get("pbr"),
            "per": metrics.get("per"),
            "roe": metrics.get("roe"),
            "roa": metrics.get("roa"),
            "div_yield": metrics.get("div_yield") or 0.0,
            "operating_cf": metrics.get("operating_cf"),
            "revenue_growth": metrics.get("revenue_growth"),
            "has_buyback": fin.get("has_buyback", False),
            "vtrap_signals": vt_signals,
            "passed_screening": False,  # updated after the full loop
        })

        if ncr is None or ncr < MIN_NC_RATIO:
            skipped_low_nc += 1
            continue

        # PER filter: exclude if PER is known and > MAX_PER
        per_val = metrics.get("per")
        if per_val is not None and per_val > MAX_PER:
            skipped_low_nc += 1
            continue

        surprise_info = surprises.get(code, {})
        surprise = surprise_info.get("surprise")
        surprise_pct = surprise_info.get("surprise_pct", 0)

        score = calculator.calc_score(
            ncr,
            metrics.get("pbr"),
            metrics.get("per"),
            surprise,
            surprise_pct,
            market_cap_oku,
            vt_signals,
        )

        note = calculator.generate_note(metrics, vt_signals, jpx_data["sector"])

        record = {
            "code": code,
            "name": jpx_data["name"],
            "price": jpx_data["price"],
            "market_cap": market_cap_oku or 0.0,
            "sector": jpx_data["sector"],
            "market": jpx_data["market"],
            "net_cash": metrics["net_cash"],
            "net_cash_ratio": ncr,
            "pbr": metrics.get("pbr"),
            "per": metrics.get("per"),
            "eps": metrics.get("eps"),
            "roe": metrics.get("roe"),
            "roa": metrics.get("roa"),
            "div_yield": metrics.get("div_yield") or 0.0,
            "operating_cf": metrics.get("operating_cf"),
            "revenue_growth": metrics.get("revenue_growth"),
            "has_buyback": fin.get("has_buyback", False),
            "debt_missing": fin.get("debt_missing", False),
            "shares": shares,
            "score": score,
            "rank": 0,           # assigned after sorting
            "surprise": surprise,
            "surprise_pct": surprise_pct,
            "vtrap_signals": vt_signals,
            "note": note,
        }

        if not _validate_stock_record(record):
            logger.warning("Incomplete record for %s — skipping", code)
            skipped_no_data += 1
            continue

        stocks_out.append(record)

    logger.info(
        "Filter results: passed=%d, skipped_no_data=%d, skipped_small_cap=%d, skipped_low_nc=%d",
        len(stocks_out), skipped_no_data, skipped_small_cap, skipped_low_nc,
    )

    # Mark which stocks passed the screener
    passed_codes = {s["code"] for s in stocks_out}
    for s in all_stocks_data:
        s["passed_screening"] = s["code"] in passed_codes

    # ---- Step 7: Sort and assign ranks --------------------------------------
    stocks_out.sort(key=lambda s: s["score"], reverse=True)
    for i, s in enumerate(stocks_out, 1):
        s["rank"] = i
        s["rank_prev"] = prev_rank_map.get(s["code"])

    # ---- Step 7.5: Save all-stocks database and history ---------------------
    now_jst = datetime.now(JST)
    logger.info("=== Step 7.5: Saving all-stocks database and history ===")
    _save_all_stocks(all_stocks_data, now_jst)

    # ---- Step 8: Write results.json -----------------------------------------
    surprise_count = sum(1 for s in stocks_out if s["surprise"] is not None)

    output = {
        "generated_at": now_jst.isoformat(),
        "next_update_at": _next_friday_18_jst(),
        "screened_count": screened_count,
        "passed_count": len(stocks_out),
        "passed_prev": passed_prev,
        "surprise_count": surprise_count,
        "surprise_prev": surprise_prev,
        "debug": {
            "jpx": jpx_diag,
            "edinet_api": _edinet_api_msg,
            "edinet_map_count": len(edinet_map),
            "edinet_fetched": fetch_count,
            "edinet_cache_hits": cache_hit_count,
            "edinet_with_data": len(financials),
            "skipped_no_data": skipped_no_data,
            "skipped_small_cap": skipped_small_cap,
            "skipped_low_nc": skipped_low_nc,
        },
        "stocks": stocks_out,
    }

    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(
        "=== Done. screened=%d  passed=%d  surprises=%d  written to %s ===",
        screened_count, len(stocks_out), surprise_count, RESULTS_JSON,
    )


if __name__ == "__main__":
    main()
