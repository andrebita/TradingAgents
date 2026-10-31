"""Review 5 stock-selection contest: every arm through the common executor, then the frozen criteria (§4-§7).

Usage, from experiments/br_baseline:
    python -m contest_r5.run [--quick]
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd
from contest_r5 import data, engine
from contest_r5.build import load

ARMS = ["BOVA11", "DIVO11", "DY", "Q", "V", "M", "L", "QVM"]
SELECTORS = ["DY", "Q", "V", "M", "L", "QVM"]
START, END = pd.Timestamp("2014-06-30"), pd.Timestamp("2026-10-30")
INITIAL, MONTHLY = 50_000.0, 1_200.0
SUBPERIODS = [("2014-07-01", "2018-12-31"), ("2019-01-01", "2022-12-31"), ("2023-01-01", "2026-10-30")]
SCALES = [(w, s) for w in (100_000, 125_000, 150_000) for s in (0.2, 0.4, 0.6)]
REPS, SEED = 10_000, 521


def xirr(flows: list[tuple[pd.Timestamp, float]], final: tuple[pd.Timestamp, float]) -> float:
    t0 = flows[0][0]
    cash = [(t, -v) for t, v in flows] + [final]
    lo, hi = -0.99, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        npv = sum(v / (1 + mid) ** ((t - t0).days / 365.25) for t, v in cash)
        lo, hi = (mid, hi) if npv > 0 else (lo, mid)
    return (lo + hi) / 2


def cagr(u: pd.Series) -> float:
    return float((u.iloc[-1] / u.iloc[0]) ** (365.25 / (u.index[-1] - u.index[0]).days) - 1)


def drawdown(u: pd.Series) -> dict:
    dd = u / u.cummax() - 1
    trough = dd.idxmin()
    peak = u.loc[:trough].idxmax()
    after = u.loc[trough:]
    rec = after[after >= u.loc[peak]]
    return {"max": round(float(dd.min()), 4), "peak": str(peak.date()), "trough": str(trough.date()),
            "recovered": str(rec.index[0].date()) if len(rec) else None}


def monthly_log(u: pd.Series) -> pd.Series:
    m = u.groupby(u.index.to_period("M")).last()
    return np.log(m / m.shift(1)).dropna()


def holm(p: dict) -> dict:
    items = sorted(p.items(), key=lambda kv: kv[1])
    out, running = {}, 0.0
    for k, (name, v) in enumerate(items):
        running = max(running, min(1.0, (len(items) - k) * v))
        out[name] = running
    return out


def bootstrap(diff: pd.DataFrame, block: int, reps: int = REPS, seed: int = SEED) -> tuple[dict, dict]:
    """One-sided p-values (mean <= 0) and 95% intervals of the annualized mean, circular blocks, same draws."""
    rng = np.random.default_rng(seed)
    n = len(diff)
    starts = rng.integers(0, n, size=(reps, -(-n // block)))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(reps, -1)[:, :n]
    x = diff.to_numpy()
    means = x[idx].mean(axis=1)                       # reps x contrasts
    obs = x.mean(axis=0)
    p = ((means - obs) >= obs).mean(axis=0)
    lo, hi = np.percentile(means, [2.5, 97.5], axis=0)
    return ({c: float(v) for c, v in zip(diff.columns, p, strict=True)},
            {c: (round(12 * float(a), 4), round(12 * float(b), 4)) for c, a, b in zip(diff.columns, lo, hi, strict=True)})


def newey_west(y: np.ndarray, X: np.ndarray, lags: int = 6) -> tuple[np.ndarray, np.ndarray, float]:
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta
    n = len(y)
    S = (X * e[:, None]).T @ (X * e[:, None]) / n
    for k in range(1, lags + 1):
        w = 1 - k / (lags + 1)
        G = (X[k:] * e[k:, None]).T @ (X[:-k] * e[:-k, None]) / n
        S += w * (G + G.T)
    Q = np.linalg.inv(X.T @ X / n)
    cov = Q @ S @ Q / n
    r2 = 1 - e.var() / y.var()
    adj = 1 - (1 - r2) * (n - 1) / (n - X.shape[1])
    return beta, np.sqrt(np.diag(cov)), float(adj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the 60-month windows")
    args = ap.parse_args()
    t0 = time.time()
    market, codes, sig, etf, _ = load()
    px = engine.Prices(market.sessions, codes, etf, market.company)
    tables: dict = {}

    def selector(arm):
        def pick(t):
            if t not in tables:
                tables[t] = sig.table(t)
            return sig.select(tables[t], arm)
        return pick

    def run(arm, start=START, end=END, initial=INITIAL, monthly=MONTHLY, **kw):
        if arm in ("BOVA11", "DIVO11"):
            return engine.run(px, lambda t: [arm], start, end, initial, monthly, etf=True, **kw)
        return engine.run(px, selector(arm), start, end, initial, monthly, **kw)

    ipca = pd.read_csv(data.HERE / "results/allocation_r5/cache/sgs_433.csv", index_col=0, parse_dates=True)["value"]
    level = (1 + ipca / 100).cumprod()

    def real_xirr(flows, final_value):
        def at(t):
            return float(level.loc[:t].iloc[-1])
        end_level = at(END)
        return xirr([(t, v * end_level / at(t)) for t, v in flows], (END, final_value))

    report = {"protocol": "protocols/protocolo_r5.md", "deviations": "README, 'disputa de seleção: dados e desvios'",
              "period": f"{START.date()} to {END.date()}", "arms": {}, "sensitivities": {}}
    primary = {}
    for arm in ARMS:
        r = run(arm)
        primary[arm] = r
        u = r.units
        years = {y: float(u[u.index.year == y].iloc[-1] / u[u.index.year < y].iloc[-1] - 1)
                 for y in range(2015, 2026)}
        avg_nav = float(r.nav.mean())
        report["arms"][arm] = {
            "final_after_liquidation": round(r.final), "final_marked": round(r.final_before),
            "contributed": round(sum(v for _, v in r.flows)),
            "irr_after_liquidation": round(xirr(r.flows, (END, r.final)), 4),
            "irr_marked": round(xirr(r.flows, (END, r.final_before)), 4),
            "irr_real_after_liquidation": round(real_xirr(r.flows, r.final), 4),
            "cagr_unit": round(cagr(u), 4), "drawdown_daily": drawdown(u),
            "drawdown_monthly": round(float((u.resample("ME").last() / u.resample("ME").last().cummax() - 1).min()), 4),
            "worst_full_year": min(years.items(), key=lambda kv: kv[1]),
            "partial_2014_h2": round(float(u[u.index.year == 2014].iloc[-1] / u.iloc[0] - 1), 4),
            "partial_2026_ytd": round(float(u.iloc[-1] / u[u.index.year < 2026].iloc[-1] - 1), 4),
            "tax_paid": round(r.tax_paid), "tax_terminal": round(r.tax_terminal), "costs": round(r.costs),
            "turnover_one_way_per_year": round(sum(r.sales_by_year.get(y, 0) for y in range(2015, 2026)) / 11
                                               / avg_nav, 3),
            "orders_per_month": round(r.orders / r.months, 2), "detected_share": round(r.detected_share, 3),
            "idle_cash_share": round(r.idle_cash_share, 3),
            "unexplained_moves": r.unexplained, "carry_end": round(r.carry_end)}
        print(arm, json.dumps({k: report["arms"][arm][k] for k in ("irr_after_liquidation", "cagr_unit",
                                                                  "drawdown_daily", "final_after_liquidation")}),
              round(time.time() - t0), "s", flush=True)

    # Sensitivities: JCP share 0%, doubled cost, nine scales (final after liquidation).
    for arm in ARMS:
        s = {"jcp_0": round(run(arm, params=engine.Params(jcp_share=0.0)).final),
             "cost_030": round(run(arm, params=engine.Params(cost=0.003)).final)}
        for w, share in SCALES:
            s[f"scale_{w // 1000}k_{int(share * 100)}"] = round(run(arm, initial=w * share,
                                                                    monthly=3000 * share).final)
        report["sensitivities"][arm] = s
        print("sens", arm, round(time.time() - t0), "s", flush=True)

    # Monthly log returns of the net unit value, and the contrasts.
    logs = pd.DataFrame({a: monthly_log(primary[a].units) for a in ARMS}).dropna()
    contrasts = {f"{a}-BOVA11": logs[a] - logs["BOVA11"] for a in ARMS if a != "BOVA11"}
    contrasts |= {f"{a}-DIVO11": logs[a] - logs["DIVO11"] for a in SELECTORS}
    diff = pd.DataFrame(contrasts)
    inference = {}
    for block in (12, 6, 24):
        p, ci = bootstrap(diff, block)
        inference[f"block_{block}"] = {"p": {k: round(v, 4) for k, v in p.items()},
                                       "p_holm": {k: round(v, 4) for k, v in holm(p).items()}, "ci95_annual": ci}
    report["inference"] = inference
    report["mean_excess_annual"] = {c: round(12 * float(v), 4) for c, v in diff.mean().items()}

    # Subperiods and robustness diagnostics on monthly excess log-returns.
    sub = {}
    for a in ARMS:
        u = primary[a].units
        sub[a] = [round(cagr(u.loc[s:e]), 4) for s, e in SUBPERIODS]
    report["subperiod_cagr"] = sub
    robust = {}
    for c, d in contrasts.items():
        years = d.groupby(d.index.year).sum()
        full = years[(years.index >= 2015) & (years.index <= 2025)]
        best = int(full.idxmax())
        robust[c] = {"without_2016": round(12 * float(d[d.index.year != 2016].mean()), 4),
                     "best_year": best, "without_best_year": round(12 * float(d[d.index.year != best].mean()), 4)}
    report["robustness"] = robust

    # 60-month windows, monthly steps, the same flows: unit CAGR per arm.
    if not args.quick:
        ends = engine.month_ends(market.sessions, START, END)
        windows = {a: [] for a in ARMS}
        for i, s in enumerate(ends):
            if i + 60 >= len(ends):
                break
            e = ends[i + 60]
            for a in ARMS:
                windows[a].append(cagr(run(a, start=s, end=e).units))
        report["windows_60m"] = {"n": len(windows["BOVA11"]), "cagr": {a: [round(x, 4) for x in v] for a, v in windows.items()}}
        print("windows", round(time.time() - t0), "s", flush=True)

    # NEFIN attribution, value- and equal-weighted factors.
    nef = {}
    for name in ("nefin_factors_monthly.csv", "nefin_factors_equally_weighted_monthly.csv"):
        f = pd.read_csv(data.OUT / "cache" / name)
        f.index = pd.PeriodIndex([pd.Period(f"{y}-{m:02d}", "M") for y, m in zip(f.year, f.month, strict=True)])
        out = {}
        simple = pd.DataFrame({a: np.exp(logs[a]) - 1 for a in ARMS})
        simple["DY-DIVO11"] = simple["DY"] - simple["DIVO11"]
        for col in list(ARMS) + ["DY-DIVO11"]:
            j = simple[[col]].join(f, how="inner")
            y = (j[col] - (0 if col == "DY-DIVO11" else j["Risk_Free"])).to_numpy()
            X = j[["Rm_minus_Rf", "SMB", "HML", "WML", "IML"]].to_numpy()
            b, se, r2 = newey_west(y, X)
            out[col] = {"months": len(j), "alpha_annual": round(12 * float(b[0]), 4), "t_alpha": round(float(b[0] / se[0]), 2),
                        "betas": {k: round(float(v), 3) for k, v in zip(["mkt", "smb", "hml", "wml", "iml"], b[1:], strict=True)},
                        "t": {k: round(float(v), 2) for k, v in zip(["mkt", "smb", "hml", "wml", "iml"], b[1:] / se[1:], strict=True)},
                        "adj_r2": round(r2, 3)}
        nef[name.split(".")[0]] = out
    report["nefin"] = nef

    data.OUT.mkdir(parents=True, exist_ok=True)
    (data.OUT / "contest_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                                                  encoding="utf-8")
    pd.DataFrame({a: primary[a].units for a in ARMS}).to_csv(data.OUT / "units_daily.csv.gz")
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()
