"""Experiment 2: does the processing after the analysts add value over a single decision?

For every (ticker, date) cell the full TradingAgents graph runs once (decision
memory off), which yields the analysts' reports and everything the graph does
after them: the bull/bear debate, the research manager's plan, the trader's
proposal and the risk debate. Then one final decider -- the same model, effort,
prompt, schema and evaluation contract -- is asked twice for the probability that
the stock beats its benchmark over the horizon:

* ``full``:   the evidence package plus that deliberation;
* ``single``: the evidence package alone.

The evidence package is the analysts' reports, the deterministic market
snapshot (prices and relative strength, closes up to the date) and the
evaluation contract; it is frozen to a file and identified by its SHA-256, and
both arms read the same bytes. The graph's own Portfolio Manager decision is
kept as a third reference (``original``) at no extra cost. A subset of cells is
asked again (``--repeat``) to measure the decider's own variability.

Both arms are judged by the paired difference of their Brier scores on the same
outcomes (``experiment2_eval.py``). This measures the value of everything after
the analysts, not of the bull/bear debate alone.

Usage (needs ANTHROPIC_API_KEY, or TRADINGAGENTS_CLAUDE_KEY in cloud sessions):
    python experiments/br_baseline/experiment2.py --out experiments/br_baseline/results/experiment2 \\
        --run-id exp2_pilot --tickers PETR4.SA,VALE3.SA \\
        --dates 2026-07-06,2026-08-03,2026-08-31,2026-09-29 --workers 3 --budget-usd 15
"""

from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import json
import logging
import os
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_validation import (
    FallbackCounter,
    OverBudget,
    SpendGuard,
    build_manifest,
    check_manifest,
    data_fingerprint,
)
from pydantic import BaseModel, Field, field_validator

from tradingagents import backtest
from tradingagents.agents.schemas import PortfolioRating
from tradingagents.backtest import run_backtest
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients import create_tier_client
from tradingagents.memory.settlement import resolve_benchmark

ARMS = ("full", "single")


