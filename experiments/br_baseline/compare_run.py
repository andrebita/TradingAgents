"""Place a ``tradingagents backtest`` run against the rule baselines on the same cells.

Reads the run's memory log (ratings and the alpha the framework settled) and a
``cells_*.csv`` written by ``baselines.py`` over the same tickers and grid, and
reports, on the cells both have:

* the run's hit rate and mean signed alpha, scored as ``summarize`` scores
  (Buy/Overweight = long, Underweight/Sell = short, Hold claims nothing);
* every rule baseline restricted to those same cells;
* the band a random direction reaches on those cells (1000 seeds);
* the largest gap between the framework's settled alpha and the baseline's
  recomputation, as a check that both measure the same outcome.

Usage:
    python experiments/br_baseline/compare_run.py LOG.md CELLS.csv [--out report.json]
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from tradingagents.memory import TradingMemoryLog

DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


def pct(text: str | None) -> float | None:
    try:
        return float((text or "").strip().rstrip("%")) / 100
    except ValueError:
        return None


def stats(dirs: np.ndarray, alphas: np.ndarray) -> dict:
    m = dirs != 0
    if not m.any():
        return {"directional": 0}
    signed = dirs[m] * alphas[m]
    return {"directional": int(m.sum()), "hit_rate": float((signed > 0).mean()),
            "mean_signed_alpha": float(signed.mean())}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("log")
    p.add_argument("cells")
    p.add_argument("--out")
    args = p.parse_args()

    entries = TradingMemoryLog({"memory_log_path": args.log}).load_entries()
    run = pd.DataFrame([{"ticker": e["ticker"], "date": e["date"], "rating": e["rating"],
                         "native_alpha": pct(e.get("alpha"))}
                        for e in entries if not e["pending"] and e["rating"] in DIRECTION])
    cells = pd.read_csv(args.cells)
    both = run.merge(cells, on=["ticker", "date"], how="inner").dropna(subset=["native_alpha"])
    if both.empty:
        raise SystemExit("no settled cell of the run is in the baseline grid")

    # The log keeps alpha to 0.1 pp, so gaps up to 0.05 pp are rounding.
    gap = (both["native_alpha"] - both["alpha"]).abs()
    dirs = both["rating"].map(DIRECTION).to_numpy(dtype=int)
    report = {
        "cells": int(len(both)), "run_cells_not_in_grid": int(len(run) - len(both)),
        "ratings": both["rating"].value_counts().to_dict(),
        "alpha_check_max_gap_pp": float(gap.max() * 100),
        "alpha_check_cells_over_0.1pp": int((gap > 0.001).sum()),
        "run": stats(dirs, both["native_alpha"].to_numpy(dtype=float)),
        "baselines": {},
    }
    for col in [c for c in both.columns if c.startswith("sig_")]:
        sub = both.dropna(subset=[col])
        report["baselines"][col[4:]] = stats(sub[col].to_numpy(dtype=int), sub["alpha"].to_numpy(dtype=float))

    rng = np.random.default_rng(7)
    m = dirs != 0
    a = both["alpha"].to_numpy(dtype=float)[m]
    hits = [float(((rng.choice([-1, 1], size=len(a)) * a) > 0).mean()) for _ in range(1000)]
    report["random_band_hit_rate_95"] = [float(np.percentile(hits, 2.5)), float(np.percentile(hits, 97.5))]

    print(json.dumps(report, indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)


if __name__ == "__main__":
    main()
