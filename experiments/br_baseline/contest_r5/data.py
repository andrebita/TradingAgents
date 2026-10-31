"""Prices, corporate events, proventos and companies for the contest (protocol §2 and §3).

* Prices and traded value: B3 COTAHIST, spot market, round lot, as traded (not adjusted). Every company that
  traded is in it, including those later delisted or renamed.
* Companies and sectors: CVM FCA (code to CNPJ, sector by year). Codes the FCA lacks or files wrongly (BTG
  "000000", CSN "4030") join a company through the ISIN issuer code they share with a mapped code.
* Splits, groupings and bonus shares: Yahoo's split record when Yahoo has the code (or the same company's code of
  the same class under a new ticker); otherwise detected in COTAHIST: a one-day price ratio within 15% of a simple
  factor, confirmed by the traded quantity moving the opposite way.
* Proventos: Yahoo's per-share amounts by ex-date, brought back to the shares of that date. Codes Yahoo never had
  (most delisted ones) have none: their returns are understated, and the dividend rule cannot rank them.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from dividend_tilt import fca, sector_at  # noqa: E402

CACHE = HERE / "results" / "dividends" / "cache"
OUT = HERE / "results" / "contest_r5"
FACTORS = np.array([1.1, 1.2, 1.25, 1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100])


# Codes the FCA does not list for their company (or lists wrongly), found by the company's name in the FCA's
# general files and fixed before any result. ALL was renamed Rumo S.A.; B3 kept BM&FBovespa's CNPJ.
MANUAL_CNPJ = {
    "AEDU3": "04.310.392/0001-46", "ALLL3": "02.387.241/0001-60", "RUMO3": "02.387.241/0001-60",
    "B3SA3": "09.346.601/0001-25", "BVMF3": "09.346.601/0001-25", "BBTG11": "15.073.274/0001-88",
    "BPAC11": "30.306.294/0001-45", "BRPR3": "06.977.751/0001-49", "CRUZ3": "33.009.911/0001-39",
    "CSNA3": "33.042.730/0001-04", "CTIP3": "09.358.105/0001-91", "KROT3": "02.800.026/0001-40",
    "COGN3": "02.800.026/0001-40", "MBRF3": "03.853.896/0001-40", "SMLE3": "15.912.764/0001-20",
    "TBLE3": "02.474.103/0001-19", "EGIE3": "02.474.103/0001-19",
}


def load_spot(path: Path) -> pd.DataFrame:
    """COTAHIST spot rows; ``stocks`` are shares and units of companies (ISIN type ACN or CDA), not funds."""
    spot = pd.read_csv(path, parse_dates=["date"], dtype={"codbdi": str, "dismes": str})
    spot["kind"] = spot["isin"].str[6:9]
    return spot


def stocks_of(spot: pd.DataFrame) -> pd.DataFrame:
    return spot[(spot["codbdi"] == "02") & spot["kind"].isin(["ACN", "CDA"])]


def companies(stocks: pd.DataFrame) -> tuple[dict[str, str], dict[str, dict[int, str]]]:
    """``{code: company}`` and ``{company: {year: sector}}``."""
    codes, sectors = fca(CACHE)
    company = {c: cnpj for cnpj, cs in codes.items() for c in cs}
    issuer = stocks.drop_duplicates("code").set_index("code")["isin"].str[2:6]
    by_issuer = {}
    company.update(MANUAL_CNPJ)
    for code, root in issuer.items():
        if code in company:
            by_issuer.setdefault(root, company[code])
    out = {}
    for code, root in issuer.items():
        out[code] = company.get(code) or by_issuer.get(root) or f"ISIN:{root}"
    return out, sectors


def sector_of(company: str, sectors: dict, year: int) -> str | None:
    s = sector_at(sectors.get(company, {}), year)
    if not s:
        return None
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def yahoo_actions(codes: list[str], cache: Path) -> dict[str, pd.DataFrame]:
    """Close, Dividends and Stock Splits per code from Yahoo (unadjusted for dividends), cached."""
    folder = cache / "yahoo_actions"
    folder.mkdir(parents=True, exist_ok=True)
    missing = [c for c in codes if not (folder / f"{c}.csv").exists()]
    for i in range(0, len(missing), 30):
        batch = missing[i:i + 30]
        for attempt in range(4):
            try:
                data = yf.download([f"{c}.SA" for c in batch], start="2008-01-01", end="2026-10-31",
                                   auto_adjust=False, actions=True, group_by="ticker", threads=True,
                                   progress=False)
                break
            except Exception:
                time.sleep(5 * 2 ** attempt)
        else:
            continue
        for code in batch:
            try:
                frame = data[f"{code}.SA"][["Close", "Dividends", "Stock Splits"]].dropna(subset=["Close"])
            except KeyError:
                frame = pd.DataFrame(columns=["Close", "Dividends", "Stock Splits"])
            frame.to_csv(folder / f"{code}.csv")
        time.sleep(2)
    out = {}
    for code in codes:
        frame = pd.read_csv(folder / f"{code}.csv", index_col=0, parse_dates=True)
        if len(frame):
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            out[code] = frame
    return out


def detect_splits(close: pd.Series, quantity: pd.Series) -> dict[pd.Timestamp, float]:
    """``{date: share factor}`` (2.0: each share became two) from a code's own prices and traded quantities."""
    out = {}
    c = close.dropna()
    ratio = c / c.shift(1)
    for day, r in ratio[(ratio < 0.67) | (ratio > 1.5)].items():
        f = 1 / r                                   # shares multiply by f when the price divides by f
        candidates = np.concatenate([FACTORS, 1 / FACTORS])
        best = candidates[np.argmin(np.abs(np.log(candidates) - np.log(f)))]
        if abs(f / best - 1) > 0.15:
            continue
        q = quantity.dropna()
        before, after = q.loc[:day].iloc[-22:-1].median(), q.loc[day:].iloc[:21].median()
        if not (before > 0 and after > 0):
            continue
        moved = after / before if best > 1 else before / after       # quantity moves against the price
        if moved > min(max(best, 1 / best), 3.0) ** 0.5:
            out[day] = float(best)
    return out


def events(code: str, close: pd.Series, quantity: pd.Series, yahoo: pd.DataFrame | None) -> tuple[dict, pd.Series]:
    """Share factors by date and raw proventos per share by ex-date for one code."""
    if yahoo is not None and len(yahoo):
        splits = {d: float(v) for d, v in yahoo["Stock Splits"].items() if v and v > 0 and v != 1}
        div = yahoo["Dividends"][yahoo["Dividends"] > 0]
        # Yahoo restates past proventos in today's shares: bring each back to the shares of its date.
        later = pd.Series(splits).sort_index() if splits else pd.Series(dtype=float)
        raw = {d: v * float(later[later.index > d].prod()) if len(later) else v for d, v in div.items()}
        return splits, pd.Series(raw, dtype=float).sort_index()
    return detect_splits(close, quantity), pd.Series(dtype=float)
