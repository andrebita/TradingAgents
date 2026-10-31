"""A small, budget-capped ``tradingagents backtest`` run with a real LLM.

Same ``run_backtest`` as the CLI, plus what a controlled run needs:

* a spend cap that reserves an estimate for every model call before it is sent
  (completed calls at their measured cost, calls in flight at their reservation),
  carried across re-runs of the same ``--run-id``; once it would be passed, no
  further call starts in any worker. A response without usage metadata also
  stops the run, since its cost is unknown. Requests already sent still finish,
  so the overshoot is bounded by the reservations in flight;
* a per-call log written as each call ends (graph node, model, tokens, cost,
  latency), which survives an interrupted run and is what the cap reads back;
* a manifest per ``--run-id`` (models, effective effort, analysts, horizon,
  memory policy, workers, vendors, dependency versions, code commit): a re-run
  that differs is refused, and uncommitted code is refused unless
  ``--allow-dirty``, so one run's cells are never mixed with another
  configuration's;
* a fingerprint of the downloaded data per invocation, so a re-run that read
  different files is visible;
* an invocation log with the cells, failures, score, structured-output
  fallbacks and cost per cell.

Usage (Anthropic; needs ANTHROPIC_API_KEY, or TRADINGAGENTS_CLAUDE_KEY in cloud sessions):
    python experiments/br_baseline/llm_validation.py --out experiments/br_baseline/results/llm_validation \\
        --run-id diag_E_r1 --tickers ITUB4.SA,PSSA3.SA --start 2026-09-14 --end 2026-09-21 --every 7 \\
        --analysts market,news,fundamentals --holding 20 --effort medium --workers 2 --budget-usd 1.5
    # --holding N judges each call over N trading days, and the agents are told so;
    # baselines.py takes the same --holding. Decision memory is off unless --memory on.
"""

from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents import backtest
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.anthropic_client import _supports_effort

# US$ per million tokens (input, output), standard tier, checked 2026-10-30, and the
# prompt size above which a model bills its long-context rate.
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
LONG_CONTEXT = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}

# What one call is assumed to cost before it is sent: generous token counts, so a
# reservation is rarely below the bill. Replaced by the measured cost when it ends.
RESERVE_TOKENS = (60_000, 12_000)

DEPENDENCIES = ("anthropic", "langchain-core", "langchain-anthropic", "langgraph", "yfinance", "pandas")


class OverBudget(BaseException):
    """Stops the run. A BaseException, so the framework's per-cell and
    fallback handlers (``except Exception``) cannot turn it into more calls."""


def price_of(model: str) -> tuple[float, float]:
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    raise OverBudget(f"no price for {model}; add it to PRICES before spending")


def call_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price = price_of(model)
    for name, (threshold, long_price) in LONG_CONTEXT.items():
        if model.startswith(name) and tokens_in > threshold:
            price = long_price
    return (tokens_in * price[0] + tokens_out * price[1]) / 1e6


class SpendGuard(BaseCallbackHandler):
    """Reserves, records and caps the cost of every model call, across workers."""

    raise_error = True

    def __init__(self, budget_usd: float, calls_log: Path, fallback_model: str):
        self._lock = threading.Lock()
        self.budget_usd = budget_usd
        self.calls_log = calls_log
        self.fallback_model = fallback_model
        self.spent_before = sum(json.loads(line)["cost_usd"]
                                for line in calls_log.read_text(encoding="utf-8").splitlines()
                                if line.strip()) if calls_log.exists() else 0.0
        self.spent = 0.0
        self.in_flight: dict = {}          # run_id -> (reservation, model, node, start)
        self.stopped: str | None = None
        self.usage: dict[str, dict[str, int]] = {}
        self.by_node: dict[str, int] = {}

    def _stop(self, reason: str) -> None:
        self.stopped = self.stopped or reason
        raise OverBudget(self.stopped)

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        metadata = metadata or {}
        model = metadata.get("ls_model_name") or self.fallback_model
        node = metadata.get("langgraph_node") or "settlement/other"
        with self._lock:
            if self.stopped:
                raise OverBudget(self.stopped)
            reservation = call_cost(model, *RESERVE_TOKENS)
            committed = self.spent_before + self.spent + sum(r[0] for r in self.in_flight.values())
            if committed + reservation > self.budget_usd:
                self._stop(f"budget US$ {self.budget_usd} would be passed: ~US$ {committed:.3f} spent or "
                           f"in flight across this run_id, next call reserves US$ {reservation:.3f}")
            self.in_flight[run_id] = (reservation, model, node, time.monotonic())

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            self.in_flight.pop(run_id, None)

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self._lock:
            _, model, node, start = self.in_flight.pop(run_id, (0.0, self.fallback_model, "?", time.monotonic()))
        for generations in response.generations:
            for gen in generations:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None)
                if not meta:
                    with self._lock:
                        self._stop("a response carried no usage metadata: its cost is unknown, so the "
                                   "budget cannot be enforced")
                served = (getattr(message, "response_metadata", None) or {}).get("model_name") or model
                cost = call_cost(served, meta.get("input_tokens", 0), meta.get("output_tokens", 0))
                record = {"node": node, "model": served, "input": meta.get("input_tokens", 0),
                          "output": meta.get("output_tokens", 0), "cost_usd": round(cost, 6),
                          "seconds": round(time.monotonic() - start, 2), "at": time.time()}
                with self._lock:
                    self.spent += cost
                    row = self.usage.setdefault(served, {"calls": 0, "input": 0, "output": 0})
                    row["calls"] += 1
                    row["input"] += record["input"]
                    row["output"] += record["output"]
                    self.by_node[node] = self.by_node.get(node, 0) + 1
                    with self.calls_log.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record) + "\n")

    def total(self) -> float:
        with self._lock:
            return self.spent_before + self.spent