class DeciderOutput(BaseModel):
    """What the final decider returns, in both arms."""

    decisive_evidence: list[str] = Field(
        description="Identifiers of the evidence items that decided the estimate, e.g. ['E1', 'E4', 'D2'].")
    rationale: str = Field(description="Two to five sentences: the facts that drive the estimate, kept "
                                       "apart from assumptions.")
    gaps: str = Field(description="What the evidence does not show that would matter for the estimate.")
    probability_outperform: float = Field(
        description="Probability in percent (a number from 0 to 100, e.g. 42 for 42%) that the stock's total "
                    "return beats the benchmark's over the horizon. 50 means no edge either way.")
    rating: PortfolioRating = Field(description="Exactly one of Buy / Overweight / Hold / Underweight / Sell.")

    @field_validator("probability_outperform")
    @classmethod
    def _in_range(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("probability must be between 0 and 100 percent")
        return v


PROMPT = """You estimate whether a stock will beat its benchmark. Use only the evidence below; do not search.

{contract}

Question: what is the probability, in percent, that {ticker}'s total return beats {benchmark}'s over that horizon?

Separate facts, assumptions and gaps. Whether additional deliberation is present or absent is not evidence about direction. A Hold rating does not imply 50%. Cite the identifiers of the evidence that decided your estimate.

## Evidence package (analysis date {date})

{evidence}

## Additional deliberation

{deliberation}
"""


def _section(item: dict) -> str:
    return f"### [{item['id']}] {item['title']}\n\n{(item['text'] or '').strip() or '(empty)'}"


def build_package(state: dict, config: dict) -> tuple[dict, list[dict]]:
    """The frozen evidence package of a cell, and the deliberation the full arm also reads."""
    ticker, date = state["company_of_interest"], state["trade_date"]
    with run_config(config):
        snapshot = build_verified_market_snapshot(ticker, date)
    contract = TradingAgentsGraph.decision_horizon(types.SimpleNamespace(config=config), ticker)
    evidence = [{"id": "E1", "title": "Market analyst report", "text": state.get("market_report", "")},
                {"id": "E2", "title": "News analyst report (company filings, macro)",
                 "text": state.get("news_report", "")},
                {"id": "E3", "title": "Fundamentals analyst report", "text": state.get("fundamentals_report", "")}]
    if (state.get("sentiment_report") or "").strip():
        evidence.append({"id": "E5", "title": "Sentiment analyst report", "text": state["sentiment_report"]})
    evidence.append({"id": "E4", "title": "Verified market data snapshot (deterministic)", "text": snapshot})
    package = {"ticker": ticker, "date": date, "benchmark": resolve_benchmark(ticker, config),
               "contract": contract, "evidence": evidence}
    debate, risk = state.get("investment_debate_state", {}), state.get("risk_debate_state", {})
    deliberation = [{"id": "D1", "title": "Investment debate (bull and bear)", "text": debate.get("history", "")},
                    {"id": "D2", "title": "Research manager's plan", "text": state.get("investment_plan", "")},
                    {"id": "D3", "title": "Trader's proposal", "text": state.get("trader_investment_plan", "")},
                    {"id": "D4", "title": "Risk team debate", "text": risk.get("history", "")}]
    return package, deliberation


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def render_prompt(package: dict, deliberation: list[dict] | None) -> str:
    return PROMPT.format(contract=package["contract"], ticker=package["ticker"], benchmark=package["benchmark"],
                         date=package["date"], evidence="\n\n".join(_section(e) for e in package["evidence"]),
                         deliberation=("\n\n".join(_section(d) for d in deliberation) if deliberation
                                       else "None for this analysis."))


def make_decider(config: dict, model: str, effort: str, guard):
    """The final decider: one model, effort and schema for both arms; no free-text fallback."""
    tier = {**config, "deep_think_llm": model, "anthropic_effort": effort}
    return create_tier_client(tier, "deep", callbacks=[guard]).get_llm().with_structured_output(DeciderOutput)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--tickers", required=True)
    p.add_argument("--dates", required=True, help="comma-separated analysis dates, chosen before the run")
    p.add_argument("--holding", type=int, default=20)
    p.add_argument("--analysts", default="market,news,fundamentals")
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--quick", default="claude-haiku-5-5")
    p.add_argument("--deep", default="claude-sonnet-5-5")
    p.add_argument("--effort", default="medium")
    p.add_argument("--decider", default="claude-sonnet-5-5")
    p.add_argument("--decider-effort", default="medium")
    p.add_argument("--repeat", type=int, default=0, help="cells whose two decisions are asked a second time")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--budget-usd", type=float, default=15.0)
    p.add_argument("--allow-dirty", action="store_true")
    args = p.parse_args()
    args.memory = "off"

    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY") \
            and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]
    key = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}.get(args.provider)
    if key and not os.environ.get(key):
        raise SystemExit(f"{key} is not set (or TRADINGAGENTS_CLAUDE_KEY for Anthropic)")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"), llm_provider=args.provider,
                  quick_think_llm=args.quick, deep_think_llm=args.deep, checkpoint_enabled=False,
                  holding_period_days=args.holding, use_decision_memory=False, anthropic_effort=args.effort)
    manifest = {**build_manifest(args, config), "experiment": "experiment2", "decider": args.decider,
                "decider_effort": args.decider_effort, "prompt_sha": sha(PROMPT),
                "schema_sha": sha(DeciderOutput.model_json_schema()), "repeat": args.repeat}
    if manifest["code"].endswith("+dirty") and not args.allow_dirty:
        raise SystemExit("uncommitted changes to tracked files: commit them, or pass --allow-dirty")
    run_dir = out / "results" / "backtest" / args.run_id
    check_manifest(run_dir / "manifest.json", manifest)

    guard = SpendGuard(args.budget_usd, out / f"{args.run_id}_calls.jsonl", fallback_model=args.quick)
    fallbacks = FallbackCounter()
    logging.getLogger("tradingagents").addHandler(fallbacks)
    began = time.monotonic()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    stopped, result = None, None

    # Stage 1: the graph, once per cell.
    original = backtest.TradingAgentsGraph
    backtest.TradingAgentsGraph = functools.partial(TradingAgentsGraph, callbacks=[guard])
    try:
        result = run_backtest(tickers, dates, config, selected_analysts=args.analysts.split(","),
                              run_id=args.run_id, workers=args.workers,
                              progress=lambda i, n, t, d: print(f"[graph {i}/{n}] {t} {d} · "
                                                                f"{time.monotonic() - began:.0f}s · "
                                                                f"US$ {guard.total():.3f}", flush=True))
    except OverBudget as exc:
        stopped = str(exc)
    finally:
        backtest.TradingAgentsGraph = original

    # Stage 2: the two decisions per cell, from the frozen package.
    decisions_path = out / f"{args.run_id}_decisions.jsonl"
    packages = out / f"{args.run_id}_packages"
    packages.mkdir(exist_ok=True)
    done = set()
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["status"] == "ok":
                done.add((row["ticker"], row["date"], row["arm"], row["rep"]))
    cells = [(t, d) for d in dates for t in tickers]
    repeated = set(cells[::max(1, len(cells) // args.repeat)][:args.repeat]) if args.repeat else set()
    lock = threading.Lock()
    decider = make_decider(config, args.decider, args.decider_effort, guard) if not stopped else None

    def decide(cell):
        ticker, date = cell
        state_path = run_dir / ticker / "TradingAgentsStrategy_logs" / f"full_states_log_{date}.json"
        if not state_path.exists():
            return
        state = json.loads(state_path.read_text(encoding="utf-8"))
        package, deliberation = build_package(state, config)
        package_sha = sha(package)
        (packages / f"{ticker}_{date}.json").write_text(
            json.dumps({"sha256": package_sha, "package": package, "deliberation": deliberation,
                        "deliberation_sha256": sha(deliberation)}, ensure_ascii=False, indent=1), encoding="utf-8")
        for rep in (1, 2) if cell in repeated else (1,):
            for arm in ARMS:
                if (ticker, date, arm, rep) in done:
                    continue
                prompt = render_prompt(package, deliberation if arm == "full" else None)
                row = {"ticker": ticker, "date": date, "arm": arm, "rep": rep, "package_sha": package_sha,
                       "deliberation_sha": sha(deliberation) if arm == "full" else None}
                try:
                    answer = decider.invoke(prompt, config={"metadata": {"langgraph_node": f"decider:{arm}"}})
                    row.update(status="ok", **answer.model_dump(mode="json"))
                except OverBudget:
                    raise
                except Exception as exc:   # a refusal, a truncation, an invalid probability: failed measurement
                    row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
                with lock, decisions_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"[decide] {ticker} {date} {arm} r{rep}: {row.get('probability_outperform', row['status'])}"
                      f" · US$ {guard.total():.3f}", flush=True)

    if decider is not None:
        try:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                for future in [pool.submit(decide, c) for c in cells]:
                    future.result()
        except OverBudget as exc:
            stopped = str(exc)
    logging.getLogger("tradingagents").removeHandler(fallbacks)

    report = {"run_id": args.run_id, "manifest": manifest, "tickers": tickers, "dates": dates,
              "wall_seconds": round(time.monotonic() - began), "stopped": stopped, "usage": guard.usage,
              "calls_by_node": guard.by_node, "structured_fallbacks": fallbacks.counts,
              "cost_usd_this_invocation": round(guard.spent, 4), "cost_usd_run_id_total": round(guard.total(), 4),
              "data_fingerprint": data_fingerprint(out / "cache") if (out / "cache").exists() else None}
    if result is not None:
        report.update(cells_run=result.cells_run, skipped=result.skipped, failures=result.failures,
                      settlement_failures=result.settlement_failures)
    with (out / f"{args.run_id}_invocations.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps({k: report.get(k) for k in ("calls_by_node", "structured_fallbacks", "cost_usd_this_invocation",
                                                  "wall_seconds", "cells_run", "failures", "stopped")}, indent=2))


if __name__ == "__main__":
    main()
