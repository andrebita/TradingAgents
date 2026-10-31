"""Dividend tilt among the largest B3 stocks: does a rule-built portfolio beat the Ibovespa and DIVO11?

No LLM. Yahoo gives prices, volume and dividends (by ex-date) from 2008; CVM's
FCA gives each company's trading codes and sector. At each quarterly rebalance
the universe is the 50 most traded companies; among those that paid in each of
the three 12-month periods before the date, the 10 with the highest dividend
yield (24 months of dividends, halved, over the close) are held equally
weighted, entered at the next session's close and left to drift until the next
rebalance. The design is pre-registered in the README ("Dividendos entre as
grandes").

Usage:
    python experiments/br_baseline/dividend_tilt.py [--out experiments/br_baseline/results/dividends]
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from insiders import CODE

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import cvm
from tradingagents.default_config import DEFAULT_CONFIG

HERE = Path(__file__).resolve().parent
YEARS = range(2010, 2027)
FIRST, LAST = "2012-03-01", "2026-06-30"    # rebalance dates (pre-registered)
COST = 0.0015
BEST = ("Bancos", "Intermediação Financeira", "Energia Elétrica", "Saneamento, Serv. Água e Gás",
        "Seguradoras e Corretoras", "Telecomunicações")


def fca(cache: Path) -> tuple[dict[str, set[str]], dict[str, dict[int, str]]]:
    """``{cnpj: codes}`` from every FCA year, and ``{cnpj: {year: sector}}``."""
    codes: dict[str, set[str]] = {}
    sectors: dict[str, dict[int, str]] = {}
    with run_config({**copy.deepcopy(DEFAULT_CONFIG), "data_cache_dir": str(cache)}):
        for year in YEARS:
            path = cvm._cached_zip(cvm._FCA_URL.format(year=year), f"fca_cia_aberta_{year}.zip", year)
            if path is None:
                continue
            for row in cvm._rows(path, f"valor_mobiliario_{year}.csv"):
                code = (row.get("Codigo_Negociacao") or "").strip().upper()
                if row.get("Mercado") == "Bolsa" and CODE.match(code):
                    codes.setdefault(row["CNPJ_Companhia"], set()).add(code)
            for row in cvm._rows(path, f"geral_{year}.csv"):
                # "Emp. Adm. Part. - Energia Elétrica" is a holding of the sector: same sector.
                sector = (row.get("Setor_Atividade") or "").removeprefix("Emp. Adm. Part. - ").strip()
                sectors.setdefault(row["CNPJ_Companhia"], {})[year] = sector
    return codes, sectors


def sector_at(sectors: dict[int, str], year: int) -> str:
    known = [y for y in sectors if y <= year]
    return sectors[max(known)] if known else ""


# A one-day rise above 100% or fall beyond 90% in a large B3 stock is a corporate action
# Yahoo misadjusted (the Assaí spin-off in PCAR3 on 2021-03-01, Azul's 2026 reverse
# splits), not a market move: that day's return is set to zero. Real crashes, such as
# Americanas' -77% in January 2023, stay. Added after the first run (see the README).
MAX_RISE, MAX_FALL = 1.0, -0.9


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


def prices(codes: set[str], cache: Path) -> dict[str, pd.DataFrame]:
    """Daily Close, Adj Close, Volume and Dividends per code from Yahoo, cached; missing codes left out."""
    folder = cache / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    missing = sorted(c for c in codes if not (folder / f"{c}.csv").exists())
    for i in range(0, len(missing), 40):
        batch = missing[i:i + 40]
        for attempt in range(4):
            try:
                data = yf.download([f"{c}.SA" for c in batch], start="2008-01-01", end="2026-10-31",
                                   auto_adjust=False, actions=True, group_by="ticker", threads=True, progress=False)
                break
            except Exception:
                time.sleep(5 * 2 ** attempt)
        else:
            continue
        for code in batch:
            try:
                frame = data[f"{code}.SA"][["Close", "Adj Close", "Volume", "Dividends"]].dropna(subset=["Adj Close"])
            except KeyError:
                frame = pd.DataFrame(columns=["Close", "Adj Close", "Volume", "Dividends"])
            frame.to_csv(folder / f"{code}.csv")
        time.sleep(2)
    out = {}
    for code in codes:
        frame = pd.read_csv(folder / f"{code}.csv", index_col=0, parse_dates=True)
        if len(frame):
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            out[code], days = clean(frame.sort_index())
            if days:
                CLEANED[code] = days
    return out


CLEANED: dict[str, list[str]] = {}


def series(symbol: str, cache: Path) -> pd.Series:
    path = cache / "prices" / f"{symbol.strip('^').replace('.', '_')}.csv"
    if not path.exists():
        frame = yf.Ticker(symbol).history(start="2008-01-01", end="2026-10-31", auto_adjust=True)
        frame.index = frame.index.tz_localize(None).normalize()
        frame[["Close"]].to_csv(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)["Close"].sort_index()


_SNAPSHOTS: dict = {}


def snapshot(codes, px, t: pd.Timestamp, size: int) -> pd.DataFrame:
    """The ``size`` most traded companies at ``t``, with their code, dividend yields and constancy."""
    if (t, size) in _SNAPSHOTS:
        return _SNAPSHOTS[(t, size)]
    rows = []
    for cnpj, cs in codes.items():
        best = None
        for code in cs:
            frame = px.get(code)
            if frame is None:
                continue
            window = frame.loc[:t].tail(63)
            if len(window) < 40 or window.index[-1] < t - pd.Timedelta(days=10):
                continue
            traded = float((window["Close"] * window["Volume"]).median())
            if best is None or traded > best[1]:
                best = (code, traded)
        if best is None:
            continue
        frame = px[best[0]]
        close = float(frame["Close"].loc[:t].iloc[-1])
        div = frame["Dividends"].loc[:t]
        paid = [float(div[(div.index > t - pd.DateOffset(months=12 * (k + 1)))
                          & (div.index <= t - pd.DateOffset(months=12 * k))].sum()) for k in range(3)]
        rows.append({"cnpj": cnpj, "code": best[0], "traded": best[1], "dy12": paid[0] / close,
                     "dy24": (paid[0] + paid[1]) / 2 / close, "constant": all(p > 0 for p in paid)})
    frame = pd.DataFrame(rows)
    _SNAPSHOTS[(t, size)] = frame.nlargest(size, "traded") if len(frame) else frame
    return _SNAPSHOTS[(t, size)]


def run(codes, sectors, px, sessions, *, size=50, top=10, yield_col="dy24", constancy=True, best_only=False,
        months=3):
    ends = sessions.to_series().groupby(sessions.to_period("M")).max()
    dates = [t for t in ends if pd.Timestamp(FIRST) <= t <= pd.Timestamp(LAST) and t.month % months == 0]
    segments, holdings, prev, mean_dy, level = [], [], {}, [], 1.0
    for i, t in enumerate(dates):
        entry = sessions[sessions > t][0]
        later = ends[ends > t]
        end = dates[i + 1] if i + 1 < len(dates) else later.iloc[min(months, len(later)) - 1]
        after = sessions[sessions > end]
        if not len(after):
            break
        exit_ = after[0]
        snap = snapshot(codes, px, t, size)
        pool = snap[snap["constant"]] if constancy else snap
        if best_only:
            pool = pool[[sector_at(sectors.get(c, {}), t.year) in BEST for c in pool["cnpj"]]]
        chosen = pool.nlargest(top, yield_col)
        mean_dy.append(float(chosen[yield_col].mean()) if len(chosen) else np.nan)
        days = sessions[(sessions >= entry) & (sessions <= exit_)]
        curves = {}
        for code in chosen["code"]:
            adj = px[code]["Adj Close"].reindex(days).ffill()   # a delisted stock keeps its last price
            if pd.notna(adj.iloc[0]):
                curves[code] = adj / adj.iloc[0]
        value = pd.concat(curves.values(), axis=1).mean(axis=1) if curves else pd.Series(1.0, index=days)
        w = {c: 1 / len(curves) for c in curves}
        turnover = sum(abs(w.get(c, 0) - prev.get(c, 0)) for c in set(w) | set(prev))
        segment = value * (1 - COST * turnover) * level
        level = float(segment.iloc[-1])
        segments.append(segment if not segments else segment.iloc[1:])   # the entry day closed the last period
        if curves:   # weights at exit, after drift, for the next turnover
            total = sum(float(c.iloc[-1]) for c in curves.values())
            prev = {code: float(c.iloc[-1]) / total for code, c in curves.items()}
        holdings.append({"t": t.date().isoformat(), "codes": list(curves), "turnover": turnover})
    curve = pd.concat(segments)
    return curve[~curve.index.duplicated()], holdings, float(np.nanmean(mean_dy))


def stats(curve: pd.Series, refs: dict[str, pd.Series]) -> dict:
    monthly = curve.resample("ME").last().pct_change().dropna()
    out = {"start": curve.index[0].date().isoformat(), "end": curve.index[-1].date().isoformat()}

    def cagr(c):
        return float((c.iloc[-1] / c.iloc[0]) ** (365.25 / (c.index[-1] - c.index[0]).days) - 1)

    def drawdown(c):
        return float((c / c.cummax() - 1).min())

    out["cagr"] = cagr(curve)
    out["max_drawdown"] = drawdown(curve)
    for name, ref in refs.items():
        r = ref.reindex(curve.index).ffill().dropna()
        c = curve.loc[r.index[0]:]
        rm = r.resample("ME").last().pct_change().dropna()
        d = (monthly - rm).dropna()
        roll = ((1 + monthly).rolling(36).apply(np.prod, raw=True) - (1 + rm).rolling(36).apply(np.prod, raw=True)).dropna()
        yearly = (1 + monthly).groupby(monthly.index.year).prod() - (1 + rm).groupby(rm.index.year).prod()
        out[name] = {"cagr_ref": cagr(r), "cagr_portfolio_same_period": cagr(c), "max_drawdown_ref": drawdown(r),
                     "mean_monthly_excess": float(d.mean()), "t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))),
                     "rolling36_positive": float((roll > 0).mean()), "months": len(d),
                     "by_year": {int(y): round(float(v), 4) for y, v in yearly.dropna().items()}}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "dividends"))
    args = p.parse_args()
    out = Path(args.out)
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    codes, sectors = fca(cache)
    px = prices(set().union(*codes.values()), cache)
    bvsp, divo = series("^BVSP", cache), series("DIVO11.SA", cache)
    sessions = bvsp.loc["2008-01-01":].index
    data = {"companies_with_codes": len(codes), "codes": len(set().union(*codes.values())),
            "codes_with_yahoo_prices": len(px),
            "companies_with_prices": len({c for c, cs in codes.items() if any(k in px for k in cs)}),
            "codes_with_days_neutralized": len(CLEANED), "days_neutralized": sum(len(v) for v in CLEANED.values()),
            "neutralized": {k: CLEANED[k] for k in ("PCAR3", "AZUL3") if k in CLEANED}}
    print(json.dumps(data, indent=2), flush=True)

    variants = {"primary": {}, "a_top15": {"top": 15}, "b_best_sectors": {"best_only": True},
                "c_dy12": {"yield_col": "dy12"}, "d_no_constancy": {"constancy": False},
                "e_semiannual": {"months": 6}, "f_top100_universe": {"size": 100}}
    report = {"data": data, "variants": {}}
    for name, kwargs in variants.items():
        curve, holdings, dy = run(codes, sectors, px, sessions, **kwargs)
        ew_curve = None
        if name == "primary":
            ew_curve, _, _ = run(codes, sectors, px, sessions, top=50, constancy=False, yield_col="traded")
        refs = {"ibovespa": bvsp, "divo11": divo, **({"equal_weight_50": ew_curve} if ew_curve is not None else {})}
        s = stats(curve, refs)
        s.update(mean_dividend_yield=dy, mean_turnover=float(np.mean([h["turnover"] for h in holdings])))
        report["variants"][name] = s
        curve.to_csv(out / f"curve_{name}.csv", header=["value"])
        (out / f"holdings_{name}.json").write_text(json.dumps(holdings, indent=1), encoding="utf-8")
        print(f"{name:18} CAGR {s['cagr']:.1%} | Ibov {s['ibovespa']['cagr_ref']:.1%} t={s['ibovespa']['t']:.2f} "
              f"roll36+ {s['ibovespa']['rolling36_positive']:.0%} | DIVO11 {s['divo11']['cagr_ref']:.1%} "
              f"(port same period {s['divo11']['cagr_portfolio_same_period']:.1%}) | DD {s['max_drawdown']:.0%} "
              f"| DY {dy:.1%}", flush=True)
    (out / "dividends_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
