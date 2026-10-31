"""Which stocks the portfolio should hold: the rule the backtests tested."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from tradingagents.dividend_portfolio.market import Company


@dataclass(frozen=True)
class Rules:
    universe: int = 50                       # most traded companies considered
    holdings: int = 10                       # stocks held, equally weighted
    paid_years: int = 3                      # dividends in each of these last 12-month periods
    yield_months: int = 24                   # dividends averaged over this window for the yield
    rebalance_months: tuple[int, ...] = (6, 12)
    liquidity_sessions: int = 63             # sessions for the median traded value
    excluded_sectors: tuple[str, ...] = ()   # the investor's exclusions, matched within the CVM sector
    suspect_days: int = 365                  # a price jump not yet explained keeps a code out this long


DEFAULT_RULES = Rules()


def snapshot(companies: dict[str, Company], px: dict[str, pd.DataFrame], as_of: pd.Timestamp,
             rules: Rules = DEFAULT_RULES) -> pd.DataFrame:
    """The ``rules.universe`` most traded companies at ``as_of``, by their most traded code.

    Columns: cnpj, name, sector, code, close, traded (median R$ a day), dy12, dy24
    (dividends per year over the window, over the close) and constant (paid in each
    of the last ``paid_years`` years). Only prices up to ``as_of`` are read.
    """
    rows = []
    for cnpj, company in companies.items():
        best = None
        for code in company.codes:
            frame = px.get(code)
            if frame is None or is_suspect(frame, as_of, rules):
                continue
            window = frame.loc[:as_of].tail(rules.liquidity_sessions)
            if len(window) < rules.liquidity_sessions * 2 // 3 or window.index[-1] < as_of - pd.Timedelta(days=10):
                continue
            traded = float((window["Close"] * window["Volume"]).median())
            if best is None or traded > best[1]:
                best = (code, traded)
        if best is None:
            continue
        frame = px[best[0]]
        close = float(frame["Close"].loc[:as_of].iloc[-1])
        div = frame["Dividends"].loc[:as_of]

        def paid(k: int, div=div) -> float:
            return float(div[(div.index > as_of - pd.DateOffset(months=12 * (k + 1)))
                             & (div.index <= as_of - pd.DateOffset(months=12 * k))].sum())

        years = [paid(k) for k in range(max(rules.paid_years, rules.yield_months // 12))]
        rows.append({"cnpj": cnpj, "name": company.name, "sector": company.sector, "code": best[0],
                     "close": close, "traded": best[1], "dy12": years[0] / close,
                     "dy24": sum(years[:rules.yield_months // 12]) / (rules.yield_months // 12) / close,
                     "constant": all(p > 0 for p in years[:rules.paid_years])})
    frame = pd.DataFrame(rows)
    return frame.nlargest(rules.universe, "traded").reset_index(drop=True) if len(frame) else frame


def is_suspect(frame: pd.DataFrame, as_of: pd.Timestamp, rules: Rules = DEFAULT_RULES) -> bool:
    """A one-sided extreme price move (see ``b3_market.clean``) within ``rules.suspect_days`` of ``as_of``."""
    return any(as_of - pd.Timedelta(days=rules.suspect_days) < pd.Timestamp(d) <= as_of
               for d in frame.attrs.get("suspect_days", ()))


def is_excluded(sector, rules: Rules = DEFAULT_RULES) -> bool:
    """The investor excluded ``sector`` (a case-insensitive part of the CVM sector name)."""
    sector = sector.lower() if isinstance(sector, str) else ""
    return any(x.lower() in sector for x in rules.excluded_sectors if x.strip())


def target(snap: pd.DataFrame, rules: Rules = DEFAULT_RULES) -> pd.DataFrame:
    """The stocks to hold: the highest 24-month yields among the steady payers outside the excluded sectors."""
    if snap.empty:
        return snap
    allowed = ~snap["sector"].map(lambda s: is_excluded(s, rules))
    return snap[snap["constant"] & allowed].nlargest(rules.holdings, "dy24").reset_index(drop=True)


def is_rebalance_month(day: pd.Timestamp, rules: Rules = DEFAULT_RULES) -> bool:
    return day.month in rules.rebalance_months
