"""Rule-based baselines for B3 signal quality, scored as ``tradingagents backtest`` scores.

For every (ticker, analysis date) cell the outcome is defined exactly as in
``tradingagents.memory.settlement.fetch_returns``: entry at the first close
*after* the analysis date, exit ``holding`` sessions later, alpha = stock return
minus Ibovespa return over the same dates, on Yahoo's dividend-adjusted closes.
Signals only read closes up to and including the analysis date.

Each strategy gives every cell a direction (+1 long, 0 hold, -1 short). As in
``tradingagents.backtest.summarize``, a directional cell is a hit when
``direction * alpha > 0``; Hold cells claim nothing. On top of that this reports
the mean signed alpha (the alpha of a long/short-vs-Ibovespa bet sized 1 per
cell), its bootstrap 95% interval, and the same net of a round-trip cost.

A random-direction control is run over many seeds, so any strategy (or a later
LLM run on the same grid) can be placed against the distribution of luck.

Usage:
    python experiments/br_baseline/baselines.py --out DIR --tickers A.SA,B.SA \
        --start 2026-01-05 --end 2026-10-23 --every 7 [--label oos_2026]
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BENCH = "^BVSP"
COST_ROUND_TRIP = 0.0020   # 20 bps: B3 fees (~0.03%/side) + spread/slippage, no brokerage


def closes(symbol: str, start: str, end: str) -> pd.Series:
    """Adjusted daily closes, as ``get_closes`` reads them (``history`` auto-adjusts)."""
    for attempt in range(4):      # Yahoo throttles bursts; back off and retry
        try:
            h = yf.Ticker(symbol).history(start=start, end=end)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * 2 ** attempt)
    if h is None or h.empty:
        return pd.Series(dtype=float)
    s = h["Close"]
    s = s[s > 0]
    s.index = s.index.tz_localize(None).normalize()
    return s


def grid(start: str, end: str, every: int) -> list[pd.Timestamp]:
    d, last, out = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d"), []
    while d <= last:
        out.append(pd.Timestamp(d))
        d += timedelta(days=every)
    return out


def outcome(stock: pd.Series, bench: pd.Series, date: pd.Timestamp, holding: int):
    after = stock[stock.index > date]
    if len(after) <= holding:
        return None
    entry, exit_ = after.index[0], after.index[holding]
    if bench.empty or bench.index[0] > entry or bench.index[-1] < exit_:
        return None
    raw = after.iloc[holding] / after.iloc[0] - 1
    b = bench.asof(exit_) / bench.asof(entry) - 1
    return {"entry": entry.strftime("%Y-%m-%d"), "exit": exit_.strftime("%Y-%m-%d"),
            "raw": float(raw), "bench": float(b), "alpha": float(raw - b)}


def rsi(series: pd.Series, n: int = 14) -> float:
    diff = series.diff().dropna()
    up, down = diff.clip(lower=0), -diff.clip(upper=0)
    # Wilder smoothing, as stockstats' rsi
    au = up.ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    ad = down.ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    return 100.0 if ad == 0 else float(100 - 100 / (1 + au / ad))


def signals(hist: pd.Series) -> dict[str, int | None]:
    """Directions from closes known at the analysis date (``hist`` ends there)."""
    def sign(x):
        return None if x is None or np.isnan(x) else (1 if x > 0 else -1)

    n = len(hist)
    last = hist.iloc[-1]
    out = {"always_long": 1}
    out["mom_20d"] = sign(last / hist.iloc[-21] - 1) if n > 21 else None
    out["mom_60d"] = sign(last / hist.iloc[-61] - 1) if n > 61 else None
    # 12-1 momentum: return from 12 months to 1 month ago
    out["mom_12_1"] = sign(hist.iloc[-22] / hist.iloc[-253] - 1) if n > 253 else None
    out["reversal_5d"] = -sign(last / hist.iloc[-6] - 1) if n > 6 else None
    out["px_gt_sma50"] = sign(last - hist.iloc[-50:].mean()) if n >= 50 else None
    out["sma20_gt_sma50"] = sign(hist.iloc[-20:].mean() - hist.iloc[-50:].mean()) if n >= 50 else None
    if n > 30:
        r = rsi(hist.iloc[-120:])
        out["rsi14_revert"] = 1 if r < 30 else (-1 if r > 70 else 0)
    else:
        out["rsi14_revert"] = None
    return out


def _pct(xs, q) -> float:
    return float(np.percentile(xs, q))


def block_bootstrap(sums: np.ndarray, counts: np.ndarray, block: int, rng: np.random.Generator,
                    boots: int) -> list[float] | None:
    """95% interval of the mean per cell, resampling circular blocks of consecutive dates.

    Cells on one date share that week's moves, and with a holding window longer
    than the grid step consecutive dates share sessions too; blocks of ``block``
    dates keep both dependencies. Too few dates for three blocks: no interval.
    """
    n = len(sums)
    if n < 3 * block:
        return None
    starts = rng.integers(0, n, size=(boots, -(-n // block)))
    idx = ((starts[..., None] + np.arange(block)) % n).reshape(boots, -1)[:, :n]
    total, cells = sums[idx].sum(axis=1), counts[idx].sum(axis=1)
    # A resample drawing only dates with no call carries no information; how many
    # did is reported, since a stable-looking interval may rest on very few calls.
    filled = cells > 0
    means = total[filled] / cells[filled]
    if not len(means):
        return None
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)), int((~filled).sum())]


def score(dirs: np.ndarray, alphas: np.ndarray, dates: np.ndarray, rng: np.random.Generator,
          grid_dates: list[str], block: int, boots: int = 2000) -> dict:
    mask = dirs != 0
    d, a, days = dirs[mask], alphas[mask], dates[mask]
    res = {"cells": int(len(dirs)), "directional": int(mask.sum()),
           "long": int((dirs > 0).sum()), "short": int((dirs < 0).sum()), "hold": int((dirs == 0).sum())}
    if not len(d):
        return res
    signed = d * a
    res["hit_rate"] = float((signed > 0).mean())
    res["mean_signed_alpha"] = float(signed.mean())
    res["mean_signed_alpha_net"] = float((signed - COST_ROUND_TRIP).mean())
    res["mean_alpha_long"] = float(a[d > 0].mean()) if (d > 0).any() else None
    res["mean_alpha_short_cells"] = float(a[d < 0].mean()) if (d < 0).any() else None
    se = signed.std(ddof=1) / np.sqrt(len(signed)) if len(signed) > 1 else float("nan")
    res["t_stat"] = float(signed.mean() / se) if se and not np.isnan(se) and se > 0 else None
    idx = rng.integers(0, len(signed), size=(boots, len(signed)))
    bm = signed[idx].mean(axis=1)
    res["ci95_signed_alpha"] = [float(np.percentile(bm, 2.5)), float(np.percentile(bm, 97.5))]
    # Every date of the grid stays in the sequence, abstentions included, so the
    # blocks span real calendar time.
    by_date = pd.Series(signed).groupby(days)
    sums = by_date.sum().reindex(grid_dates, fill_value=0.0).to_numpy()
    counts = by_date.count().reindex(grid_dates, fill_value=0).to_numpy()
    res["ci95_signed_alpha_blocks"] = block_bootstrap(sums, counts, block, rng, boots)
    res["block_dates"] = block
    return res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--tickers", required=True)
    p.add_argument("--start", help="first grid date (with --end and --every), unless --dates")
    p.add_argument("--end")
    p.add_argument("--every", type=int, default=7)
    p.add_argument("--dates", help="comma-separated analysis dates; replaces the start/end/every grid, so the "
                                   "cells match a run made on explicit dates")
    p.add_argument("--holding", type=int, default=5)
    p.add_argument("--label", default=None)
    p.add_argument("--random-seeds", type=int, default=1000)
    args = p.parse_args()

    if not args.dates and not (args.start and args.end):
        p.error("give --start and --end, or --dates")
    if args.dates:
        analysis_dates = [pd.Timestamp(d.strip()) for d in args.dates.split(",") if d.strip()]
        args.start, args.end = min(analysis_dates).strftime("%Y-%m-%d"), max(analysis_dates).strftime("%Y-%m-%d")
    else:
        analysis_dates = grid(args.start, args.end, args.every)
    label = args.label or f"{args.start}_{args.end}"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    lo = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")
    # Reach far enough past the last date for its holding window to trade: about
    # 7 calendar days per 5 sessions, plus room for holidays (the end is exclusive).
    hi = (datetime.strptime(args.end, "%Y-%m-%d")
          + timedelta(days=args.holding * 7 // 5 + 15)).strftime("%Y-%m-%d")
    # Today's bar is the live session, not a close: drop it as settlement does.
    today = pd.Timestamp.now().normalize()
    bench = closes(BENCH, lo, hi)
    bench = bench[bench.index < today]

    rows, missing = [], []
    for t in tickers:
        s = closes(t, lo, hi)
        s = s[s.index < today] if not s.empty else s
        if s.empty:
            missing.append(t)
            continue
        for date in analysis_dates:
            o = outcome(s, bench, date, args.holding)
            if o is None:
                continue
            hist = s[s.index <= date]
            if hist.empty:
                continue
            rows.append({"ticker": t, "date": date.strftime("%Y-%m-%d"), "holding": args.holding,
                         "last_close_date": hist.index[-1].strftime("%Y-%m-%d"), **o,
                         **{f"sig_{k}": v for k, v in signals(hist).items()}})
    cells = pd.DataFrame(rows)
    cells.to_csv(out / f"cells_{label}.csv", index=False)

    rng = np.random.default_rng(20261030)
    strategies = [c[4:] for c in cells.columns if c.startswith("sig_")]
    summary = {"label": label, "tickers": tickers, "missing": missing, "start": args.start,
               "end": args.end, "every_days": args.every, "holding_sessions": args.holding,
               "benchmark": BENCH, "cost_round_trip": COST_ROUND_TRIP,
               "generated_at": datetime.now().isoformat(timespec="seconds"), "strategies": {}}
    # Blocks span at least six weeks and twice the holding window, in grid dates:
    # a holding window of h sessions covers about h*7/5 calendar days.
    step = (int(np.median(np.diff([d.toordinal() for d in analysis_dates]))) if len(analysis_dates) > 1
            else args.every)
    block = max(-(-42 // step), -(-(2 * args.holding * 7 // 5) // step))
    # The requested grid, not just the dates that produced cells: a missing date
    # stays a gap in calendar time instead of joining its neighbours.
    # Dates past the last settled one are not abstentions, only too recent.
    grid_dates = [d.strftime("%Y-%m-%d") for d in analysis_dates
                  if d.strftime("%Y-%m-%d") <= cells["date"].max()]
    summary["block_dates"] = block
    for k in strategies:
        sub = cells.dropna(subset=[f"sig_{k}"])
        summary["strategies"][k] = score(sub[f"sig_{k}"].to_numpy(dtype=int),
                                         sub["alpha"].to_numpy(dtype=float),
                                         sub["date"].to_numpy(), rng, grid_dates, block)

    # Random-direction control on the same cells: the spread luck alone produces.
    alphas = cells["alpha"].to_numpy(dtype=float)
    hits, means = [], []
    for _ in range(args.random_seeds):
        d = rng.choice([-1, 1], size=len(alphas))
        hits.append(float(((d * alphas) > 0).mean()))
        means.append(float((d * alphas).mean()))
    summary["random_control"] = {
        "seeds": args.random_seeds, "cells": int(len(alphas)),
        "hit_rate": {"p2.5": _pct(hits, 2.5), "p50": _pct(hits, 50), "p97.5": _pct(hits, 97.5)},
        "mean_signed_alpha": {"p2.5": _pct(means, 2.5), "p50": _pct(means, 50), "p97.5": _pct(means, 97.5)},
    }
    per_ticker = cells.groupby("ticker")["alpha"].agg(["count", "mean", "std"])
    per_ticker["share_beating_ibov"] = cells.groupby("ticker")["alpha"].apply(lambda a: (a > 0).mean())
    summary["per_ticker_alpha"] = per_ticker.round(5).reset_index().to_dict(orient="records")
    (out / f"summary_{label}.json").write_text(json.dumps(summary, indent=2))

    print(f"== {label}: {len(cells)} cells, {len(tickers) - len(missing)} tickers"
          + (f" (missing: {missing})" if missing else ""))
    print(f"{'strategy':<16}{'n_dir':>6}{'hit':>8}{'signed α':>10}{'net':>9}{'t':>7}"
          f"   95% CI (cells, iid | {block}-date blocks)")
    for k, r in summary["strategies"].items():
        if "hit_rate" not in r:
            continue
        ci, cd = r["ci95_signed_alpha"], r["ci95_signed_alpha_blocks"]
        t = f"{r['t_stat']:.2f}" if r["t_stat"] is not None else "n/a"
        print(f"{k:<16}{r['directional']:>6}{r['hit_rate']:>8.1%}{r['mean_signed_alpha']:>+10.2%}"
              f"{r['mean_signed_alpha_net']:>+9.2%}{t:>7}   [{ci[0]:+.2%}, {ci[1]:+.2%}]"
              + (f" | [{cd[0]:+.2%}, {cd[1]:+.2%}]" + (f" ({cd[2]} empty resamples)" if cd[2] else "")
                 if cd else " | too few dates"))
    rc = summary["random_control"]
    print(f"{'random (p50)':<16}{rc['cells']:>6}{rc['hit_rate']['p50']:>8.1%}"
          f"{rc['mean_signed_alpha']['p50']:>+10.2%}   hit 95% band "
          f"[{rc['hit_rate']['p2.5']:.1%}, {rc['hit_rate']['p97.5']:.1%}]")


if __name__ == "__main__":
    main()
