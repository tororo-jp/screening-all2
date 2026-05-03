"""
edinet_fetcher.py — Fetch financial data from EDINET API v2.

Flow:
  1. Scan the last N days of EDINET document submissions to build a map
     of TSE code → latest annual/quarterly report metadata.
  2. For each company not in cache (or with a stale cache entry), download
     the full document ZIP (type=1) and parse the XBRL / inline-XBRL data.
  3. Cache parsed results in data/edinet_cache.json to avoid re-fetching.

All monetary values are stored/returned in raw JPY (int or float).
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta, date
from typing import Optional

import requests
from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"

DOC_TYPE_ANNUAL = "120"        # 有価証券報告書
DOC_TYPE_QUARTERLY = "140"     # 四半期報告書（2024年度廃止、移行期残存分のみ）
DOC_TYPE_INTERIM = "160"       # 半期報告書（2024年度より四半期報告書に代わり義務化）

ACCEPTED_DOC_TYPES = frozenset({DOC_TYPE_ANNUAL, DOC_TYPE_QUARTERLY, DOC_TYPE_INTERIM})

DELAY_DOCUMENTS = 0.15
DELAY_XBRL = 0.1   # reduced: type=4 ZIP is smaller; EDINET allows faster polling

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NetCashScreener/1.0; "
        "+https://github.com/tororo-jp/screening-frontend)"
    ),
}

# ---------------------------------------------------------------------------
# XBRL element local-name candidates (J-GAAP then IFRS fallbacks)
# ---------------------------------------------------------------------------

XBRL_ELEMENTS = {
    "cash": [
        "CashAndDeposits",
        "CashAndCashEquivalents",
        "CashAndDueFromBanks",
    ],
    "current_assets": [
        "CurrentAssets",
    ],
    "securities": [
        "InvestmentSecurities",
        "InvestmentSecuritiesAndEquitySecurities",
    ],
    "total_liabilities": [
        "Liabilities",
    ],
    "debt_short": [
        "ShortTermLoansPayable",
        "ShortTermBorrowings",
        "CommercialPapersPayable",
        "CurrentPortionOfLongTermLoansPayable",
        "CurrentPortionOfBondsPayable",
        "ShorttermBorrowings",
        "CurrentPortionOfLongtermBorrowings",
        "CurrentBondsBillsAndNotesPayable",
        "LeaseLiabilitiesCurrent",
        "LeaseObligationsCurrent",
    ],
    "debt_long": [
        "LongTermLoansPayable",
        "LongTermBorrowings",
        "BondsPayable",
        "LongtermBorrowings",
        "NoncurrentBondsBillsAndNotesPayable",
        "LeaseLiabilitiesNoncurrent",
        "LeaseObligationsNoncurrent",
    ],
    "net_assets": [
        "NetAssets",
        "Equity",
        "NetAssetsNonConsolidated",
    ],
    "total_assets": [
        "Assets",
    ],
    "net_profit": [
        "ProfitLoss",
        "ProfitLossAttributableToOwnersOfParent",
        "NetIncomeLoss",
    ],
    "revenue_cur": [
        "NetSales",
        "Revenue",
        "OperatingRevenue",
    ],
    "operating_cf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "CashFlowsFromUsedInOperatingActivities",
        "NetCashProvidedByOperatingActivities",
        "CashFlowsFromOperatingActivities",
        "NetCashFromOperatingActivities",
    ],
    "shares": [
        "IssuedSharesTotalNumberOfSharesEtc",
        "IssuedSharesTotalNumberOfSharesEtcCoverPage",
        "NumberOfSharesOutstanding",
        "NumberOfSharesIssuedAndOutstanding",
        "TotalNumberOfIssuedShares",
        "NumberOfIssuedShares",
        "IssuedSharesCommonStock",
        "CommonStockSharesOutstanding",
        "NumberOfSharesIssued",
        "TotalSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasicShares",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfOrdinarySharesOutstandingBasic",
    ],
    "div_per_share": [
        "DividendPaidPerShareSummaryOfBusinessResults",   # cover-page annual total (most common)
        "DividendPerShareDividendsOfSurplus",             # per-payment; fallback (may be interim only)
        "DividendPaidPerShareKeyFinancialData",           # historical table fallback
        "DividendPaidPerShare",
        "DividendsPerShare",
        "AnnualDividendsPerShare",
    ],
    "buyback": [
        "TreasurySharesAcquiredDuringPeriod",
        "PurchaseOfTreasuryShares",
        "RepurchaseOfCommonStock",
    ],
}

ADDITIVE_FIELDS = {"debt_short", "debt_long"}

# Financial fields (present in cache = successfully parsed)
FIN_FIELDS = ("current_assets", "shares", "net_assets", "net_profit", "total_assets")

# Share-count element names (used for unit-ref filtering)
_SHARE_LOCAL_NAMES: frozenset = frozenset(XBRL_ELEMENTS["shares"])
# Currency unit refs — exclude these when collecting share-count elements
_MONETARY_UNITS = ("JPY", "USD", "EUR", "GBP", "CNY", "KRW", "AUD", "HKD")
# Catch-all exclusion keywords for share name search (prevents equity/capital yen values)
_SHARE_EXCL_KWS = ("treasury", "buyback", "purchase", "repurchase",
                   "equity", "assets", "capital", "amount", "value", "consideration")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _edinet_request(path: str, api_key: str, stream: bool = False, timeout: int = 60) -> requests.Response:
    sep = "&" if "?" in path else "?"
    url = f"{EDINET_BASE}/{path}{sep}Subscription-Key={api_key}"
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, stream=stream, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            wait = 2 ** attempt
            logger.warning("EDINET request failed (attempt %d): %s — retrying in %ds", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Step 1: Build TSE-code → EDINET doc mapping
# ---------------------------------------------------------------------------

def load_map_cache(path: str) -> tuple[dict[str, dict], str]:
    """Load persisted edinet map and last-scan date. Returns (map, last_scan_date_str)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("map", {}), data.get("last_scan_date", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, ""


def save_map_cache(path: str, mapping: dict[str, dict], last_scan_date: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_scan_date": last_scan_date, "map": mapping}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save edinet map cache: %s", exc)


def build_code_to_edinet_map(
    api_key: str,
    target_codes: list[str],
    days_back: int = 90,
    map_cache_path: str = "",
) -> dict[str, dict]:
    """
    Scan EDINET document listings and build TSE-code → doc metadata map.

    If map_cache_path is provided, the existing map is loaded and only days
    since the last scan are fetched (incremental update), greatly reducing
    API calls on subsequent weekly runs.
    """
    if not target_codes:
        return {}

    target_set = set(target_codes)
    today = date.today()

    # Load cached map and determine which days still need scanning
    cached_map, last_scan_str = load_map_cache(map_cache_path) if map_cache_path else ({}, "")
    mapping: dict[str, dict] = dict(cached_map)

    if last_scan_str:
        try:
            last_scan = date.fromisoformat(last_scan_str)
            scan_days = (today - last_scan).days + 1  # +1 to include today
            scan_days = min(scan_days, days_back)
            logger.info("edinet_map cache loaded (%d entries, last scan %s) — scanning %d new days",
                        len(mapping), last_scan_str, scan_days)
        except ValueError:
            scan_days = days_back
    else:
        scan_days = days_back
        logger.info("No edinet_map cache — scanning %d days from scratch", scan_days)

    logger.info("Scanning EDINET document list for %d target codes over %d days...", len(target_set), scan_days)

    for i in range(scan_days):
        scan_date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            resp = _edinet_request(f"documents.json?date={scan_date}&type=2", api_key, timeout=30)
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch EDINET document list for %s: %s", scan_date, exc)
            time.sleep(DELAY_DOCUMENTS)
            continue

        for doc in data.get("results", []):
            sec_code = doc.get("secCode", "")
            if not (sec_code and len(sec_code) == 5 and sec_code[:4].isdigit()):
                continue
            tse_code = sec_code[:4]
            if tse_code not in target_set:
                continue

            doc_type = doc.get("docTypeCode", "")
            if doc_type not in ACCEPTED_DOC_TYPES:
                continue

            edinet_code = doc.get("edinetCode", "")
            doc_id = doc.get("docID", "")
            period_end = doc.get("periodEnd", "")
            submit_date = (doc.get("submitDateTime") or "")[:10]

            existing = mapping.get(tse_code)
            if existing is None:
                mapping[tse_code] = dict(
                    edinet_code=edinet_code, doc_id=doc_id,
                    period_end=period_end, submit_date=submit_date, doc_type=doc_type,
                )
            else:
                # Always adopt the most recently submitted document.
                # Annual reports are NOT auto-preferred over quarterly: a quarterly
                # filed 1 month ago has a more current balance sheet than an annual
                # filed 6 months ago.
                if submit_date > existing["submit_date"]:
                    mapping[tse_code] = dict(
                        edinet_code=edinet_code, doc_id=doc_id,
                        period_end=period_end, submit_date=submit_date, doc_type=doc_type,
                    )

        time.sleep(DELAY_DOCUMENTS)

    logger.info("EDINET doc map built for %d / %d companies", len(mapping), len(target_set))

    if map_cache_path:
        save_map_cache(map_cache_path, mapping, today.isoformat())

    return mapping


# ---------------------------------------------------------------------------
# Step 2: Cache helpers
# ---------------------------------------------------------------------------

def load_cache(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(path: str, cache: dict[str, dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def needs_refresh(cached: Optional[dict], latest_doc_id: str, latest_submit_date: str) -> bool:
    if cached is None:
        return True
    if cached.get("doc_id") != latest_doc_id:
        return True
    # Re-fetch if the cache entry is missing any of the three key fields used
    # for market-cap calculation and net-cash ratio. "any" is not enough —
    # a prior run may have written cash+net_assets but left shares=None.
    if not all(cached.get(k) is not None for k in ("current_assets", "shares", "net_assets")):
        return True
    # Re-fetch if revenue_cur == revenue_prev (indicates the prior-year XBRL row was
    # picked as the current-year value due to document ordering; fixed in _cur_preferred).
    rev_cur = cached.get("revenue_cur")
    rev_prev = cached.get("revenue_prev")
    if rev_cur is not None and rev_prev is not None and rev_cur == rev_prev:
        return True
    fetched_at_str = cached.get("fetched_at", "")
    if fetched_at_str:
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            if (datetime.now(fetched_at.tzinfo) - fetched_at).days > 30:
                return True
        except ValueError:
            return True
    return False


# ---------------------------------------------------------------------------
# Step 3: XBRL / iXBRL parsing
# ---------------------------------------------------------------------------

def _is_consolidated(context_ref: str) -> bool:
    non_cons_keywords = ("NonConsolidated", "Standalone", "Parent")
    return not any(kw in context_ref for kw in non_cons_keywords)


def _text_to_float(text: str, scale_attr: Optional[str] = None, sign_attr: Optional[str] = None) -> Optional[float]:
    """Convert text content to float, applying iXBRL scale/sign if present."""
    s = re.sub(r"[\s, 　]", "", text)
    if not s or s.lower() in ("nil", "n/a", "−", "-", "－"):
        return None
    # Strip trailing non-numeric unit suffixes (e.g. "株", "円", "千株")
    s = re.sub(r"[^\d.\-eE]+$", "", s)
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    if scale_attr:
        try:
            val *= 10 ** int(scale_attr)
        except (ValueError, OverflowError):
            pass
    if sign_attr == "-":
        val = -val
    return val


_SHARE_KWS = ("share", "issued", "outstanding")


def _collect_xbrl_values(tree: etree._ElementTree) -> dict[str, list]:
    """
    Collect concept → [(contextRef, value)] from a pure XBRL document.

    Two passes:
    1. Targeted search for known element local names in XBRL_ELEMENTS.
    2. Catch-all scan of every element whose local name contains a share-related
       keyword — logs the actual element name so we can add it to XBRL_ELEMENTS.
    """
    concept_vals: dict[str, list] = {}
    all_local_names: set[str] = set()
    for items in XBRL_ELEMENTS.values():
        all_local_names.update(items)

    # Pass 1: targeted
    for local_name in all_local_names:
        for el in tree.xpath(f"//*[local-name()='{local_name}']"):
            if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil", "").lower() == "true":
                continue
            # Skip monetary-unit values for share-count fields
            if local_name in _SHARE_LOCAL_NAMES:
                unit_ref = el.get("unitRef", "").upper()
                if any(c in unit_ref for c in _MONETARY_UNITS):
                    continue
            text = (el.text or "").strip()
            val = _text_to_float(text)
            if val is None:
                continue
            ctx = el.get("contextRef", "")
            concept_vals.setdefault(local_name, []).append((ctx, val))

    # Pass 2: catch-all for share-related elements not in our target list
    for el in tree.getroot().iter():
        raw_tag = el.tag or ""
        local = raw_tag.split("}")[-1] if "}" in raw_tag else raw_tag
        if local in all_local_names:
            continue  # already handled
        if not any(kw in local.lower() for kw in _SHARE_KWS):
            continue
        if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil", "").lower() == "true":
            continue
        text = (el.text or "").strip()
        val = _text_to_float(text)
        if val is None or val < 1000:  # shares must be a meaningful count
            continue
        # Skip monetary-unit values from catch-all share scan
        unit_ref = el.get("unitRef", "").upper()
        if any(c in unit_ref for c in _MONETARY_UNITS):
            continue
        ctx = el.get("contextRef", "")
        if not ctx:
            continue
        logger.info("[XBRL] catch-all share element found: %s = %s (ctx=%s)", local, val, ctx)
        concept_vals.setdefault(local, []).append((ctx, val))

    return concept_vals


def _collect_ixbrl_values(nonfrac_elements: list) -> dict[str, list]:
    """
    Collect concept → [(contextRef, value)] from iXBRL value elements.
    Works on any element that carries name="ns:LocalName" and contextRef attributes.
    """
    concept_vals: dict[str, list] = {}
    for node in nonfrac_elements:
        name_attr = node.get("name", "")
        local_name = name_attr.split(":", 1)[-1] if ":" in name_attr else name_attr
        if not local_name:
            continue

        ctx = node.get("contextRef", "")
        text = "".join(node.itertext()).strip()
        val = _text_to_float(
            text,
            scale_attr=node.get("scale"),
            sign_attr=node.get("sign"),
        )
        if val is None:
            continue

        if local_name not in concept_vals:
            concept_vals[local_name] = []
        concept_vals[local_name].append((ctx, val))

    return concept_vals


def _collect_ixbrl_by_attr(tree: etree._ElementTree) -> dict[str, list]:
    """
    Attribute-based iXBRL collection: find ANY element with
    name="ns:LocalName" and contextRef="..." regardless of tag name.

    This avoids the HTML-parser pitfall where <ix:nonFraction> becomes
    <ix:nonfraction> (lowercase), breaking local-name()='nonFraction' XPath.
    """
    concept_vals: dict[str, list] = {}
    try:
        # All elements carrying both name (with ":") and contextRef — the iXBRL value signature
        elements = tree.xpath("//*[@contextRef and @name[contains(., ':')]]")
    except Exception:
        return concept_vals

    for el in elements:
        name_attr = el.get("name", "")
        local_name = name_attr.split(":", 1)[-1] if ":" in name_attr else ""
        if not local_name:
            continue
        # Skip monetary-unit values when collecting share-count fields
        if local_name in _SHARE_LOCAL_NAMES:
            unit_ref = el.get("unitRef", "").upper()
            if any(c in unit_ref for c in _MONETARY_UNITS):
                continue
        ctx = el.get("contextRef", "")
        text = "".join(el.itertext()).strip()
        val = _text_to_float(text, scale_attr=el.get("scale"), sign_attr=el.get("sign"))
        if val is None:
            continue
        if local_name not in concept_vals:
            concept_vals[local_name] = []
        concept_vals[local_name].append((ctx, val))

    return concept_vals


def _build_fin_dict(concept_vals: dict[str, list]) -> dict:
    """Convert a concept-value map into the standardized fin dict."""
    fin: dict = {}

    # Prior-year context keywords — used to exclude comparison-period values
    # when selecting the "current" period for income/flow fields.
    _PRIOR_KWS = ("Prior1", "Prev", "Previous", "prior")

    def _cur_preferred(candidates: list) -> list:
        """Return candidates with prior-year contexts removed; fall back to all if none remain."""
        cur = [(c, v) for c, v in candidates
               if not any(k in c for k in _PRIOR_KWS)]
        return cur if cur else candidates

    for field, local_names in XBRL_ELEMENTS.items():
        if field == "buyback":
            continue
        is_additive = field in ADDITIVE_FIELDS

        if is_additive:
            total: Optional[float] = None
            seen: set = set()
            for name in local_names:
                for ctx, val in concept_vals.get(name, []):
                    key = (name, _is_consolidated(ctx))
                    if key in seen:
                        continue
                    seen.add(key)
                    total = (total or 0.0) + val
            fin[field] = total
        else:
            found_val = None
            for name in local_names:
                candidates = concept_vals.get(name, [])
                if not candidates:
                    continue
                preferred = _cur_preferred(candidates)
                cons = [(c, v) for c, v in preferred if _is_consolidated(c)]
                found_val = cons[0][1] if cons else preferred[0][1]
                break

            # Shares fallback: if not found via known names, search the entire
            # concept_vals for any key containing share-related keywords.
            # This handles element names not yet in XBRL_ELEMENTS["shares"].
            if found_val is None and field == "shares":
                _EXCL = _SHARE_EXCL_KWS
                best: Optional[float] = None
                best_name = ""
                for concept_name, vals in concept_vals.items():
                    if not any(kw in concept_name.lower() for kw in _SHARE_KWS):
                        continue
                    if any(ex in concept_name.lower() for ex in _EXCL):
                        continue
                    for _ctx, v in vals:
                        if v > 1000 and (best is None or v > best):
                            best = v
                            best_name = concept_name
                if best is not None:
                    logger.info("[shares] fallback used: %s = %s", best_name, best)
                    found_val = best

            fin[field] = found_val

    # Previous-year revenue
    fin["revenue_prev"] = None
    for name in XBRL_ELEMENTS["revenue_cur"]:
        for ctx, val in concept_vals.get(name, []):
            if any(k in ctx for k in ("Prior1", "Prev", "prior")):
                fin["revenue_prev"] = val
                break
        if fin["revenue_prev"] is not None:
            break

    # Buyback flag
    fin["has_buyback"] = False
    for name in XBRL_ELEMENTS["buyback"]:
        for ctx, val in concept_vals.get(name, []):
            if val is not None and val > 0:
                fin["has_buyback"] = True
                break

    # Flag when both debt fields are absent — net_cash may be overstated
    fin["debt_missing"] = fin.get("debt_short") is None and fin.get("debt_long") is None

    return fin


def _parse_document_bytes(data: bytes, fname: str) -> Optional[dict]:
    """
    Parse a single XBRL or iXBRL document, returning fin dict or None.

    Strategy order:
    1. Attribute-based iXBRL (works even when HTML parser lowercases tags)
    2. Tag-name-based iXBRL (original approach, works when XML parser succeeds)
    3. Pure XBRL element search by local name
    """
    # Try XML parser first; fall back to HTML parser
    try:
        root = etree.fromstring(data)
        used_html_parser = False
    except etree.XMLSyntaxError:
        try:
            root = etree.fromstring(data, parser=etree.HTMLParser())
            used_html_parser = True
        except Exception as exc:
            logger.warning("Cannot parse %s: %s", fname, exc)
            return None

    tree = root.getroottree()
    short = fname.split("/")[-1]

    # --- Strategy 1: attribute-based iXBRL (most robust) ---
    concept_vals = _collect_ixbrl_by_attr(tree)
    if concept_vals:
        _log_concepts(short, "attr-iXBRL", concept_vals)
        result = _build_fin_dict(concept_vals)
        if any(result.get(k) is not None for k in FIN_FIELDS):
            return result

    # --- Strategy 2: tag-name-based iXBRL (XML parse only) ---
    if not used_html_parser:
        nonfrac = tree.xpath("//*[local-name()='nonFraction']")
        if nonfrac:
            cv2 = _collect_ixbrl_values(nonfrac)
            _log_concepts(short, "tag-iXBRL", cv2)
            for k, v in cv2.items():
                if k not in concept_vals:
                    concept_vals[k] = v
            result = _build_fin_dict(concept_vals)
            if any(result.get(k) is not None for k in FIN_FIELDS):
                return result

    # --- Strategy 3: pure XBRL element local-name search ---
    cv3 = _collect_xbrl_values(tree)
    if cv3:
        _log_concepts(short, "pure-XBRL", cv3)
        for k, v in cv3.items():
            if k not in concept_vals:
                concept_vals[k] = v

    if not concept_vals:
        return None

    return _build_fin_dict(concept_vals)


def _log_concepts(fname: str, strategy: str, concept_vals: dict) -> None:
    """Log diagnostic info about found XBRL concepts, especially share-related ones."""
    share_kws = ("share", "issued", "outstanding", "株式", "発行")
    share_concepts = sorted(k for k in concept_vals if any(w in k.lower() for w in share_kws))
    logger.info("[XBRL/%s] %s: %d concepts, share-related=%s",
                strategy, fname, len(concept_vals), share_concepts or "NONE")
    if not share_concepts:
        # Show first 20 concepts so we can identify what element name to add
        logger.info("[XBRL/%s] %s: first 20 concepts: %s",
                    strategy, fname, sorted(concept_vals.keys())[:20])


def _parse_xbrl_zip(zip_bytes: bytes) -> Optional[dict]:
    """
    Open the EDINET document ZIP and extract financial data from the XBRL/iXBRL file.
    Returns a fin dict or None on failure.
    """
    # Verify it's actually a ZIP (magic bytes PK)
    if len(zip_bytes) < 4 or zip_bytes[:2] != b"PK":
        preview = zip_bytes[:300].decode("utf-8", errors="replace")
        logger.warning("Response is not a ZIP file. First 300 chars: %s", preview)
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            all_names = zf.namelist()
            logger.info("ZIP contents (%d files): %s", len(all_names), all_names)

            # Build candidate list (pure .xbrl first, then iXBRL .htm/.html/.xhtml)
            def score_name(n: str) -> int:
                n_low = n.lower()
                if n_low.endswith(".xbrl"):
                    return 0
                is_html = n_low.endswith(".htm") or n_low.endswith(".html") or n_low.endswith(".xhtml")
                if not is_html:
                    return 99
                if "ixbrl" in n_low:
                    return 1
                if "public" in n_low or "honbun" in n_low:
                    return 2
                return 3

            candidates = sorted(
                [n for n in all_names if score_name(n) < 99],
                key=score_name,
            )

            if not candidates:
                logger.warning("No XBRL/iXBRL files in ZIP. Files: %s", all_names[:20])
                return None

            logger.info("XBRL candidates (%d): %s", len(candidates), candidates[:10])

            # Parse ALL candidate files and merge results so that fields spread
            # across multiple documents (e.g. shares in cover page, cash in BS) are combined.
            merged: dict = {}
            for fname in candidates:
                try:
                    data = zf.read(fname)
                except KeyError:
                    continue
                result = _parse_document_bytes(data, fname)
                if not result:
                    continue
                for k, v in result.items():
                    if k == "has_buyback":
                        merged["has_buyback"] = merged.get("has_buyback", False) or bool(v)
                    elif merged.get(k) is None and v is not None:
                        merged[k] = v
                # Log progress
                have = [k for k in FIN_FIELDS if merged.get(k) is not None]
                logger.info("After %s: have %s", fname.split("/")[-1], have)

            if not merged or not any(merged.get(k) is not None for k in FIN_FIELDS):
                logger.warning("All XBRL candidates returned no usable financial data")
                return None
            if merged.get("shares") is None:
                excluded = [n for n in all_names if score_name(n) == 99]
                logger.warning(
                    "shares still None after parsing %d candidates. "
                    "Excluded files (%d): %s",
                    len(candidates), len(excluded), excluded[:20],
                )
            return merged

    except zipfile.BadZipFile as exc:
        logger.warning("Bad ZIP file: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Step 4: Fetch financials for a single document
# ---------------------------------------------------------------------------

def fetch_financials(api_key: str, doc_id: str) -> dict:
    """
    Download the document ZIP for `doc_id` and return parsed financials.

    Strategy:
      1. type=4 (財務書類XBRL) — financial statements only; fast, smaller ZIP.
         Gives cash/debt/P&L but NOT the cover page (表紙) where shares live.
      2. type=1 (全体) — full document package; larger but includes cover page
         with IssuedSharesTotalNumberOfSharesEtc.
    We always try type=4 first.  If shares is missing after type=4, we fetch
    type=1 to pick up the cover-page shares, then merge the two results.
    """
    time.sleep(DELAY_XBRL)

    merged: Optional[dict] = None

    for doc_type in (4, 1):
        try:
            resp = _edinet_request(f"documents/{doc_id}?type={doc_type}", api_key, stream=False, timeout=120)
        except Exception as exc:
            logger.warning("EDINET document fetch failed (doc=%s type=%d): %s", doc_id, doc_type, exc)
            continue

        content = resp.content
        if len(content) < 4 or content[:2] != b"PK":
            logger.debug("type=%d response for %s is not a ZIP (%d bytes)", doc_type, doc_id, len(content))
            continue

        logger.debug("Parsing doc %s with type=%d (%d bytes)", doc_id, doc_type, len(content))
        parsed = _parse_xbrl_zip(content)
        if not parsed:
            continue

        if merged is None:
            merged = dict(parsed)
        else:
            # Merge: fill in any fields still None from the earlier type
            for k, v in parsed.items():
                if k == "has_buyback":
                    merged["has_buyback"] = merged.get("has_buyback", False) or bool(v)
                elif merged.get(k) is None and v is not None:
                    merged[k] = v

        if merged.get("shares") is not None:
            logger.debug("Got shares from type=%d for %s", doc_type, doc_id)
            break  # shares found — no need for type=1

    if merged:
        if merged.get("shares") is None:
            logger.warning("shares still None after all fetch attempts for doc %s", doc_id)
        return merged
    return {}


# ---------------------------------------------------------------------------
# CLI helper for local testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        print("Set EDINET_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    test_codes = ["6501", "7203", "9984", "3382", "4755"]
    m = build_code_to_edinet_map(key, test_codes, days_back=30)
    for code, info in m.items():
        print(f"{code}: {info}")
        fin = fetch_financials(key, info["doc_id"])
        print(f"  → {fin}")
