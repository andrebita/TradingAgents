"""Score the dividend-trap filter against the pre-registered criteria (README, "Filtro de armadilhas").

Primary: the AUC of the LLM's cut probability for the cuts that followed, with a
95% interval from resampling the rebalance dates, against the AUC of the no-LLM
rule. Secondary: Brier score, cuts avoided, and the top-10 portfolio with no
filter, the LLM filter (probability of 50% or more dropped) and the rule filter
(any flag dropped). The identification probe's answers are listed for review.

Usage:
    python experiments/br_baseline/dividend_filter_eval.py [--run-id filter_v1]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
HOLD, FLAG = 10, 50


def auc(score: np.ndarray, label: np.ndarray) -> float:
    """Probability that a random cut scores above a random non-cut (ties count half)."""
    pos, neg = score[label], score[~label]
    if not len(pos) or not len(neg):
        return float("nan")
    greater = (pos[:, None] > neg[None, :]).mean()
    ties = (pos[:, None] == neg[None, :]).mean()
    return float(greater + 0.5 * ties)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "dividend_filter"))
    p.add_argument("--run-id", default="filter_v1")
    args = p.parse_args()
    out = Path(args.out)

    cells = pd.DataFrame(json.loads((out / "candidates.json").read_text(encoding="utf-8")))
    answers = pd.DataFrame([json.loads(line) for line in
                            (out / f"{args.run_id}_answers.jsonl").read_text(encoding="utf-8").splitlines()])
    assess = answers[(answers["task"] == "assess") & (answers["status"] == "ok")].drop_duplicates(
        ["t", "cnpj"], keep="last")[["t", "cnpj", "probability_cut"]]
    frame = cells.merge(assess, on=["t", "cnpj"], how="left")
    scored = frame.dropna(subset=["probability_cut"])
    known = scored[scored["cut"].notna()].copy()
    known["cut"] = known["cut"].astype(bool)

    rng = np.random.default_rng(11)
    dates = known["t"].unique()
    by_date = dict(list(known.groupby("t")))
    boots = []
    for _ in range(2000):
        sample = pd.concat([by_date[d] for d in rng.choice(dates, len(dates))])
        y = sample["cut"].to_numpy()
        boots.append((auc(sample["probability_cut"].to_numpy(), y), auc(sample["rule_flags"].to_numpy(), y)))
    boots = np.array(boots)
    y = known["cut"].to_numpy()
    llm_auc, rule_auc = auc(known["probability_cut"].to_numpy(), y), auc(known["rule_flags"].to_numpy(), y)
    prob = known["probability_cut"].to_numpy() / 100
    report = {
        "cells": int(len(frame)), "assessed": int(len(scored)), "with_outcome": int(len(known)),
        "failed": int((answers["status"] != "ok").sum()), "cut_rate": float(y.mean()),
        "auc_llm": llm_auc, "auc_llm_ci95": [float(np.nanpercentile(boots[:, 0], 2.5)),
                                            float(np.nanpercentile(boots[:, 0], 97.5))],
        "auc_rule": rule_auc, "auc_rule_ci95": [float(np.nanpercentile(boots[:, 1], 2.5)),
                                              float(np.nanpercentile(boots[:, 1], 97.5))],
        "auc_llm_minus_rule_ci95": [float(np.nanpercentile(boots[:, 0] - boots[:, 1], 2.5)),
                                    float(np.nanpercentile(boots[:, 0] - boots[:, 1], 97.5))],
        "brier_llm": float(((prob - y) ** 2).mean()), "brier_base_rate": float(((y.mean() - y) ** 2).mean()),
        "mean_probability": float(prob.mean()),
        "flagged_llm": int((known["probability_cut"] >= FLAG).sum()),
        "cut_rate_if_flagged_llm": float(known[known["probability_cut"] >= FLAG]["cut"].mean())
        if (known["probability_cut"] >= FLAG).any() else None,
        "flagged_rule": int((known["rule_flags"] >= 1).sum()),
        "cut_rate_if_flagged_rule": float(known[known["rule_flags"] >= 1]["cut"].mean()),
    }
    report["passes"] = bool(report["auc_llm_ci95"][0] > 0.5 and llm_auc > rule_auc)

    # Portfolios: the 10 highest yields among each date's candidates, after each filter.
    rows = []
    for t, g in scored.groupby("t"):
        g = g.dropna(subset=["ret"])
        picks = {"none": g.nlargest(HOLD, "dy24"),
                 "llm": g[g["probability_cut"] < FLAG].nlargest(HOLD, "dy24"),
                 "rule": g[g["rule_flags"] < 1].nlargest(HOLD, "dy24")}
        rows.append({"t": t, **{f"ret_{k}": float(v["ret"].mean()) for k, v in picks.items()},
                     **{f"cuts_{k}": int((v["cut"] == True).sum()) for k, v in picks.items()}})  # noqa: E712
    port = pd.DataFrame(rows)
    report["portfolio"] = {}
    for k in ("none", "llm", "rule"):
        r = port[f"ret_{k}"]
        report["portfolio"][k] = {"cagr": float((1 + r).prod() ** (2 / len(r)) - 1), "cuts_held": int(port[f"cuts_{k}"].sum())}
    for k in ("llm", "rule"):
        d = port[f"ret_{k}"] - port["ret_none"]
        report["portfolio"][f"{k}_minus_none"] = {"mean_semiannual": float(d.mean()),
                                                  "t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))),
                                                  "periods_positive": int((d > 0).sum()), "periods": int(len(d))}
    d = port["ret_llm"] - port["ret_rule"]
    report["portfolio"]["llm_minus_rule"] = {"mean_semiannual": float(d.mean()),
                                             "t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))))}

    probe = answers[(answers["task"] == "identify") & (answers["status"] == "ok")]
    report["probe"] = [{"code": r["code"], "t": r["t"], "guess": r["guess"], "confidence": r["confidence"]}
                       for _, r in probe.iterrows()]
    calls = out / f"{args.run_id}_calls.jsonl"
    if calls.exists():
        c = pd.DataFrame([json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()])
        report["cost_usd"] = round(float(c["cost_usd"].sum()), 4)
    port.to_csv(out / f"{args.run_id}_portfolios.csv", index=False)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    (out / f"{args.run_id}_eval.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
