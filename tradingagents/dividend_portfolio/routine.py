"""One run of the routine: select, plan the orders, read the week's news, record the shadow, report.

In a rebalance month (June, December) the target is recomputed, recorded in the
shadow portfolio, and the orders may sell what left it. In any other week the
target is the one last recorded (frozen, as in the backtest), new money is placed
in it, and the only sales are those still pending from the last rebalance (kept
in ``vendas_pendentes.json`` and retried each month within the tax exemption).

The routine manages only shares of listed companies: funds (FII, ETF) and BDRs
are taxed differently and are reported as outside it.
"""

from __future__ import annotations

import copy
import json
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
    prices: dict = field(default_factory=dict)
    pending: set = field(default_factory=set)       # codes still to sell, from the last rebalance
    keep: set = field(default_factory=set)          # held or targeted codes with a price under review
    book: PortfolioContext | None = None            # the positions the routine manages
    outside: list = field(default_factory=list)     # held codes that are not listed companies' shares


PENDING_FILE = "vendas_pendentes.json"


def frozen_target(codes: list[str], snap: pd.DataFrame, companies: dict,
                  rules: selection.Rules = selection.DEFAULT_RULES) -> pd.DataFrame:
    """The recorded target ``codes`` as a target frame, with today's figures where the snapshot has them."""
    by_code = {r["code"]: r for r in snap.to_dict("records")} if len(snap) else {}
    company_of = {code: (cnpj, c) for cnpj, c in companies.items() for code in c.codes}
    rows = []
    for code in codes:
        if code in by_code:
            row = by_code[code]
        elif code in company_of:
            cnpj, c = company_of[code]
            row = {"cnpj": cnpj, "name": c.name, "sector": c.sector, "code": code}
        else:
            row = {"cnpj": None, "name": code, "sector": "", "code": code}
        if not selection.is_excluded(row.get("sector"), rules):
            rows.append(row)
    return pd.DataFrame(rows, columns=list(snap.columns) or ["cnpj", "name", "sector", "code"])


def same_company_codes(target: pd.DataFrame, companies: dict, held: set[str], priced: set[str]) -> pd.DataFrame:
    """``target`` with a held share class of a selected company in place of the selected code.

    Holding BBDC3 when the rule picks BBDC4 is holding Bradesco: swapping classes would
    only cost fees and, above the exemption, tax.
    """
    company_of = {code: cnpj for cnpj, c in companies.items() for code in c.codes}
    target = target.copy()
    for i, row in target.iterrows():
        if not isinstance(row["cnpj"], str):
            continue
        mine = sorted(c for c in held if company_of.get(c) == row["cnpj"] and c in priced)
        if mine and row["code"] not in mine:
            target.at[i, "code"] = mine[0]
    return target


