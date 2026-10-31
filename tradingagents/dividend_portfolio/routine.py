"""One run of the routine: select, plan the orders, read the week's news, record the shadow, report.

In a rebalance month (June, December) the target is recomputed, recorded in the
shadow portfolio, and the orders may sell what left it. In any other week only
new money is placed, in the current target, and nothing is sold.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.config import run_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dividend_portfolio import market, selection
from tradingagents.dividend_portfolio.allocation import plan_allocation
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
    day: date | None = None
    target_frame: pd.DataFrame | None = None
    plan: object = None
    alerts: dict = field(default_factory=dict)
    cut: dict = field(default_factory=dict)
    holdings: list = field(default_factory=list)
    allocation: object = None
    performance: dict | None = None


def same_company_codes(target: pd.DataFrame, companies: dict, held: set[str], priced: set[str]) -> pd.DataFrame:
    """``target`` with a held share class of a selected company in place of the selected code.

    Holding BBDC3 when the rule picks BBDC4 is holding Bradesco: swapping classes would
    only cost fees and, above the exemption, tax.
    """
    company_of = {code: cnpj for cnpj, c in companies.items() for code in c.codes}
    target = target.copy()
    for i, row in target.iterrows():
        mine = sorted(c for c in held if company_of.get(c) == row["cnpj"] and c in priced)
        if mine and row["code"] not in mine:
            target.at[i, "code"] = mine[0]
    return target


def review(book: PortfolioContext | None = None, *, contribution: float = 0.0, as_of: date | None = None,
           rebalance: bool | None = None, use_llm: bool = True, budget_usd: float = 1.0,
           sold_earlier_this_month: float = 0.0, gain_earlier_this_month: float = 0.0,
           loss_carryforward: float = 0.0, fixed_income: float | None = None, target_share: float | None = None,
           band: float = 0.05, transition_months: int = 12, config: dict | None = None,
           rules: selection.Rules = selection.DEFAULT_RULES) -> Result:
    """``target_share`` (0-1) with ``fixed_income`` (R$) splits the contribution between the two classes and
    adds the month's transfer from fixed income to the stocks' cash; without it all of it goes to stocks."""
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
        target = same_company_codes(selection.target(snap, rules), companies, held_codes, set(px))
        codes = list(target["code"])
        prices = {c: float(f["Close"].loc[:day].iloc[-1]) for c, f in px.items()
                  if c in set(codes) | held_codes and len(f.loc[:day])}
        allocation = None
        if target_share is not None:
            stocks = (book.cash or 0.0) + sum(p.quantity * prices.get(code_of(p.ticker), 0.0) for p in book.positions)
            allocation = plan_allocation(stocks, fixed_income or 0.0, contribution, target_share, band=band,
                                         transition_months=transition_months)
            # The month's transfer joins the orders on the run that brings the month's contribution;
            # weekly runs without one only report where the allocation stands.
            contribution = allocation.to_stocks_this_month if contribution > 0 else 0.0
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
    text = render(day.date().isoformat(), rebalance, target, holdings, plan, alerts, cut, performance, spent, use_llm,
                  allocation)
    path = out_dir / f"relatorio_{day.date().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return Result(text, path, rebalance, codes, spent, day.date(), target, plan, alerts, cut, holdings, allocation,
                  performance)
