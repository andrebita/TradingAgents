"""Run the native ``tradingagents backtest`` pipeline on B3 tickers with no LLM.

Every node of the real graph runs, every analyst calls each of its bound tools
against the real vendors with the run's point-in-time guards, the decision is
written to a memory log, settled against the Ibovespa by the framework's own
``settle_pending`` and scored by its own ``summarize``. Only the language model
is replaced: a scripted model that calls each tool once, records what came back,
and answers with a fixed rating.

What this measures, and what it does not:

* It validates the infrastructure (data access, graph, settlement, scoring) for
  ``.SA`` tickers, and records which data would reach the agents on each date.
* With ``--rating Buy`` the scored result is the "always long" baseline in the
  framework's own metric (hit rate = share of cells beating the Ibovespa over the
  holding window), which an LLM run has to beat to show any skill.
* It says nothing about the quality of LLM decisions.

Usage:
    python experiments/br_baseline/native_dryrun.py --out DIR \
        --tickers ITUB4.SA,PSSA3.SA,SBSP3.SA,BBDC4.SA,B3SA3.SA \
        --start 2026-01-05 --end 2026-10-23 --every 7 --rating Buy
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph

# Dates after any trade date: the tools clamp them back to the run's own date
# (``as_of`` / ``as_of_window``), so the scripted model need not know the date.
FUTURE_ARGS = {"curr_date": "2099-12-31", "start_date": "2099-12-01", "end_date": "2099-12-31",
               "indicator": "rsi", "look_back_days": 30, "freq": "quarterly",
               "topic": "Brazil Selic interest rate", "indicators": ["rsi", "close_50_sma", "macd"]}

_lock = threading.Lock()


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, logs the answers, then states a fixed rating."""

    rating: str = "Buy"
    tools: tuple = ()
    tool_log: str = ""
    calls: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError  # the agents fall back to free text

    def _text(self) -> str:
        action = {"Buy": "BUY", "Overweight": "BUY", "Hold": "HOLD",
                  "Underweight": "SELL", "Sell": "SELL"}[self.rating]
        return (f"Scripted placeholder (no LLM).\n\n**Rating**: {self.rating}\n\n"
                f"FINAL TRANSACTION PROPOSAL: **{action}**")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(1)
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        if self.tools and not tool_results:
            calls = []
            for i, t in enumerate(self.tools):
                props = t.tool_call_schema.model_json_schema().get("properties", {})
                calls.append({"name": t.name, "id": f"call_{i}",
                              "args": {k: v for k, v in FUTURE_ARGS.items() if k in props}})
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        if tool_results and self.tool_log:
            with _lock, open(self.tool_log, "a", encoding="utf-8") as fh:
                for m in tool_results:
                    text = str(m.content)
                    fh.write(json.dumps({"tool": m.name, "chars": len(text),
                                         "head": text.strip()[:300]}, ensure_ascii=False) + "\n")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._text()))])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--tickers", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--every", type=int, default=7)
    p.add_argument("--rating", default="Buy", choices=["Buy", "Overweight", "Hold", "Underweight", "Sell"])
    p.add_argument("--analysts", default="market,social,news,fundamentals")
    p.add_argument("--run-id", default=None)
    args = p.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    model = ScriptedModel(rating=args.rating, tool_log=str(out / "tool_outputs.jsonl"))
    trading_graph.create_tier_client = lambda config, tier, **k: _Client(model)

    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"),
                  checkpoint_enabled=False)
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    dates = iter_grid(args.start, args.end, args.every)

    def progress(done, total, ticker, date):
        print(f"[{done}/{total}] {ticker} {date}", flush=True)

    result = run_backtest(tickers, dates, config, selected_analysts=args.analysts.split(","),
                          run_id=args.run_id, progress=progress)
    summary = summarize(result)
    print(summary.render())
    (out / "summary.json").write_text(json.dumps({
        "run_id": result.run_id, "log_path": str(result.log_path),
        "tickers": tickers, "dates": dates, "rating": args.rating, "analysts": args.analysts,
        "cells_run": result.cells_run, "skipped": result.skipped,
        "failures": result.failures, "settlement_failures": result.settlement_failures,
        "resolved": summary.resolved, "pending": summary.pending,
        "by_rating": {k: vars(v) for k, v in summary.by_rating.items()},
        "llm_calls": len(model.calls),
    }, indent=2, ensure_ascii=False))
    (out / "summary.txt").write_text(summary.render() + "\n")


if __name__ == "__main__":
    main()
