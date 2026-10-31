"""Review 5, allocation module (protocol §8): which mix gives the best chance of beating Tesouro Selic in 5 years?

Policies, all with R$ 125,000 to start and R$ 3,000 a month:
  A: 40% BOVA11 / 60% Tesouro Selic
  B: 60% BOVA11 / 40% Tesouro Selic
  C: 20% BOVA11 / 40% global equities / 40% Tesouro Selic
  D: 80% equities (26.7% BOVA11 / 53.3% global / 20% Selic), diagnostic only

"Good" (the investor's choice, frozen before this ran): more wealth after liquidation and taxes than the same
flows kept in Tesouro Selic. Measured over historical 60-month windows (a start every month) and by a circular
block bootstrap (12-month blocks, 10,000 paths, seed 521). Pre-registered in the README ("Revisão 5").

Global equities before WRLD11 (Oct 2021) are a hypothetical reconstruction: VT total return in dollars, less 30%
withholding on its dividends and WRLD11's 0.36% a year, converted at the PTAX rate. Shown apart from WRLD11.

Simplified execution, declared before running: monthly steps (decide and execute at the month's last session),
fractional ETF units, 0.15% per ETF order, 15% tax on ETF gains at the average cost (no R$ 20k exemption; ETF
losses offset ETF gains), Tesouro Selic with 0.20% a year custody and the regressive tax by lot (22.5% up to 6
months, 20% to 12, 17.5% to 24, 15% beyond) charged on redemption and on the final liquidation.

Usage:
    python experiments/br_baseline/allocation_r5.py [--out experiments/br_baseline/results/allocation_r5]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).resolve().parent
END = "2026-10-30"
MONTHLY = 3000.0
SCALES = (100_000.0, 125_000.0, 150_000.0)
MAIN_SCALE = 125_000.0
POLICIES = {"A": (0.40, 0.00, 0.60), "B": (0.60, 0.00, 0.40), "C": (0.20, 0.40, 0.40),
            "D_diagnostic": (0.80 / 3, 1.60 / 3, 0.20)}          # (BOVA11, global, Selic)
COST = 0.0015
ETF_TAX = 0.15
CUSTODY = 0.0020                       # Tesouro Selic custody, a year
WITHHOLDING, WRLD11_FEE = 0.30, 0.0036
BAND = 0.05
WINDOW = 60
PATHS, BLOCK, SEED = 10_000, 12, 521


# --- Data ---------------------------------------------------------------------------------------

def sgs(series: int, cache: Path) -> pd.Series:
    path = cache / f"sgs_{series}.csv"
    if not path.exists():
        rows = []
        for year in range(2008, 2027):
            url = (f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series}/dados?formato=json"
                   f"&dataInicial=01/01/{year}&dataFinal=31/12/{year}")
            for attempt in range(5):
                try:
                    response = requests.get(url, timeout=60)
                    response.raise_for_status()
                    rows += response.json()
                    break
                except (requests.RequestException, ValueError):
                    time.sleep(3 * 2 ** attempt)
            else:
                raise RuntimeError(f"SGS {series} {year} unavailable")
        pd.Series({pd.to_datetime(r["data"], dayfirst=True): float(r["valor"]) for r in rows}).sort_index() \
            .to_csv(path, header=["value"])
    return pd.read_csv(path, index_col=0, parse_dates=True)["value"]


def yahoo(symbol: str, cache: Path, adjusted: bool) -> pd.DataFrame:
    path = cache / f"{symbol.replace('.', '_')}_{'adj' if adjusted else 'raw'}.csv"
    if not path.exists():
        frame = yf.Ticker(symbol).history(start="2008-01-01", end="2026-10-31", auto_adjust=adjusted)
        frame.index = frame.index.tz_localize(None).normalize()
        frame[["Close", "Dividends"]].to_csv(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)


def daily_indexes(cache: Path) -> tuple[pd.DataFrame, pd.Series]:
    """Daily total-return indexes in reais on the B3 calendar: bova, global (hypothetical), selic; and WRLD11."""
    bova = yahoo("BOVA11.SA", cache, True)["Close"]
    sessions = bova.index[(bova.index >= "2008-12-01") & (bova.index <= END)]
    vt_adj, vt_raw = yahoo("VT", cache, True), yahoo("VT", cache, False)
    tr = vt_adj["Close"].pct_change().fillna(0.0)
    prev_raw = vt_raw["Close"].shift(1)
    withheld = (WITHHOLDING * vt_raw["Dividends"] / prev_raw).reindex(tr.index).fillna(0.0)
    days = (tr.index.to_series().diff().dt.days.fillna(0)).values
    fee = (1 - WRLD11_FEE) ** (days / 365.25) - 1
    usd = (1 + tr - withheld + fee).cumprod()
    ptax = sgs(1, cache)
    glob = (usd.reindex(sessions, method="ffill") * ptax.reindex(sessions, method="ffill"))
    selic_day = sgs(11, cache) / 100                                   # % a day
    selic = (1 + selic_day.reindex(sessions).fillna(0.0)).cumprod()
    gaps = sessions.to_series().diff().dt.days.fillna(0).values
    selic = selic * (1 - CUSTODY) ** (np.cumsum(gaps) / 365.25)
    frame = pd.DataFrame({"bova": bova.reindex(sessions), "glob": glob, "selic": selic}).ffill().dropna()
    wrld = yahoo("WRLD11.SA", cache, True)["Close"]
    return frame / frame.iloc[0], wrld


def monthly_returns(daily: pd.DataFrame, ipca: pd.Series) -> pd.DataFrame:
    ends = daily.index.to_series().groupby(daily.index.to_period("M")).max()
    m = daily.loc[ends.values].pct_change().dropna()
    m.index = m.index.to_period("M")
    m["ipca"] = (ipca / 100).reindex(m.index.to_timestamp()).values
    return m.dropna()


# --- The policy simulator -----------------------------------------------------------------------

def regressive(months: int) -> float:
    return 0.225 if months <= 6 else 0.20 if months <= 12 else 0.175 if months <= 24 else 0.15


class Book:
    """Holdings as units of each class's index: ETFs with their cost, Tesouro Selic as lots (month, principal,
    units). Growth only moves the index, so a month costs the same however many lots there are."""

    def __init__(self):
        self.idx = {"bova": 1.0, "glob": 1.0, "selic": 1.0}
        self.units = {"bova": 0.0, "glob": 0.0}
        self.cost = {"bova": 0.0, "glob": 0.0}
        self.lots: list[list[float]] = []
        self.selic_units = 0.0
        self.etf_loss = 0.0
        self.tax_paid = 0.0

    def value(self) -> dict:
        return {"bova": self.units["bova"] * self.idx["bova"], "glob": self.units["glob"] * self.idx["glob"],
                "selic": self.selic_units * self.idx["selic"]}

    def grow(self, r: dict) -> None:
        for c in self.idx:
            self.idx[c] *= 1 + r[c]

    def buy(self, c: str, amount: float, month: int) -> None:
        if amount <= 0:
            return
        if c == "selic":
            units = amount / self.idx["selic"]
            self.lots.append([month, amount, units])
            self.selic_units += units
        else:
            self.units[c] += amount * (1 - COST) / self.idx[c]
            self.cost[c] += amount

    def sell(self, c: str, amount: float, month: int) -> float:
        """Sell ``amount`` (at value) of class ``c``; return the cash after cost and tax."""
        if amount <= 0:
            return 0.0
        if c == "selic":
            cash, left, price = 0.0, amount, self.idx["selic"]
            while left > 1e-9 and self.lots:
                bought, principal, units = self.lots[0]
                value = units * price
                take = min(left, value)
                share = take / value
                tax = regressive(month - bought) * max(value - principal, 0.0) * share
                self.tax_paid += tax
                cash += take - tax
                self.selic_units -= units * share
                if share >= 1 - 1e-12:
                    self.lots.pop(0)
                else:
                    self.lots[0] = [bought, principal * (1 - share), units * (1 - share)]
                left -= take
            return cash
        value = self.units[c] * self.idx[c]
        take = min(amount, value)
        if value <= 0:
            return 0.0
        share = take / value
        basis = self.cost[c] * share
        self.units[c] -= self.units[c] * share
        self.cost[c] -= basis
        proceeds = take * (1 - COST)
        gain = proceeds - basis - self.etf_loss
        tax = ETF_TAX * max(gain, 0.0)
        self.etf_loss = max(-gain, 0.0)
        self.tax_paid += tax
        return proceeds - tax

    def liquidate(self, month: int) -> float:
        v = self.value()
        return sum(self.sell(c, v[c], month) for c in ("bova", "glob", "selic"))


def place(book: Book, weights, flow: float, month: int) -> None:
    """New money to the largest deficits in reais, in order."""
    v = book.value()
    total = sum(v.values()) + flow
    deficits = sorted(((w * total - v[c], c) for w, c in zip(weights, ("bova", "glob", "selic"), strict=True) if w > 0),
                      reverse=True)
    for deficit, c in deficits:
        amount = min(max(deficit, 0.0), flow)
        book.buy(c, amount, month)
        flow -= amount
    if flow > 1e-9:
        book.buy("selic", flow, month)


def rebalance(book: Book, weights, month: int) -> None:
    v = book.value()
    total = sum(v.values())
    classes = ("bova", "glob", "selic")
    if total <= 0 or max(abs(v[c] / total - w) for w, c in zip(weights, classes, strict=True)) <= BAND:
        return
    cash = 0.0
    for w, c in zip(weights, classes, strict=True):
        if v[c] > w * total:
            cash += book.sell(c, v[c] - w * total, month)
    place(book, weights, cash, month)


def run(returns: np.ndarray, months: list, weights, initial: float, *, path: bool = False):
    """Simulate ``weights`` over monthly ``returns`` (bova, glob, selic, ipca); return wealth after liquidation.

    ``months``: calendar month numbers (1-12) for the semiannual review. With ``path``, also the monthly values.
    """
    book, sel = Book(), Book()
    values, flows = [], []
    for i, (rb, rg, rs, _) in enumerate(returns):
        flow = MONTHLY + (initial if i == 0 else 0.0)
        if i:
            book.grow({"bova": rb, "glob": rg, "selic": rs})
            sel.grow({"bova": rb, "glob": rg, "selic": rs})
        values.append(sum(book.value().values()))
        flows.append(flow)
        place(book, weights, flow, i)
        sel.buy("selic", flow, i)
        if months[i] in (6, 12) and i:
            rebalance(book, weights, i)
    n = len(returns)
    final, final_selic = book.liquidate(n), sel.liquidate(n)
    inflation = float(np.prod(1 + returns[1:, 3]))
    out = {"final": final, "final_selic": final_selic, "contributed": initial + MONTHLY * n,
           "real_final": final / inflation, "tax_paid": book.tax_paid}
    if path:
        out["values"], out["flows"] = values, flows
    return out


def xirr(flows: list[float], final: float) -> float:
    lo, hi = -0.99, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        npv = sum(-f / (1 + mid) ** (i / 12) for i, f in enumerate(flows)) + final / (1 + mid) ** (len(flows) / 12)
        lo, hi = (mid, hi) if npv > 0 else (lo, mid)
    return (lo + hi) / 2


def daily_drawdown(daily: pd.DataFrame, weights) -> dict:
    """Worst fall of a monthly rebalanced-to-target unit value, measured every session (no flows, no tax)."""
    ends = set(daily.index.to_series().groupby(daily.index.to_period("M")).max())
    units, nav, out = None, 1.0, []
    cols = ["bova", "glob", "selic"]
    for day, row in daily[cols].iterrows():
        if units is None:
            units = {c: w * nav / row[c] for w, c in zip(weights, cols, strict=True)}
        nav = sum(units[c] * row[c] for c in cols)
        out.append(nav)
        if day in ends:
            units = {c: w * nav / row[c] for w, c in zip(weights, cols, strict=True)}
    s = pd.Series(out, index=daily.index)
    dd = s / s.cummax() - 1
    trough = dd.idxmin()
    peak = s.loc[:trough].idxmax()
    later = s.loc[trough:]
    recovered = later[later >= s.loc[peak]]
    return {"max_drawdown_daily": round(float(dd.min()), 4), "peak": peak.date().isoformat(),
            "trough": trough.date().isoformat(),
            "recovered": recovered.index[0].date().isoformat() if len(recovered) else None,
            "cagr_unit": round(float(s.iloc[-1] ** (365.25 / (s.index[-1] - s.index[0]).days) - 1), 4)}


def summarize(results: list[dict]) -> dict:
    ratio = np.array([r["final"] / r["final_selic"] for r in results])
    real = np.array([r["real_final"] / r["contributed"] for r in results])
    losses = ratio[ratio < 1] - 1
    return {"n": len(results), "p_beat_selic": round(float((ratio > 1).mean()), 4),
            "median_vs_selic": round(float(np.median(ratio) - 1), 4),
            "p5_vs_selic": round(float(np.percentile(ratio, 5) - 1), 4),
            "median_shortfall_when_losing": round(float(np.median(losses)), 4) if len(losses) else None,
            "p_real_gain": round(float((real > 1).mean()), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "results" / "allocation_r5"))
    args = ap.parse_args()
    out = Path(args.out)
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    daily, wrld = daily_indexes(cache)
    m = monthly_returns(daily, sgs(433, cache))
    arr = m[["bova", "glob", "selic", "ipca"]].to_numpy()
    cal = [p.month for p in m.index]
    report = {"design": "README, 'Revisão 5'", "monthly_sample": f"{m.index[0]} a {m.index[-1]}",
              "global_before_wrld11": "hypothetical: VT TR less 30% withholding and 0.36%/yr, at PTAX",
              "policies": {k: dict(zip(("bova", "global", "selic"), (round(x, 4) for x in w), strict=True))
                           for k, w in POLICIES.items()}, "results": {}}

    # Tracking of the reconstruction against WRLD11 over their overlap.
    overlap = daily.index[daily.index >= wrld.index[0]]
    w = wrld.reindex(overlap, method="ffill")
    g = daily["glob"].reindex(overlap)
    years = (overlap[-1] - overlap[0]).days / 365.25
    report["wrld11_check"] = {"since": overlap[0].date().isoformat(),
                              "wrld11_cagr": round(float((w.iloc[-1] / w.iloc[0]) ** (1 / years) - 1), 4),
                              "reconstruction_cagr": round(float((g.iloc[-1] / g.iloc[0]) ** (1 / years) - 1), 4)}

    rng = np.random.default_rng(SEED)
    n = len(arr)
    starts = rng.integers(0, n, size=(PATHS, WINDOW // BLOCK))
    boot_idx = ((starts[:, :, None] + np.arange(BLOCK)) % n).reshape(PATHS, WINDOW)
    boot_months = [((i % 12) + 1) for i in range(WINDOW)]

    for scale in SCALES:
        for name, weights in POLICIES.items():
            key = f"{name}_{int(scale / 1000)}k"
            full = run(arr, cal, weights, scale, path=True)
            windows = [run(arr[s:s + WINDOW], cal[s:s + WINDOW], weights, scale) for s in range(n - WINDOW + 1)]
            entry = {"full_period": {"final": round(full["final"]), "final_selic": round(full["final_selic"]),
                                     "irr": round(xirr(full["flows"], full["final"]), 4),
                                     "irr_selic": round(xirr(full["flows"], full["final_selic"]), 4),
                                     "tax_paid": round(full["tax_paid"])},
                     "windows_60m": summarize(windows)}
            if scale == MAIN_SCALE:
                boot = [run(arr[idx], boot_months, weights, scale) for idx in boot_idx]
                entry["bootstrap"] = summarize(boot)
                entry["daily"] = daily_drawdown(daily, weights)
                worst = min(range(len(windows)), key=lambda s: windows[s]["final"] / windows[s]["final_selic"])
                entry["worst_window"] = {"start": str(m.index[worst]),
                                         "vs_selic": round(windows[worst]["final"] / windows[worst]["final_selic"]
                                                           - 1, 4)}
            report["results"][key] = entry
            print(key, json.dumps(entry, ensure_ascii=False), flush=True)

    # Stress scenarios over 60 months, Selic at 15% a year throughout (not forecasts).
    selic_m = 1.15 ** (1 / 12) - 1
    # The shocks land in month 1: month 0 is the first purchase, with no return before it.
    scen = {"queda_simultanea": {1: (-0.50, -0.35)}, "real_valoriza_20pct": {1: (0.0, -0.20)},
            "juros_altos_bolsa_parada": {}}
    report["stress"] = {}
    for sname, shocks in scen.items():
        r = np.array([[*shocks.get(i, (0.0, 0.0)), selic_m, 0.004] for i in range(WINDOW)])
        report["stress"][sname] = {name: round(run(r, boot_months, w_, MAIN_SCALE)["final"]
                                               / run(r, boot_months, w_, MAIN_SCALE)["final_selic"] - 1, 4)
                                   for name, w_ in POLICIES.items()}

    # Exploratory, added after seeing the results (not part of the decision): the global sleeve without the real's
    # average depreciation over the sample, to show how much of policy C's edge is the currency.
    fx = sgs(1, cache)
    fx_m = fx.groupby(fx.index.to_period("M")).last().pct_change().reindex(m.index).fillna(0.0)
    drift = float((1 + fx_m).prod() ** (1 / len(fx_m)) - 1)
    neutral = arr.copy()
    neutral[:, 1] = (1 + neutral[:, 1]) / (1 + drift) - 1
    report["exploratory_fx_neutral"] = {
        "note": "post hoc; global returns less the real's mean monthly depreciation over the sample",
        "fx_drift_annual": round((1 + drift) ** 12 - 1, 4),
        "bootstrap": {name: summarize([run(neutral[idx], boot_months, POLICIES[name], MAIN_SCALE)
                                       for idx in boot_idx]) for name in ("C", "D_diagnostic")}}

    ranked = sorted(((v["bootstrap"]["p_beat_selic"], v["bootstrap"]["p5_vs_selic"], k)
                     for k, v in report["results"].items() if k.endswith("125k") and not k.startswith("D_")),
                    reverse=True)
    report["decision"] = {"ranking_bootstrap": [(k, p, p5) for p, p5, k in ranked],
                          "rule": "highest bootstrap P(beat Selic); ties within 5 points by the smaller 5th-pct "
                                  "loss; if none above 0.5, back to the investor"}
    (out / "allocation_r5_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    m.to_csv(out / "monthly_returns.csv")
    print(json.dumps(report["decision"], indent=1))


if __name__ == "__main__":
    main()
