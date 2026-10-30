"""Measure the tokens one backtest cell sends and receives, without an LLM.

Runs the real graph on real data for one (ticker, date) per call, with a scripted
model per tier (quick / deep) that calls every bound tool once and answers with a
reply padded to ``--reply-tokens``. Every prompt is counted with ``o200k_base``
(the OpenAI GPT-4o+ tokenizer). Later prompts carry the earlier replies, so the
padding makes the growth of the context realistic.

Not measured: reasoning tokens (billed as output by reasoning models) and extra
tool rounds a real model may take; the report applies them as factors.

Usage:
    python experiments/br_baseline/estimate_tokens.py --out DIR --ticker ITUB4.SA \
        --date 2026-03-02 --analysts market --reply-tokens 800
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import tiktoken
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from experiments.br_baseline.native_dryrun import FUTURE_ARGS, ScriptedModel, _Client
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph

ENC = tiktoken.get_encoding("o200k_base")
USAGE: list[dict] = []   # module-level: pydantic copies list fields on validation
FILLER = ("The analysis weighs price trend, momentum, volatility and relative strength "
          "against the Ibovespa, and the risks to the thesis over the holding window. ")


def _tokens(messages) -> int:
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else json.dumps(m.content)
        total += len(ENC.encode(content)) + 4   # per-message framing
        for call in getattr(m, "tool_calls", None) or []:
            total += len(ENC.encode(json.dumps(call.get("args", {})))) + 10
    return total


class CountingModel(ScriptedModel):
    tier: str = "quick"
    reply_tokens: int = 800

    def _reply(self) -> str:
        body = FILLER * (self.reply_tokens // len(ENC.encode(FILLER)) + 1)
        return body + "\n\n" + self._text()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        if self.tools and not tool_results:
            calls = []
            for i, t in enumerate(self.tools):
                props = t.tool_call_schema.model_json_schema().get("properties", {})
                calls.append({"name": t.name, "id": f"call_{i}",
                              "args": {k: v for k, v in FUTURE_ARGS.items() if k in props}})
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content=self._reply())
        tool_defs = sum(len(ENC.encode(json.dumps(t.tool_call_schema.model_json_schema())))
                        for t in self.tools)
        USAGE.append({"tier": self.tier, "input": _tokens(messages) + tool_defs,
                           "output": _tokens([message])})
        return ChatResult(generations=[ChatGeneration(message=message)])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--ticker", default="ITUB4.SA")
    p.add_argument("--date", default="2026-03-02")
    p.add_argument("--analysts", default="market")
    p.add_argument("--reply-tokens", type=int, default=800)
    args = p.parse_args()

    out = Path(args.out).resolve()
    models = {tier: CountingModel(tier=tier, reply_tokens=args.reply_tokens)
              for tier in ("quick", "deep")}
    trading_graph.create_tier_client = lambda config, tier, **k: _Client(models[tier])
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"),
                  memory_log_path=str(out / "log.md"), checkpoint_enabled=False)
    graph = trading_graph.TradingAgentsGraph(args.analysts.split(","), config=config)
    graph.propagate(args.ticker, args.date)

    totals = {}
    for tier in ("quick", "deep"):
        rows = [u for u in USAGE if u["tier"] == tier]
        totals[tier] = {"calls": len(rows), "input": sum(u["input"] for u in rows),
                        "output": sum(u["output"] for u in rows)}
    report = {"ticker": args.ticker, "date": args.date, "analysts": args.analysts,
              "reply_tokens": args.reply_tokens, "per_cell": totals, "calls": USAGE}
    out.mkdir(parents=True, exist_ok=True)
    (out / f"tokens_{args.analysts.replace(',', '+')}_{args.reply_tokens}.json").write_text(
        json.dumps(report, indent=2))
    print(json.dumps({"analysts": args.analysts, "reply_tokens": args.reply_tokens, **totals}))


if __name__ == "__main__":
    main()
