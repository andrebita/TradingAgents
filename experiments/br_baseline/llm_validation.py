"""A small, budget-capped ``tradingagents backtest`` run with a real LLM.

Same ``run_backtest`` as the CLI, plus what a controlled first run needs:

* token usage per model, read from each response's ``usage_metadata``;
* a spend estimate from a price table, checked before every cell: the sweep
  stops once it passes ``--budget-usd`` (a re-run with the same ``--run-id``
  skips finished cells, settles them and goes on);
* a ``summary.json`` with the cells, failures, score and cost per cell.

Usage (Anthropic; needs ANTHROPIC_API_KEY, or TRADINGAGENTS_CLAUDE_KEY in cloud sessions):
    # Phase A: one cell
    python experiments/br_baseline/llm_validation.py --out experiments/br_baseline/results/llm_validation \
        --run-id claude_validation --tickers ITUB4.SA --start 2026-09-14 --end 2026-09-14
    # Phase B: 5 tickers x 2 dates (the Phase A cell is skipped)
    python experiments/br_baseline/llm_validation.py --out experiments/br_baseline/results/llm_validation \
        --run-id claude_validation --tickers ITUB4.SA,PSSA3.SA,SBSP3.SA,BBDC4.SA,B3SA3.SA \
        --start 2026-09-14 --end 2026-09-21 --every 7
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import os
import shutil
import threading
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents import backtest
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# US$ per million tokens (input, output), standard tier, checked 2026-10-30.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}


class OverBudget(RuntimeError):
    pass


class UsageByModel(BaseCallbackHandler):
    """Tokens and calls per model, from each response's usage metadata."""

    def __init__(self, fallback_model: str):
        self._lock = threading.Lock()
        self.fallback_model = fallback_model
        self.usage: dict[str, dict[str, int]] = {}

    def on_llm_end(self, response, **kwargs):
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None) or {}
                model = ((getattr(message, "response_metadata", None) or {}).get("model_name")
                         or (response.llm_output or {}).get("model_name") or self.fallback_model)
                with self._lock:
                    row = self.usage.setdefault(model, {"calls": 0, "input": 0, "output": 0})
                    row["calls"] += 1
                    row["input"] += meta.get("input_tokens", 0)
                    row["output"] += meta.get("output_tokens", 0)

    def cost(self) -> float:
        total = 0.0
        for model, row in self.usage.items():
            price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
            if price is None:
                raise OverBudget(f"no price for {model}; add it to PRICES before spending")
            total += (row["input"] * price[0] + row["output"] * price[1]) / 1e6
        return total


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--tickers", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--every", type=int, default=7)
    p.add_argument("--analysts", default="market")
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--quick", default="claude-haiku-5-5")
    p.add_argument("--deep", default="claude-sonnet-5-5")
    p.add_argument("--effort", default="low", help="anthropic_effort / openai_reasoning_effort / google_thinking_level")
    p.add_argument("--budget-usd", type=float, default=3.0)
    args = p.parse_args()

    key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}.get(args.provider)
    # Cloud sessions drop user-set ANTHROPIC_* variables (the host owns that
    # provider), so the project's key can also come from TRADINGAGENTS_CLAUDE_KEY.
    # It is set for this process only.
    if args.provider == "anthropic" and not os.environ.get(key) and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
        os.environ[key] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]
    if key and not os.environ.get(key):
        raise SystemExit(f"{key} is not set: add the project's own key to the environment first"
                         + (" (or TRADINGAGENTS_CLAUDE_KEY)" if args.provider == "anthropic" else ""))

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"),
                  llm_provider=args.provider, quick_think_llm=args.quick, deep_think_llm=args.deep,
                  checkpoint_enabled=False)
    effort_key = {"anthropic": "anthropic_effort", "openai": "openai_reasoning_effort",
                  "chatgpt": "openai_reasoning_effort", "google": "google_thinking_level"}[args.provider]
    config[effort_key] = args.effort

    usage = UsageByModel(fallback_model=args.quick)
    # run_backtest builds its own graph; hand it the usage callback.
    backtest.TradingAgentsGraph = functools.partial(TradingAgentsGraph, callbacks=[usage])

    def progress(done, total, ticker, date):
        spent = usage.cost()
        print(f"[{done}/{total}] {ticker} {date} · spent so far ~US$ {spent:.3f}", flush=True)
        if spent >= args.budget_usd:
            raise OverBudget(f"budget US$ {args.budget_usd} reached (~US$ {spent:.3f}); "
                             f"re-run with the same --run-id to settle and continue")

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    dates = iter_grid(args.start, args.end, args.every)
    stopped = None
    try:
        result = run_backtest(tickers, dates, config, selected_analysts=args.analysts.split(","),
                              run_id=args.run_id, progress=progress)
    except OverBudget as exc:
        stopped = str(exc)
        print(stopped)
        result = None

    log = out / "results" / "backtest" / args.run_id / "trading_memory.md"
    report = {"run_id": args.run_id, "provider": args.provider, "quick": args.quick, "deep": args.deep,
              "effort": args.effort, "analysts": args.analysts, "tickers": tickers, "dates": dates,
              "budget_usd": args.budget_usd, "stopped": stopped, "usage": usage.usage,
              "cost_usd_this_invocation": round(usage.cost(), 4)}
    if result is not None:
        report.update(cells_run=result.cells_run, skipped=result.skipped, failures=result.failures,
                      settlement_failures=result.settlement_failures)
        if result.cells_run:
            report["cost_usd_per_cell"] = round(usage.cost() / result.cells_run, 4)
    if log.is_file():
        summary = summarize(log)
        report["score"] = {"resolved": summary.resolved, "pending": summary.pending,
                           "by_rating": {k: vars(v) for k, v in summary.by_rating.items()}}
        print(summary.render())
        shutil.copy(log, out / f"{args.run_id}_trading_memory.md")
    history = out / f"{args.run_id}_invocations.jsonl"
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps({k: report.get(k) for k in ("usage", "cost_usd_this_invocation", "cost_usd_per_cell",
                                                  "cells_run", "skipped", "failures", "stopped")}, indent=2))


if __name__ == "__main__":
    main()
