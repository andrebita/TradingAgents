"""A small, budget-capped ``tradingagents backtest`` run with a real LLM.

Same ``run_backtest`` as the CLI, plus what a controlled first run needs:

* token usage per model, read from each response's ``usage_metadata``;
* a spend estimate from a price table, checked before every model call and
  carried across re-runs of the same ``--run-id``: the run stops once it passes
  ``--budget-usd`` (one call can still finish past it), and a response without
  usage metadata stops it too, since its cost is unknown;
* a manifest per ``--run-id`` (models, effective effort, analysts, horizon,
  vendors, code commit): a re-run that differs is refused, so one run's cells
  are never mixed with another configuration's;
* an invocation log with the cells, failures, score and cost per cell.

Usage (Anthropic; needs ANTHROPIC_API_KEY, or TRADINGAGENTS_CLAUDE_KEY in cloud sessions):
    # Phase A: one cell
    python experiments/br_baseline/llm_validation.py --out experiments/br_baseline/results/llm_validation \
        --run-id claude_validation --tickers ITUB4.SA --start 2026-09-14 --end 2026-09-14
    # Phase B: 5 tickers x 2 dates (the Phase A cell is skipped)
    python experiments/br_baseline/llm_validation.py --out experiments/br_baseline/results/llm_validation \
        --run-id claude_validation --tickers ITUB4.SA,PSSA3.SA,SBSP3.SA,BBDC4.SA,B3SA3.SA \
        --start 2026-09-14 --end 2026-09-21 --every 7
    # Longer horizon: --holding 20 judges each call over 20 trading days (~1 month);
    # the agents are told the horizon, and baselines.py takes the same --holding.
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents import backtest
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.anthropic_client import _supports_effort

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


class OverBudget(BaseException):
    """Stops the run. A BaseException, so the framework's per-cell and
    fallback handlers (``except Exception``) cannot turn it into more calls."""


class UsageByModel(BaseCallbackHandler):
    """Tokens and calls per model, from each response's usage metadata, and the
    budget check made before every call."""

    raise_error = True

    def __init__(self, fallback_model: str, budget_usd: float, spent_before: float):
        self._lock = threading.Lock()
        self.fallback_model = fallback_model
        self.budget_usd = budget_usd
        self.spent_before = spent_before
        self.usage: dict[str, dict[str, int]] = {}

    def _check(self):
        spent = self.spent_before + self.cost()
        if spent >= self.budget_usd:
            raise OverBudget(f"budget US$ {self.budget_usd} reached (~US$ {spent:.3f} across this run_id)")

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self._check()

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._check()

    def on_llm_end(self, response, **kwargs):
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None)
                if not meta:
                    raise OverBudget("a response carried no usage metadata: its cost is unknown, so the "
                                     "budget cannot be enforced")
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


def code_commit() -> str:
    """The commit the run's code is at, marked when the tree has local changes."""
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=False).stdout.strip()
    head = git("rev-parse", "HEAD") or "unknown"
    return head + ("+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def build_manifest(args, config) -> dict:
    """Every setting that changes what a cell means; two runs sharing a run_id must agree on all."""
    effort_key = {"anthropic": "anthropic_effort", "openai": "openai_reasoning_effort",
                  "chatgpt": "openai_reasoning_effort", "google": "google_thinking_level"}[args.provider]

    def effective(model):
        if args.provider == "anthropic" and not _supports_effort(model):
            return "provider default (effort not sent)"
        return args.effort

    return {"provider": args.provider, "quick": args.quick, "deep": args.deep, "effort_setting": effort_key,
            "effort_effective": {"quick": effective(args.quick), "deep": effective(args.deep)},
            "analysts": sorted(args.analysts.split(",")), "holding_days": args.holding,
            "benchmark": config["benchmark_ticker"] or config["benchmark_map"].get(".SA"),
            "data_vendors": config["data_vendors"], "tool_vendors": config["tool_vendors"],
            "debate_rounds": config["max_debate_rounds"], "risk_rounds": config["max_risk_discuss_rounds"],
            "memory": "own log per run_id; lessons filtered by resolution date", "code": code_commit()}


def check_manifest(path: Path, manifest: dict) -> None:
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        differ = sorted(k for k in set(stored) | set(manifest) if stored.get(k) != manifest.get(k))
        if differ:
            raise SystemExit(f"run_id belongs to another configuration (differs in: {', '.join(differ)}); "
                             "use a new --run-id")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def spent_so_far(history: Path) -> float:
    if not history.exists():
        return 0.0
    return sum(json.loads(line).get("cost_usd_this_invocation", 0.0)
               for line in history.read_text(encoding="utf-8").splitlines() if line.strip())


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
    p.add_argument("--holding", type=int, default=5, help="trading days a decision is judged over")
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
                  checkpoint_enabled=False, holding_period_days=args.holding)
    effort_key = {"anthropic": "anthropic_effort", "openai": "openai_reasoning_effort",
                  "chatgpt": "openai_reasoning_effort", "google": "google_thinking_level"}[args.provider]
    config[effort_key] = args.effort

    manifest = build_manifest(args, config)
    check_manifest(out / "results" / "backtest" / args.run_id / "manifest.json", manifest)
    history = out / f"{args.run_id}_invocations.jsonl"
    usage = UsageByModel(fallback_model=args.quick, budget_usd=args.budget_usd,
                         spent_before=spent_so_far(history))

    def progress(done, total, ticker, date):
        spent = usage.spent_before + usage.cost()
        print(f"[{done}/{total}] {ticker} {date} · spent so far ~US$ {spent:.3f}", flush=True)

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    dates = iter_grid(args.start, args.end, args.every)
    stopped = None
    # run_backtest builds its own graph; hand it the usage callback for this run only.
    original = backtest.TradingAgentsGraph
    backtest.TradingAgentsGraph = functools.partial(TradingAgentsGraph, callbacks=[usage])
    try:
        result = run_backtest(tickers, dates, config, selected_analysts=args.analysts.split(","),
                              run_id=args.run_id, progress=progress)
    except OverBudget as exc:
        stopped = f"{exc}; re-run with the same --run-id and a higher --budget-usd to continue"
        print(stopped)
        result = None
    finally:
        backtest.TradingAgentsGraph = original

    log = out / "results" / "backtest" / args.run_id / "trading_memory.md"
    report = {"run_id": args.run_id, "manifest": manifest, "tickers": tickers, "dates": dates,
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
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps({k: report.get(k) for k in ("usage", "cost_usd_this_invocation", "cost_usd_per_cell",
                                                  "cells_run", "skipped", "failures", "stopped")}, indent=2))


if __name__ == "__main__":
    main()