class FallbackCounter(logging.Handler):
    """Counts structured-output calls that fell back to free text, per agent."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.counts: dict[str, int] = {}
        self._guard = threading.Lock()

    def emit(self, record):
        message = record.getMessage()
        if "structured-output invocation failed" in message:
            agent = message.split(":", 1)[0]
            with self._guard:
                self.counts[agent] = self.counts.get(agent, 0) + 1


def code_commit() -> str:
    """The commit the run's code is at, marked when tracked files have local changes."""
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=False).stdout.strip()
    head = git("rev-parse", "HEAD") or "unknown"
    return head + ("+dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def versions() -> dict:
    out = {}
    for name in DEPENDENCIES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


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
            "decision_memory": config["use_decision_memory"], "workers": args.workers,
            "data_vendors": config["data_vendors"], "tool_vendors": config["tool_vendors"],
            "debate_rounds": config["max_debate_rounds"], "risk_rounds": config["max_risk_discuss_rounds"],
            "max_tool_rounds": config["max_tool_rounds"], "llm_max_retries": config.get("llm_max_retries"),
            "max_tokens": config.get("max_tokens"), "temperature": config.get("temperature"),
            "dependencies": versions(), "code": code_commit()}


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


def data_fingerprint(cache: Path) -> str:
    """A hash of every downloaded file the run read (CVM zips, price CSVs)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in cache.rglob("*") if p.is_file() and not p.name.endswith((".lock", ".tmp"))):
        digest.update(str(path.relative_to(cache)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


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
    p.add_argument("--workers", type=int, default=1,
                   help="tickers analyzed at once; mind the provider's rate limits")
    p.add_argument("--memory", choices=("off", "on"), default="off",
                   help="decision memory: off keeps each cell independent of the others")
    p.add_argument("--allow-dirty", action="store_true", help="run with uncommitted code (development only)")
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
                  checkpoint_enabled=False, holding_period_days=args.holding,
                  use_decision_memory=args.memory == "on")
    effort_key = {"anthropic": "anthropic_effort", "openai": "openai_reasoning_effort",
                  "chatgpt": "openai_reasoning_effort", "google": "google_thinking_level"}[args.provider]
    config[effort_key] = args.effort

    manifest = build_manifest(args, config)
    if manifest["code"].endswith("+dirty") and not args.allow_dirty:
        raise SystemExit("uncommitted changes to tracked files: commit them, or pass --allow-dirty for a "
                         "development run")
    check_manifest(out / "results" / "backtest" / args.run_id / "manifest.json", manifest)
    guard = SpendGuard(args.budget_usd, out / f"{args.run_id}_calls.jsonl", fallback_model=args.quick)
    fallbacks = FallbackCounter()
    logging.getLogger("tradingagents").addHandler(fallbacks)
    began = time.monotonic()

    def progress(done, total, ticker, date):
        print(f"[{done}/{total}] {ticker} {date} · {time.monotonic() - began:.0f}s elapsed"
              f" · spent so far ~US$ {guard.total():.3f}", flush=True)

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    dates = iter_grid(args.start, args.end, args.every)
    stopped = None
    # run_backtest builds its own graphs; hand them the guard for this run only.
    original = backtest.TradingAgentsGraph
    backtest.TradingAgentsGraph = functools.partial(TradingAgentsGraph, callbacks=[guard])
    try:
        result = run_backtest(tickers, dates, config, selected_analysts=args.analysts.split(","),
                              run_id=args.run_id, progress=progress, workers=args.workers)
    except OverBudget as exc:
        stopped = f"{exc}; re-run with the same --run-id and a higher --budget-usd to continue"
        print(stopped)
        result = None
    finally:
        backtest.TradingAgentsGraph = original
        logging.getLogger("tradingagents").removeHandler(fallbacks)

    log = out / "results" / "backtest" / args.run_id / "trading_memory.md"
    spent = guard.spent
    report = {"run_id": args.run_id, "manifest": manifest, "tickers": tickers, "dates": dates,
              "workers": args.workers, "wall_seconds": round(time.monotonic() - began),
              "budget_usd": args.budget_usd, "stopped": stopped, "usage": guard.usage,
              "calls_by_node": guard.by_node, "structured_fallbacks": fallbacks.counts,
              "data_fingerprint": data_fingerprint(out / "cache") if (out / "cache").exists() else None,
              "cost_usd_this_invocation": round(spent, 4),
              "cost_usd_run_id_total": round(guard.total(), 4)}
    if result is not None:
        report.update(cells_run=result.cells_run, skipped=result.skipped, failures=result.failures,
                      settlement_failures=result.settlement_failures)
        if result.cells_run:
            report["cost_usd_per_cell"] = round(spent / result.cells_run, 4)
    if log.is_file():
        summary = summarize(log)
        report["score"] = {"resolved": summary.resolved, "pending": summary.pending,
                           "by_rating": {k: vars(v) for k, v in summary.by_rating.items()}}
        print(summary.render())
        shutil.copy(log, out / f"{args.run_id}_trading_memory.md")
    history = out / f"{args.run_id}_invocations.jsonl"
    earlier = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()
               if line.strip()] if history.exists() else []
    if earlier and earlier[-1].get("data_fingerprint") not in (None, report["data_fingerprint"]):
        print("note: this invocation read different data files than the previous one of this run_id")
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps({k: report.get(k) for k in ("usage", "calls_by_node", "structured_fallbacks",
                                                  "cost_usd_this_invocation", "cost_usd_per_cell",
                                                  "wall_seconds", "cells_run", "skipped", "failures",
                                                  "stopped")}, indent=2))


if __name__ == "__main__":
    main()
