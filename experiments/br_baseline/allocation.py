"""Stocks or CDI by valuation: hold stocks only when their yield beats the real rate by more than usual.

At each month-end the spread is the median 24-month dividend yield of the 50 most
traded B3 companies (``dividend_tilt.py``'s rules, cleaned prices) minus the real
rate (Selic target less the latest released 12-month IPCA). Above its expanding
median (from 36 months of history) the next month is held in stocks, otherwise
in CDI, with 0.1% per switch. The design is pre-registered in the README ("Ações
ou CDI pelo valuation").

Usage:
    python experiments/br_baseline/allocation.py [--out experiments/br_baseline/results/allocation]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import dividend_tilt as tilt
import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
SWITCH_COST = 0.001
MIN_HISTORY = 36
IPCA_LAG_DAYS = 12


def sgs(series: int, cache: Path) -> pd.Series:
    """A BCB SGS series, cached, fetched year by year (the API caps a request's span)."""
    path = cache / f"sgs_{series}.csv"
    if not path.exists():
        rows = []
        for year in range(2008, 2027):
            url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados?formato=json"
                   f"&dataInicial=01/01/{year}&dataFinal=31/12/{year}")
            rows += requests.get(url, timeout=60).json()
        pd.Series({pd.to_datetime(r["data"], dayfirst=True): float(r["valor"]) for r in rows}).sort_index() \
            .to_csv(path, header=["value"])
    return pd.read_csv(path, index_col=0, parse_dates=True)["value"]


def stats(r: pd.Series, ref: pd.Series) -> dict:
    def cagr(x):
        return float((1 + x).prod() ** (12 / len(x)) - 1)

    def dd(x):
        eq = (1 + x).cumprod()
        return float((eq / eq.cummax() - 1).min())

    d = r - ref
    roll = ((1 + r).rolling(36).apply(np.prod, raw=True) - (1 + ref).rolling(36).apply(np.prod, raw=True)).dropna()
    yearly = (1 + r).groupby(r.index.year).prod() - (1 + ref).groupby(ref.index.year).prod()
    return {"cagr": cagr(r), "cagr_ref": cagr(ref), "max_drawdown": dd(r), "max_drawdown_ref": dd(ref),
            "mean_monthly_excess": float(d.mean()), "t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))),
            "rolling36_positive": float((roll > 0).mean()), "months": len(d),
            "by_year": {int(y): round(float(v), 4) for y, v in yearly.items()}}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "allocation"))
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = HERE / "results" / "dividends" / "cache"

    codes, _ = tilt.fca(cache)
    px = tilt.prices(set().union(*codes.values()), cache)
    bvsp = tilt.series("^BVSP", cache)
    sessions = bvsp.loc["2008-01-01":].index
    ends = sessions.to_series().groupby(sessions.to_period("M")).max()
    ends = ends[(ends >= pd.Timestamp("2010-01-01")) & (ends <= pd.Timestamp("2026-09-30"))]

    selic, ipca12, cdi = sgs(432, cache), sgs(13522, cache), sgs(12, cache)
    rows = []
    for t in ends:
        snap = tilt.snapshot(codes, px, t, 50)
        known = ipca12[ipca12.index + pd.offsets.MonthEnd(0) + pd.Timedelta(days=IPCA_LAG_DAYS) <= t]
        real = float(selic.loc[:t].iloc[-1]) - float(known.iloc[-1])
        rows.append({"t": t, "equity_yield": 100 * float(snap["dy24"].median()), "real_rate": real})
    sig = pd.DataFrame(rows).set_index("t")
    sig["spread"] = sig["equity_yield"] - sig["real_rate"]
    sig["threshold"] = sig["spread"].expanding(MIN_HISTORY).median().shift(0)
    sig["in_stocks"] = sig["spread"] > sig["threshold"]
    sig = sig.dropna(subset=["threshold"])

    # Monthly returns of the month after each signal: index, CDI, the dividend portfolio.
    month = pd.PeriodIndex(sig.index, freq="M") + 1
    cdi_m = (1 + cdi / 100).groupby(cdi.index.to_period("M")).prod() - 1
    ibov_m = bvsp.groupby(bvsp.index.to_period("M")).last().pct_change()
    div = pd.read_csv(HERE / "results" / "dividends" / "curve_e_semiannual.csv", index_col=0, parse_dates=True)["value"]
    div_m = div.groupby(div.index.to_period("M")).last().pct_change()
    frame = pd.DataFrame({"in_stocks": sig["in_stocks"].to_numpy(), "spread": sig["spread"].to_numpy()},
                         index=month)
    frame["cdi"], frame["ibov"], frame["div"] = cdi_m.reindex(month), ibov_m.reindex(month), div_m.reindex(month)
    frame = frame.dropna(subset=["cdi", "ibov"])
    switch = frame["in_stocks"].ne(frame["in_stocks"].shift()).astype(float) * SWITCH_COST
    switch.iloc[0] = 0.0
    frame["alloc_ibov"] = frame["ibov"].where(frame["in_stocks"], frame["cdi"]) - switch
    frame["alloc_div"] = frame["div"].where(frame["in_stocks"], frame["cdi"]) - switch
    frame.index = frame.index.to_timestamp(how="end").normalize()

    report = {"signal_months": int(len(frame)), "share_in_stocks": float(frame["in_stocks"].mean()),
              "switches": int(frame["in_stocks"].ne(frame["in_stocks"].shift()).sum() - 1),
              "A_ibov_or_cdi_vs_ibov": stats(frame["alloc_ibov"], frame["ibov"]),
              "cdi_vs_ibov": stats(frame["cdi"], frame["ibov"])}
    d = frame.dropna(subset=["div"])
    report["B_dividends_or_cdi_vs_dividends"] = stats(d["alloc_div"], d["div"])
    a = report["A_ibov_or_cdi_vs_ibov"]
    report["passes"] = bool(a["cagr"] > a["cagr_ref"] and a["max_drawdown"] > a["max_drawdown_ref"]
                            and a["rolling36_positive"] >= 0.6 and a["t"] >= 1.65)
    sig.to_csv(out / "signal.csv")
    frame.to_csv(out / "monthly.csv")
    text = json.dumps(report, indent=2)
    (out / "allocation_report.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
