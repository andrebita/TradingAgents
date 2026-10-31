"""Score experiment 3: the single decider with the rebuilt news report against the old one.

Arms ``new`` and ``old`` come from the run's decisions log; ``exp2`` is
experiment 2's own first answer to the old package (its ``single`` arm), so
``old`` against ``exp2`` is the same question asked twice: the decider's noise.
Outcomes come from experiment 2's cells file (same dates and 20-session window).

Primary metric, fixed before the run: the paired Brier difference ``new - old``
per cell, averaged within each analysis date and then across the four dates,
with a t-interval over the window means and an exact sign-flip test (with four
windows the smallest two-sided p-value is 0.125).

Usage:
    python experiments/br_baseline/experiment3_eval.py OUT_DIR RUN_ID [--out report.json]
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
from experiment2_eval import arm_scores, paired

HERE = Path(__file__).resolve().parent
EXP2 = HERE / "results" / "experiment2"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("out_dir")
    p.add_argument("run_id")
    p.add_argument("--cells", default=str(EXP2 / "cells_exp2.csv"))
    p.add_argument("--exp2-decisions", default=str(EXP2 / "exp2_pilot_full_decisions.jsonl"))
    p.add_argument("--out")
    args = p.parse_args()
    out = Path(args.out_dir)

    def load(path):
        return pd.DataFrame([json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
                             if line.strip()])

    decisions = load(out / f"{args.run_id}_decisions.jsonl")
    ok = decisions[decisions["status"] == "ok"].drop_duplicates(["ticker", "date", "arm"], keep="last")
    wide = ok.pivot(index=["ticker", "date"], columns="arm", values="probability_outperform")
    wide.columns = [f"p_{c}" for c in wide.columns]
    exp2 = load(args.exp2_decisions)
    exp2 = exp2[(exp2["arm"] == "single") & (exp2["rep"] == 1) & (exp2["status"] == "ok")]
    wide["p_exp2"] = exp2.set_index(["ticker", "date"])["probability_outperform"].reindex(wide.index)

    cells = pd.read_csv(args.cells)
    frame = wide.reset_index().merge(cells[["ticker", "date", "holding", "alpha"]], on=["ticker", "date"], how="left")
    unscored = int(frame["alpha"].isna().sum())
    frame = frame.dropna(subset=["alpha"])
    frame["won"] = (frame["alpha"] > 0).astype(float)
    moved = (frame["p_new"] - frame["p_old"]).abs()
    noise = (frame["p_old"] - frame["p_exp2"]).abs()

    report = {"run_id": args.run_id, "cells_with_outcome": int(len(frame)), "cells_without_outcome": unscored,
              "share_beating_benchmark": float(frame["won"].mean()),
              "arms": {arm: arm_scores(frame, f"p_{arm}") for arm in ("new", "old", "exp2")},
              "paired_new_minus_old": paired(frame, "p_new", "p_old"),
              "paired_old_minus_exp2_noise": paired(frame, "p_old", "p_exp2"),
              "probability_shift_pp": {"mean_abs_new_vs_old": float(moved.mean()),
                                       "mean_abs_old_vs_exp2_noise": float(noise.mean()),
                                       "cells_moved_over_5pp": int((moved > 5).sum())},
              "failed_decisions": decisions[decisions["status"] != "ok"][["ticker", "date", "arm"]]
              .to_dict("records")}

    # What the rebuilt news reports drew on.
    reads = []
    for path in sorted((out / f"{args.run_id}_news_inputs").glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            calls = json.load(fh)
        news = [c.get("output", "") for c in calls if c.get("tool") == "get_news"]
        reads.append({"get_news_calls": len(news), "chars": sum(len(t) for t in news),
                      "filing_texts": sum(t.count("### Text of the filings") for t in news),
                      "press_texts": sum(t.count("### Text of the articles") for t in news)})
    if reads:
        r = pd.DataFrame(reads)
        report["news_inputs"] = {"cells": len(r), "cells_calling_get_news": int((r["get_news_calls"] > 0).sum()),
                                 "median_chars_read": float(r["chars"].median()),
                                 "cells_with_filing_text": int((r["filing_texts"] > 0).sum()),
                                 "cells_with_press_text": int((r["press_texts"] > 0).sum())}

    calls_path = out / f"{args.run_id}_calls.jsonl"
    if calls_path.exists():
        calls = load(calls_path)
        report["cost_usd_by_node"] = calls.groupby("node")["cost_usd"].sum().round(4).to_dict()
        report["cost_usd_total"] = round(float(calls["cost_usd"].sum()), 4)

    text = json.dumps(report, indent=2, ensure_ascii=False, default=lambda o: o.item() if isinstance(o, np.generic)
                      else str(o))
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
