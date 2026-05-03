"""
calculator.py — Net cash metrics, VT signal detection, scoring, and note generation.
All monetary values from EDINET are in raw JPY; market_cap is in 億円.
"""

from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# Net cash and core metrics
# ---------------------------------------------------------------------------

def calc_net_cash_oku(fin: dict) -> Optional[float]:
    """
    Net cash (億円) = (流動資産 + 投資有価証券×70% − 負債) / 1e8
    清原達郎式: NC比率 = ネットキャッシュ ÷ 時価総額
    Returns None if essential balance-sheet data is missing.
    """
    current_assets = fin.get("current_assets")
    if current_assets is None:
        return None
    invest_sec = fin.get("securities") or 0.0
    total_liabilities = fin.get("total_liabilities") or 0.0
    net_cash_jpy = current_assets + invest_sec * 0.7 - total_liabilities
    return round(net_cash_jpy / 1e8, 1)


def calc_metrics(jpx_data: dict, fin: dict) -> dict:
    """
    Combine JPX market data with EDINET financial data to produce all metrics.
    Returns a dict with keys: net_cash, net_cash_ratio, pbr, per, eps, roe, roa, div_yield.
    Missing/uncalculable values are None (or 0 for div_yield).
    """
    price = jpx_data["price"]          # JPY
    market_cap = jpx_data["market_cap"]  # 億円

    # Prefer EDINET shares for per-share ratios; fall back to JPX-derived shares
    shares = fin.get("shares") or jpx_data.get("shares")

    # --- Net cash ---
    net_cash = calc_net_cash_oku(fin)
    if net_cash is not None and market_cap and market_cap > 0:
        net_cash_ratio = round(net_cash / market_cap, 2)
    else:
        net_cash_ratio = None

    # --- PBR ---
    net_assets = fin.get("net_assets")
    pbr = None
    if net_assets and shares and shares > 0:
        bps = net_assets / shares
        if bps > 0:
            pbr = round(price / bps, 2)

    # --- EPS ---
    net_profit = fin.get("net_profit")
    eps = None
    if net_profit is not None and shares and shares > 0:
        eps = round(net_profit / shares, 2)

    # --- PER (None if loss-making) ---
    per = None
    if eps is not None and eps > 0:
        per = round(price / eps, 1)

    # --- ROE ---
    roe = None
    if net_profit is not None and net_assets and net_assets > 0:
        roe = round((net_profit / net_assets) * 100, 1)

    # --- ROA ---
    total_assets = fin.get("total_assets")
    roa = None
    if net_profit is not None and total_assets and total_assets > 0:
        roa = round((net_profit / total_assets) * 100, 1)

    # --- Dividend yield ---
    div_per_share = fin.get("div_per_share") or 0.0
    div_yield = round((div_per_share / price) * 100, 1) if price > 0 else 0.0

    # --- Operating CF (億円) ---
    op_cf_jpy = fin.get("operating_cf")
    operating_cf = round(op_cf_jpy / 1e8, 1) if op_cf_jpy is not None else None

    # --- Revenue growth rate (%) ---
    rev_cur = fin.get("revenue_cur")
    rev_prev = fin.get("revenue_prev")
    revenue_growth = None
    if rev_cur is not None and rev_prev is not None and rev_prev > 0:
        revenue_growth = round((rev_cur - rev_prev) / rev_prev * 100, 1)

    return {
        "net_cash": net_cash,
        "net_cash_ratio": net_cash_ratio,
        "pbr": pbr,
        "per": per,
        "eps": eps,
        "roe": roe,
        "roa": roa,
        "div_yield": div_yield,
        "operating_cf": operating_cf,
        "revenue_growth": revenue_growth,
    }


# ---------------------------------------------------------------------------
# Value-trap (VT) signal detection
# ---------------------------------------------------------------------------

