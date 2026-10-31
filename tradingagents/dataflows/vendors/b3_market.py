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
import requests
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.vendors import cvm

logger = logging.getLogger(__name__)

# A share or unit code: four letters and the class (PETR4, TAEE11).
CODE = re.compile(r"^[A-Z]{4}(3|4|5|6|11)$")
# A one-day rise above 100% or fall beyond 90% in a large B3 stock is either a misadjusted
# corporate action or a real event. When the price undoes it within ``REVERSAL_SESSIONS``
# (back within 25% of where it was), it was a data error: both days' returns are set to
# zero (Azul's 2026 reverse splits). When it stays, it is kept as it is and flagged as
# suspect: only a check against the event can tell a misadjusted spin-off (PCAR3 on
# 2021-03-01) from a real crash, so the routine keeps such a stock out of new purchases.
MAX_RISE, MAX_FALL = 1.0, -0.9
REVERSAL_SESSIONS = 10
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


def clean(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str], list[str]]:
    """``frame`` with reversed spikes in Adj Close neutralized, the days changed, and the suspect days.

    The suspect days are also left in ``frame.attrs["suspect_days"]``.
    """
    adj = frame["Adj Close"]
    returns = adj.pct_change()
    extreme = returns[(returns > MAX_RISE) | (returns < MAX_FALL)].index
    neutral: set = set()
    suspect = []
    for day in extreme:
        if day in neutral:
            continue
        i = adj.index.get_loc(day)
        before = adj.iloc[i - 1]
        later = adj.iloc[i + 1:i + 1 + REVERSAL_SESSIONS]
        back = later[(later / before).between(0.75, 1 / 0.75)]
        if len(back):
            neutral |= {day, back.index[0]}
        else:
            suspect.append(day.date().isoformat())
    fixed = frame.copy()
    if neutral:
        returns.loc[sorted(neutral)] = 0.0
        fixed["Adj Close"] = adj.iloc[0] * (1 + returns.fillna(0)).cumprod()
    fixed.attrs["suspect_days"] = suspect
    return fixed, sorted(d.date().isoformat() for d in neutral), suspect


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
            out[code], _, _ = clean(frame.sort_index())
    return out


def index_series(symbol: str, start: str = "2008-01-01") -> pd.Series:
    """Adjusted closes of an index or ETF (``^BVSP``, ``BOVA11.SA``, ``DIVO11.SA``), cached."""
    path = _folder() / f"{symbol.strip('^').replace('.', '_')}.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > PRICE_TTL:
        frame = yf.Ticker(symbol).history(start=start, auto_adjust=True)
        frame.index = frame.index.tz_localize(None).normalize()
        frame[["Close"]].to_csv(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)["Close"].sort_index()


def sgs_series(series: int, start_year: int = 2010) -> pd.Series:
    """A Banco Central SGS series (e.g. 12, the daily CDI in % a day), cached for ``PRICE_TTL``."""
    path = _folder() / f"sgs_{series}.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > PRICE_TTL:
        rows = []
        for year in range(start_year, date.today().year + 1):   # the API caps a request's span
            url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados?formato=json"
                   f"&dataInicial=01/01/{year}&dataFinal=31/12/{year}")
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            rows += response.json()
        pd.Series({pd.to_datetime(r["data"], dayfirst=True): float(r["valor"]) for r in rows}).sort_index() \
            .to_csv(path, header=["value"])
    return pd.read_csv(path, index_col=0, parse_dates=True)["value"]