def review(book: PortfolioContext | None = None, *, contribution: float = 0.0, as_of: date | None = None,
           rebalance: bool | None = None, use_llm: bool = True, budget_usd: float = 1.0,
           sold_earlier_this_month: float = 0.0, gain_earlier_this_month: float = 0.0,
           loss_carryforward: float = 0.0, fixed_income: float | None = None, target_share: float | None = None,
           band: float = 0.05, transition_months: int = 12, config: dict | None = None,
           rules: selection.Rules = selection.DEFAULT_RULES, budget=None) -> Result:
    """``target_share`` (0-1) with ``fixed_income`` (R$) splits the contribution between the two classes and
    adds the month's transfer from fixed income to the stocks' cash; without it all of it goes to stocks.
    ``budget``: a ``monitor.Budget`` shared with the caller; else the run has its own of ``budget_usd``."""
    config = copy.deepcopy(config or DEFAULT_CONFIG)
    out_dir = Path(config["results_dir"]) / "carteira"
    out_dir.mkdir(parents=True, exist_ok=True)
    book = book or PortfolioContext(cash=0.0)
    with run_config(config):
        companies = market.companies()
        listed = set().union(*(c.codes for c in companies.values()))
        outside = sorted({code_of(p.ticker) for p in book.positions} - listed)
        book = book.model_copy(update={"positions": [p for p in book.positions if code_of(p.ticker) in listed]})
        held_codes = {code_of(p.ticker) for p in book.positions}
        px = market.histories(listed | held_codes)
        bova, divo = market.index_series("BOVA11.SA"), market.index_series("DIVO11.SA")
        sessions = bova.index
        day = pd.Timestamp(as_of) if as_of else sessions[-1]
        day = sessions[sessions <= day][-1]
        rebalance = selection.is_rebalance_month(day, rules) if rebalance is None else rebalance

        # The rule's target at a rebalance (or on the first run); the recorded one in between.
        snap = selection.snapshot(companies, px, day, rules)
        shadow = Shadow(out_dir / "carteira_sombra.json")
        recorded = [e for e in shadow.events if e["date"] <= day.date().isoformat()]
        if rebalance or not recorded:
            chosen = selection.target(snap, rules)
            shadow.record(day.date().isoformat(), list(chosen["code"]))
        else:
            chosen = frozen_target(recorded[-1]["codes"], snap, companies, rules)
        target = same_company_codes(chosen, companies, held_codes, set(px))
        codes = list(target["code"])
        prices = {c: float(f["Close"].loc[:day].iloc[-1]) for c, f in px.items()
                  if c in set(codes) | held_codes and len(f.loc[:day])}
        keep = {c for c in set(codes) | held_codes if c in px and selection.is_suspect(px[c], day, rules)}
        pending_path = out_dir / PENDING_FILE
        pending = set(json.loads(pending_path.read_text(encoding="utf-8"))["codes"]) if pending_path.exists() else set()
        pending &= held_codes
        allocation = None
        if target_share is not None:
            stocks = (book.cash or 0.0) + sum(p.quantity * prices.get(code_of(p.ticker), 0.0) for p in book.positions)
            allocation = plan_allocation(stocks, fixed_income or 0.0, contribution, target_share, band=band,
                                         transition_months=transition_months)
            # The month's transfer joins the orders on the run that brings the month's contribution;
            # weekly runs without one only report where the allocation stands.
            if held_codes - set(prices):
                # A missing price is not zero: the stocks' share is unknown, so no transfer this time.
                allocation.monthly_transfer = min(allocation.monthly_transfer, 0.0)
            contribution = allocation.to_stocks_this_month if contribution > 0 else 0.0
        plan = plan_orders(book, codes, prices, contribution=contribution, sell=rebalance, pending=pending,
                           keep=keep, sold_earlier_this_month=sold_earlier_this_month,
                           gain_earlier_this_month=gain_earlier_this_month, loss_carryforward=loss_carryforward)
        # What is still to sell: every code with a sale this time (sold or deferred). One sold
        # and gone drops out on the next run; one not sold yet is retried.
        pending = {o.code for o in plan.orders + plan.deferred if o.side == "venda"}
        pending_path.write_text(json.dumps({"date": day.date().isoformat(), "codes": sorted(pending)}, indent=1),
                                encoding="utf-8")
        if allocation is not None and held_codes - set(prices):
            plan.notes.append(f"Sem preço para {', '.join(sorted(held_codes - set(prices)))}: a parte em ações é "
                              "desconhecida, então não há transferência da renda fixa nesta revisão.")
        if keep:
            plan.notes.append(f"Salto de preço sem explicação em {', '.join(sorted(keep))}: sem compra nem venda até "
                              "conferir o evento (desdobramento, cisão ou queda real).")
        if outside:
            plan.notes.append(f"Fora da rotina (não são ações de empresas listadas): {', '.join(outside)}.")
        performance = shadow.performance(px, {"bova11": bova, "divo11": divo})

        alerts, cut, spent = {}, {}, 0.0
        if use_llm:
            from tradingagents.dividend_portfolio.monitor import Assistant

            assistant = Assistant(config, budget_usd=budget_usd, budget=budget)
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
                  performance, prices, pending, keep, book, outside)
