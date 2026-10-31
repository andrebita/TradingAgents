"""Experiment 3: does real news change the decision?

Experiment 2 froze, for each of its 60 cells, the evidence package its final
decider read: the market (E1), news (E2) and fundamentals (E3) analysts'
reports, a deterministic market snapshot (E4) and the evaluation contract. Its
news analyst saw only the titles of CVM filings. This experiment reruns that one
analyst per cell with the current news vendor (``b3``: the text of material
facts and notices, and Brazilian press articles), puts its report in place of
E2 in an otherwise byte-identical package, and asks experiment 2's single
decider (same prompt, schema, model and effort, no deliberation) about both:

* ``old``: experiment 2's package, asked again;
* ``new``: the same package with the rebuilt news report.

Asking ``old`` again, rather than reusing experiment 2's answers, puts both arms
on the same code and day; the gap between experiment 2's answer and this one
measures the decider's own noise on every cell.

What the news analyst read (each tool call and its full output) is kept beside
the packages, so the point-in-time filtering can be audited cell by cell.

Usage (needs ANTHROPIC_API_KEY, or TRADINGAGENTS_CLAUDE_KEY in cloud sessions):
    python experiments/br_baseline/experiment3.py --run-id exp3_news --workers 3 --budget-usd 6
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from experiment2 import PROMPT, DeciderOutput, make_decider, render_prompt, sha
from langchain_core.callbacks import BaseCallbackHandler
from llm_validation import FallbackCounter, OverBudget, SpendGuard, build_manifest, check_manifest

from tradingagents.agents import create_news_analyst
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.setup import _analyst_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph

HERE = Path(__file__).resolve().parent
ARMS = ("old", "new")


class ToolLog(BaseCallbackHandler):
    """Every tool call of one cell's news analyst, with its full output."""

    def __init__(self):
        self.calls, self._open = [], {}

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._open[run_id] = {"tool": (serialized or {}).get("name") or kwargs.get("name"), "input": input_str}

    def on_tool_end(self, output, *, run_id, **kwargs):
        call = self._open.pop(run_id, {"tool": kwargs.get("name"), "input": None})
        self.calls.append({**call, "output": str(getattr(output, "content", output))})

    def on_tool_error(self, error, *, run_id, **kwargs):
        call = self._open.pop(run_id, {"tool": kwargs.get("name"), "input": None})
        self.calls.append({**call, "error": f"{type(error).__name__}: {error}"})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "experiment3"))
    p.add_argument("--run-id", required=True)
    p.add_argument("--source", default=str(HERE / "results" / "experiment2" / "exp2_pilot_full_packages"),
                   help="experiment 2's frozen packages")
    p.add_argument("--holding", type=int, default=20)
    p.add_argument("--quick", default="claude-haiku-5-5")
    p.add_argument("--effort", default="medium")
    p.add_argument("--decider", default="claude-sonnet-5-5")
    p.add_argument("--decider-effort", default="medium")
    p.add_argument("--limit", type=int, default=0, help="only the first N cells (a dry run)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--budget-usd", type=float, default=6.0)
    p.add_argument("--allow-dirty", action="store_true")
    args = p.parse_args()
    args.provider, args.deep, args.analysts, args.memory = "anthropic", args.decider, "news", "off"

    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (or TRADINGAGENTS_CLAUDE_KEY)")

    sources = {}
    for path in sorted(Path(args.source).glob("*.json")):
        frozen = json.loads(path.read_text(encoding="utf-8"))
        if sha(frozen["package"]) != frozen["sha256"]:
            raise SystemExit(f"{path.name} does not match its recorded SHA-256: the package was altered")
        sources[(frozen["package"]["ticker"], frozen["package"]["date"])] = frozen
    cells = sorted(sources, key=lambda c: (c[1], c[0]))[:args.limit or None]
    if not cells:
        raise SystemExit(f"no packages in {args.source}")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"), llm_provider="anthropic",
                  quick_think_llm=args.quick, deep_think_llm=args.decider, checkpoint_enabled=False,
                  holding_period_days=args.holding, use_decision_memory=False, anthropic_effort=args.effort)
    manifest = {**build_manifest(args, config), "experiment": "experiment3", "decider": args.decider,
                "decider_effort": args.decider_effort, "prompt_sha": sha(PROMPT),
                "schema_sha": sha(DeciderOutput.model_json_schema()),
                "source_packages_sha": sha(sorted(s["sha256"] for s in sources.values())),
                "text_budgets": {k: config[k] for k in ("cvm_filing_text_chars", "br_press_text_chars")}}
    if manifest["code"].endswith("+dirty") and not args.allow_dirty:
        raise SystemExit("uncommitted changes to tracked files: commit them, or pass --allow-dirty")
    check_manifest(out / f"{args.run_id}_manifest.json", manifest)

    guard = SpendGuard(args.budget_usd, out / f"{args.run_id}_calls.jsonl", fallback_model=args.quick)
    fallbacks = FallbackCounter()
    logging.getLogger("tradingagents").addHandler(fallbacks)
    began = time.monotonic()
    packages, read = out / f"{args.run_id}_packages", out / f"{args.run_id}_news_inputs"
    packages.mkdir(exist_ok=True)
    read.mkdir(exist_ok=True)
    decisions_path = out / f"{args.run_id}_decisions.jsonl"
    done = set()
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["status"] == "ok":
                done.add((row["ticker"], row["date"], row["arm"]))
    lock, local = threading.Lock(), threading.local()
    failures, stopped = [], None
    spec = build_analyst_execution_plan(["news"]).specs[0]
    decider = make_decider(config, args.decider, args.decider_effort, guard)

    def news_report(ticker: str, date: str) -> tuple[str, list[dict]]:
        """The news analyst alone, exactly as the graph runs it, on this cell."""
        if not hasattr(local, "graph"):
            local.ta = TradingAgentsGraph(selected_analysts=["news"], config=config, callbacks=[guard])
            local.graph = _analyst_graph(spec, create_news_analyst(local.ta.quick_thinking_llm),
                                         config["max_tool_rounds"])
        tools = ToolLog()
        result = local.graph.invoke(local.ta.create_run_state(ticker, date),
                                    config={"recursion_limit": config["max_recur_limit"], "callbacks": [tools],
                                            "metadata": {"langgraph_node": "news analyst"}})
        return result.get(spec.report_key, ""), tools.calls

    def run(cell):
        ticker, date = cell
        target = packages / f"{ticker}_{date}.json"
        if target.exists():
            rebuilt = json.loads(target.read_text(encoding="utf-8"))
        else:
            try:
                report, calls = news_report(ticker, date)
            except OverBudget:
                raise
            except Exception as exc:
                failures.append({"ticker": ticker, "date": date, "error": f"{type(exc).__name__}: {exc}"[:500]})
                print(f"[news] {ticker} {date}: FAILED {exc}", flush=True)
                return
            if not report.strip():
                failures.append({"ticker": ticker, "date": date, "error": "empty news report"})
                return
            package = copy.deepcopy(sources[cell]["package"])
            next(e for e in package["evidence"] if e["id"] == "E2")["text"] = report
            with gzip.open(read / f"{ticker}_{date}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(calls, fh, ensure_ascii=False)
            rebuilt = {"sha256": sha(package), "package": package, "source_sha256": sources[cell]["sha256"],
                       "news_tools": [c["tool"] for c in calls]}
            target.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[news] {ticker} {date}: {len(report)} chars, tools {rebuilt['news_tools']} · "
                  f"{time.monotonic() - began:.0f}s · US$ {guard.total():.3f}", flush=True)
        for arm, frozen in (("old", sources[cell]), ("new", rebuilt)):
            if (ticker, date, arm) in done:
                continue
            row = {"ticker": ticker, "date": date, "arm": arm, "package_sha": frozen["sha256"]}
            try:
                answer = decider.invoke(render_prompt(frozen["package"], None),
                                        config={"metadata": {"langgraph_node": f"decider:{arm}"}})
                row.update(status="ok", **answer.model_dump(mode="json"))
            except OverBudget:
                raise
            except Exception as exc:   # a refusal, a truncation, an invalid probability: failed measurement
                row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
            with lock, decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[decide] {ticker} {date} {arm}: {row.get('probability_outperform', row['status'])}"
                  f" · US$ {guard.total():.3f}", flush=True)

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for future in [pool.submit(run, c) for c in cells]:
                future.result()
    except OverBudget as exc:
        stopped = str(exc)
    logging.getLogger("tradingagents").removeHandler(fallbacks)

    report = {"run_id": args.run_id, "manifest": manifest, "cells": len(cells),
              "wall_seconds": round(time.monotonic() - began), "stopped": stopped, "failures": failures,
              "usage": guard.usage, "calls_by_node": guard.by_node, "structured_fallbacks": fallbacks.counts,
              "cost_usd_this_invocation": round(guard.spent, 4), "cost_usd_run_id_total": round(guard.total(), 4)}
    with (out / f"{args.run_id}_invocations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps({k: report[k] for k in ("calls_by_node", "structured_fallbacks", "cost_usd_this_invocation",
                                             "wall_seconds", "failures", "stopped")}, indent=2))


if __name__ == "__main__":
    main()
