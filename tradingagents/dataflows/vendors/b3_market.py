"""B3 companies, their trading codes and sectors (CVM FCA), and their prices and dividends (Yahoo).

The market data of the dividend portfolio routine (``tradingagents.dividend_portfolio``).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.vendors import cvm

logger = logging.getLogger(__name__)

# A share or unit code: four letters and the class (PETR4, TAEE11).
CODE = re.compile(r"^[A-Z]{4}(3|4|5|6|11)$")
# A one-day rise above 100% or fall beyond 90% in a large B3 stock is a corporate action
# Yahoo misadjusted (the Assai spin-off in PCAR3, Azul's 2026 reverse splits), not a
# market move: that day's return is set to zero. Real crashes such as Americanas' -77%
# stay.
MAX_RISE, MAX_FALL = 1.0, -0.9
PRICE_TTL = 12 * 60 * 60
COLUMNS = ["Close", "Adj Close", "Volume", "Dividends"]


@dataclass
class Company:
    cnpj: str
    name: str = ""
    codes: set[str] = field(default_factory=set)
    sector: str = ""


def companies(years: range | None = None) -> dict[str, Company]:
    """``{cnpj: Company}`` from the CVM FCA forms of ``years`` (the last three by default).

    The sector of an investment holding ("Emp. Adm. Part. - Energia Elétrica") is its
    sector; the latest year's name and sector win.
    """
    years = years or range(date.today().year - 2, date.today().year + 1)
    out: dict[str, Company] = {}
    for year in years:
        path = cvm._cached_zip(cvm._FCA_URL.format(year=year), f"fca_cia_aberta_{year}.zip", year)
        if path is None:
            continue
        for row in cvm._rows(path, f"valor_mobiliario_{year}.csv"):
            code = (row.get("Codigo_Negociacao") or "").strip().upper()
            if row.get("Mercado") == "Bolsa" and CODE.match(code):
                out.setdefault(row["CNPJ_Companhia"], Company(row["CNPJ_Companhia"])).codes.add(code)
        for row in cvm._rows(path, f"geral_{year}.csv"):
            company = out.get(row["CNPJ_Companhia"])
            if company is not None:
                company.name = row.get("Nome_Empresarial") or company.name
                company.sector = (row.get("Setor_Atividade") or "").removeprefix("Emp. Adm. Part. - ").strip()
    return out


def clean(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """``frame`` with misadjusted one-day jumps in Adj Close neutralized, and the days changed."""
    returns = frame["Adj Close"].pct_change()
    bad = returns[(returns > MAX_RISE) | (returns < MAX_FALL)].index
    if not len(bad):
        return frame, []
    returns.loc[bad] = 0.0
    fixed = frame.copy()
    fixed["Adj Close"] = frame["Adj Close"].iloc[0] * (1 + returns.fillna(0)).cumprod()
    return fixed, [d.date().isoformat() for d in bad]


def _folder() -> Path:
    path = Path(get_config()["data_cache_dir"]) / "b3_prices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def histories(codes: set[str], start: str = "2008-01-01") -> dict[str, pd.DataFrame]:
    """Daily Close, Adj Close, Volume and Dividends per code, cached for ``PRICE_TTL``.

    Codes Yahoo does not serve are left out. Adj Close is cleaned (see ``MAX_RISE``).
    """
    folder = _folder()
    stale = sorted(c for c in codes if not (folder / f"{c}.csv").exists()
                   or time.time() - (folder / f"{c}.csv").stat().st_mtime > PRICE_TTL)
    for i in range(0, len(stale), 40):
        batch = stale[i:i + 40]
        data = None
        for attempt in range(4):
            try:
                data = yf.download([f"{c}.SA" for c in batch], start=start, auto_adjust=False, actions=True,
                                   group_by="ticker", threads=True, progress=False)
                break
            except Exception as exc:  # yfinance raises many types on throttling
                logger.warning("Yahoo download failed (%s); retrying", exc)
                time.sleep(5 * 2 ** attempt)
        if data is None:
            continue
        for code in batch:
            try:
                frame = data[f"{code}.SA"][COLUMNS].dropna(subset=["Adj Close"])
            except KeyError:
                frame = pd.DataFrame(columns=COLUMNS)
            frame.to_csv(folder / f"{code}.csv")
    out = {}
    for code in codes:
        path = folder / f"{code}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(frame):
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            out[code], _ = clean(frame.sort_index())
    return out


def index_series(symbol: str, start: str = "2008-01-01") -> pd.Series:
    """Adjusted closes of an index or ETF (``^BVSP``, ``BOVA11.SA``, ``DIVO11.SA``), cached."""
    path = _folder() / f"{symbol.strip('^').replace('.', '_')}.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > PRICE_TTL:
        frame = yf.Ticker(symbol).history(start=start, auto_adjust=True)
        frame.index = frame.index.tz_localize(None).normalize()
        frame[["Close"]].to_csv(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)["Close"].sort_index()
