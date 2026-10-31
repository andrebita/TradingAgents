"""One run of the routine: select, plan the orders, read the week's news, record the shadow, report.

In a rebalance month (June, December) the target is recomputed, recorded in the
shadow portfolio, and the orders may sell what left it. In any other week only
new money is placed, in the current target, and nothing is sold.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.config import run_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dividend_portfolio import market, selection
from tradingagents.dividend_portfolio.orders import code_of, plan_orders
from tradingagents.dividend_portfolio.report import render
from tradingagents.dividend_portfolio.shadow import Shadow
from tradingagents.portfolio import PortfolioContext


@dataclass
class Result:
    report: str
    report_path: Path
    rebalance: bool
    target: list[str]
    spent_usd: float


def review(book: PortfolioContext | None = None, *, contribution: float = 0.0, as_of: date | None = None,
           rebalance: bool | None = None, use_llm: bool = True, budget_usd: float = 1.0,
           sold_earlier_this_month: float = 0.0, gain_earlier_this_month: float = 0.0,
           loss_carryforward: float = 0.0, config: dict | None = None,
           rules: selection.Rules = selection.DEFAULT_RULES) -> Result:
    config = copy.deepcopy(config or DEFAULT_CONFIG)
    out_dir = Path(config["results_dir"]) / "carteira"
    out_dir.mkdir(parents=True, exist_ok=True)
    book = book or PortfolioContext(cash=0.0)
    with run_config(config):
        companies = market.companies()
        held_codes = {code_of(p.ticker) for p in book.positions}
        px = market.histories(set().union(*(c.codes for c in companies.values())) | held_codes)
        bova, divo = market.index_series("BOVA11.SA"), market.index_series("DIVO11.SA")
        sessions = bova.index
        day = pd.Timestamp(as_of) if as_of else sessions[-1]
        day = sessions[sessions <= day][-1]
        rebalance = selection.is_rebalance_month(day, rules) if rebalance is None else rebalance

        snap = selection.snapshot(companies, px, day, rules)
        target = selection.target(snap, rules)
        codes = list(target["code"])
        prices = {c: float(f["Close"].loc[:day].iloc[-1]) for c, f in px.items()
                  if c in set(codes) | held_codes and len(f.loc[:day])}
        plan = plan_orders(book, codes, prices, contribution=contribution, sell=rebalance,
                           sold_earlier_this_month=sold_earlier_this_month,
                           gain_earlier_this_month=gain_earlier_this_month, loss_carryforward=loss_carryforward)

        shadow = Shadow(out_dir / "carteira_sombra.json")
        if rebalance or not shadow.events:
            shadow.record(day.date().isoformat(), codes)
        performance = shadow.performance(px, {"bova11": bova, "divo11": divo})

        alerts, cut, spent = {}, {}, 0.0
        if use_llm:
            from tradingagents.dividend_portfolio.monitor import Assistant

            assistant = Assistant(config, budget_usd=budget_usd)
            for code in sorted(held_codes):
                alerts[code] = assistant.news(code, day.date())
            if rebalance:
                for _, row in target.iterrows():
                    cut[row["code"]] = assistant.cut_risk(row["cnpj"], row["sector"], px[row["code"]], day.date())
            spent = assistant.spent

    holdings = []
    for p in book.positions:
        code = code_of(p.ticker)
        if code in prices:
            holdings.append({"code": code, "quantity": p.quantity, "price": prices[code],
                             "value": p.quantity * prices[code], "in_target": code in codes})
    text = render(day.date().isoformat(), rebalance, target, holdings, plan, alerts, cut, performance, spent, use_llm)
    path = out_dir / f"relatorio_{day.date().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return Result(text, path, rebalance, codes, spent)
