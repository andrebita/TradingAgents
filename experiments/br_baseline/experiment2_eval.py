"""Score experiment 2: the full arm against the single decider, on the same outcomes.

Outcomes come from a ``baselines.py`` cells file built over the same dates and
holding window (unrounded alpha; 1 when the stock beat the benchmark). Arms:
``full`` and ``single`` from the decisions log (first ask only), and
``original``, the graph's own Portfolio Manager probability from the state logs.

Primary metric: the paired Brier difference per cell,
``delta = (p_full - y)^2 - (p_single - y)^2`` (negative favours the full arm),
averaged within each analysis date (window) and then across windows. With few
windows the interval is a t-interval over window means (n - 1 degrees of
freedom) and the test an exact sign-flip test over them: with 4 windows the
smallest attainable two-sided p-value is 0.125, so this is a pilot, not proof.

Usage:
    python experiments/br_baseline/experiment2_eval.py OUT_DIR RUN_ID CELLS.csv [--out report.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

# Two-sided 95% t quantiles by degrees of freedom.
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t975(df: int) -> float:
    return T975.get(df) or T975[max(k for k in T975 if k <= df)]


def sign_flip_p(values: list[float]) -> float:
    """Exact two-sided sign-flip p-value for a mean of zero."""
    observed = abs(np.mean(values))
    flips = [abs(np.mean([s * v for s, v in zip(signs, values, strict=True)]))
             for signs in itertools.product((1, -1), repeat=len(values))]
    return float(np.mean([f >= observed - 1e-15 for f in flips]))


def arm_scores(frame: pd.DataFrame, col: str) -> dict:
    ok = frame.dropna(subset=[col])
    if ok.empty:
        return {"cells": 0}
    p, y, a = ok[col].to_numpy() / 100, ok["won"].to_numpy(), ok["alpha"].to_numpy()
    lean = p != 0.5
    per_date = [g[col].rank().corr(g["alpha"].rank()) for _, g in ok.groupby("date") if len(g) >= 3]
    per_date = [r for r in per_date if not np.isnan(r)]
    return {"cells": int(len(ok)), "coverage": float(len(ok) / len(frame)), "brier": float(((p - y) ** 2).mean()),
            "mean_probability": float(p.mean()), "share_above_50": float((p > 0.5).mean()),
            "share_below_50": float((p < 0.5).mean()),
            "hit_rate_of_lean": float(((p[lean] > 0.5) == (a[lean] > 0)).mean()) if lean.any() else None,
            "rank_ic_mean_by_date": float(np.mean(per_date)) if per_date else None}


def paired(frame: pd.DataFrame, a: str, b: str) -> dict:
    both = frame.dropna(subset=[a, b]).copy()
    if both.empty:
        return {"cells": 0}
    y = both["won"]
    both["delta"] = (both[a] / 100 - y) ** 2 - (both[b] / 100 - y) ** 2
    windows = both.groupby("date")["delta"].mean()
    out = {"cells": int(len(both)), "windows": int(len(windows)), "mean_delta": float(both["delta"].mean()),
           "mean_delta_by_window": {d: round(float(v), 6) for d, v in windows.items()},
           "mean_of_window_means": float(windows.mean())}
    if len(windows) >= 2:
        half = t975(len(windows) - 1) * windows.std(ddof=1) / np.sqrt(len(windows))
        out["ci95_over_windows"] = [float(windows.mean() - half), float(windows.mean() + half)]
        out["sign_flip_p_over_windows"] = sign_flip_p(list(windows))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_dir")
    p.add_argument("run_id")
    p.add_argument("cells")
    p.add_argument("--out")
    args = p.parse_args()
    out = Path(args.out_dir)

    rows = [json.loads(line) for line in (out / f"{args.run_id}_decisions.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    decisions = pd.DataFrame(rows)
    first = decisions[decisions["rep"] == 1].drop_duplicates(["ticker", "date", "arm"], keep="last")
    wide = first.pivot(index=["ticker", "date"], columns="arm", values="probability_outperform")
    status = first.pivot(index=["ticker", "date"], columns="arm", values="status")
    wide.columns = [f"p_{c}" for c in wide.columns]
    for arm in ("full", "single"):
        if f"p_{arm}" in wide:
            wide.loc[status[arm] != "ok", f"p_{arm}"] = np.nan

    original = {}
    run_dir = out / "results" / "backtest" / args.run_id
    for path in run_dir.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json"):
        state = json.loads(path.read_text(encoding="utf-8"))
        original[(state["company_of_interest"], state["trade_date"])] = state.get("final_probability")
    wide["p_original"] = [original.get(k) for k in wide.index]

    cells = pd.read_csv(args.cells)
    frame = wide.reset_index().merge(cells[["ticker", "date", "holding", "alpha"]], on=["ticker", "date"],
                                     how="left")
    unscored = frame["alpha"].isna().sum()
    frame = frame.dropna(subset=["alpha"])
    frame["won"] = (frame["alpha"] > 0).astype(float)

    report = {"run_id": args.run_id, "cells_with_outcome": int(len(frame)), "cells_without_outcome": int(unscored),
              "holding": sorted(set(frame["holding"])),
              "share_beating_benchmark": float(frame["won"].mean()) if len(frame) else None,
              "arms": {arm: arm_scores(frame, f"p_{arm}") for arm in ("full", "single", "original")
                       if f"p_{arm}" in frame},
              "paired_full_minus_single": paired(frame, "p_full", "p_single"),
              "paired_original_minus_single": paired(frame, "p_original", "p_single"),
              "failed_decisions": decisions[decisions["status"] != "ok"][["ticker", "date", "arm", "rep",
                                                                           "error"]].to_dict("records")
              if "error" in decisions else []}

    repeats = decisions[decisions["status"] == "ok"].groupby(["ticker", "date", "arm"])["probability_outperform"]
    spread = repeats.agg(lambda s: s.max() - s.min() if len(s) > 1 else np.nan).dropna()
    report["repeat_spread_pp"] = ({"cells": int(len(spread)), "mean": float(spread.mean()),
                                   "max": float(spread.max())} if len(spread) else None)

    calls_path = out / f"{args.run_id}_calls.jsonl"
    if calls_path.exists():
        calls = pd.DataFrame([json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()])
        report["cost_usd_by_node"] = calls.groupby("node")["cost_usd"].sum().round(4).to_dict()
        report["cost_usd_total"] = round(float(calls["cost_usd"].sum()), 4)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