def detect_vt_signals(fin: dict, metrics: dict) -> list[str]:
    """
    Detect value-trap warning signals. Returns a list of signal keys matching
    the VT_LABELS defined in index.html.
    """
    signals: list[str] = []

    roe = metrics.get("roe")
    if roe is not None:
        if roe < 0:
            signals.append("negative_roe")
        elif roe < 8.0:
            signals.append("low_roe")

    rev_cur = fin.get("revenue_cur")
    rev_prev = fin.get("revenue_prev")
    if rev_cur is not None and rev_prev is not None and rev_prev > 0:
        if rev_cur < rev_prev:
            signals.append("revenue_decline")

    op_cf = fin.get("operating_cf")
    if op_cf is not None and op_cf < 0:
        signals.append("negative_cf")

    div_per_share = fin.get("div_per_share") or 0.0
    has_buyback = fin.get("has_buyback", False)
    if div_per_share == 0 and not has_buyback:
        signals.append("no_return")

    pbr = metrics.get("pbr")
    if pbr is not None and pbr < 0.3:
        signals.append("low_pbr")

    if fin.get("debt_missing"):
        signals.append("debt_unconfirmed")

    return signals


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_nc_ratio(ncr: Optional[float]) -> int:
    """NC比率 component — max 40 pts."""
    if ncr is None or ncr < 0:
        return 0
    if ncr >= 3.0:
        return 40
    if ncr >= 2.5:
        return 35
    if ncr >= 2.0:
        return 30
    if ncr >= 1.5:
        return 25
    if ncr >= 1.0:
        return 18
    if ncr >= 0.5:
        return 10
    return 5  # 0.1x–0.5x — included but low


def _score_pbr(pbr: Optional[float]) -> int:
    """PBR component — max 20 pts."""
    if pbr is None:
        return 0
    if pbr < 0.3:
        return 20
    if pbr < 0.5:
        return 17
    if pbr < 0.7:
        return 14
    if pbr < 1.0:
        return 10
    if pbr < 1.5:
        return 5
    return 0


def _score_per(per: Optional[float]) -> int:
    """PER component — max 15 pts. None (red ink) = 0."""
    if per is None:
        return 0
    if per < 5:
        return 15
    if per < 8:
        return 12
    if per < 11:
        return 9
    if per < 15:
        return 5
    if per < 20:
        return 2
    return 0


def _score_surprise(surprise: Optional[str], surprise_pct: int) -> int:
    """Surprise component — max 15 pts."""
    if surprise == "positive":
        return min(15, 10 + surprise_pct * 2)
    return 0  # negative or None


def _score_market_cap(market_cap_oku: float) -> int:
    """Market cap component — max 10 pts. Prefers small caps."""
    if market_cap_oku < 10:
        return 10
    if market_cap_oku < 30:
        return 9
    if market_cap_oku < 50:
        return 7
    if market_cap_oku < 100:
        return 5
    if market_cap_oku < 300:
        return 3
    if market_cap_oku < 500:
        return 1
    return 0


def calc_score(
    ncr: Optional[float],
    pbr: Optional[float],
    per: Optional[float],
    surprise: Optional[str],
    surprise_pct: int,
    market_cap: float,
    vt_signals: list[str],
) -> int:
    """Composite score 0–100. VT signals penalise −3 pts each, max −15."""
    raw = (
        _score_nc_ratio(ncr)
        + _score_pbr(pbr)
        + _score_per(per)
        + _score_surprise(surprise, surprise_pct)
        + _score_market_cap(market_cap)
    )
    penalty = min(15, len(vt_signals) * 3)
    return max(0, min(100, raw - penalty))


# ---------------------------------------------------------------------------
# Investment note generation
# ---------------------------------------------------------------------------

def generate_note(metrics: dict, vt_signals: list[str], sector: str) -> str:
    """Short Japanese comment summarising the stock's key characteristics."""
    parts: list[str] = []

    ncr = metrics.get("net_cash_ratio")
    if ncr is not None:
        if ncr >= 2.0:
            parts.append(f"NC比率{ncr:.1f}倍と現金が極めて豊富")
        elif ncr >= 1.0:
            parts.append(f"NC比率{ncr:.1f}倍と現金豊富")

    roe = metrics.get("roe")
    if roe is not None and roe >= 8:
        parts.append(f"ROE{roe:.1f}%で収益力あり")
    elif roe is not None and roe < 0:
        parts.append("当期赤字に注意")

    pbr = metrics.get("pbr")
    if pbr is not None and pbr < 0.6:
        parts.append(f"PBR{pbr:.2f}倍と解散価値以下")

    div_yield = metrics.get("div_yield") or 0.0
    if div_yield >= 3.0:
        parts.append(f"配当利回り{div_yield:.1f}%")

    if not vt_signals:
        parts.append("VTシグナルなし")
    elif len(vt_signals) >= 3:
        parts.append(f"VT警告{len(vt_signals)}件に注意")

    if parts:
        return "。".join(parts) + "。"
    return f"{sector}セクター銘柄。"
