"""A committee meeting: analysts, then the director, then the policy's check, the record and the letter."""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.committee import analysts, director
from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import Wealth
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import b3_market
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dividend_portfolio import routine
from tradingagents.dividend_portfolio.monitor import CutRisk, NewsAlert
from tradingagents.dividend_portfolio.orders import plan_orders
from tradingagents.dividend_portfolio.report import brl, pct
from tradingagents.dividend_portfolio.shadow import Shadow


@dataclass
class Meeting:
    letter: str
    path: Path
    spent_usd: float
    checked: director.Checked
    reports: list


def equity_facts(eq: routine.Result) -> dict:
    def alert(a):
        if isinstance(a, NewsAlert):
            return {"level": a.level, "summary": a.summary, "dividend_news": a.dividend_news}
        return a or "nada entregue à CVM nem publicado na imprensa acompanhada"

    target = []
    for _, r in eq.target_frame.iterrows():
        risk = eq.cut.get(r["code"])
        target.append({"code": r["code"], "name": r["name"], "sector": r["sector"], "dividend_yield_24m":
                       round(float(r["dy24"]), 4),
                       "dividend_cut_probability": risk.probability_cut if isinstance(risk, CutRisk) else None})
    return {"rebalance_month": eq.rebalance, "rule_target": target, "holdings": eq.holdings,
            "rule_orders": orders_json(eq.plan), "deferred_sales": [f"{o.code} {o.quantity}" for o in eq.plan.deferred],
            "weekly_alerts": {k: alert(v) for k, v in eq.alerts.items()}, "shadow_performance": eq.performance}


def orders_json(plan) -> list[dict]:
    return [{"side": o.side, "code": o.code, "quantity": o.quantity, "value": round(o.value, 2), "reason": o.reason}
            for o in plan.orders]


def rule_decision(alloc: dict) -> director.DirectorDecision:
    return director.DirectorDecision(
        letter="Reunião sem LLM: as decisões abaixo são as da regra, sem leitura do diretor.",
        contribution_to_stocks=alloc["rule_contribution_to_stocks"],
        monthly_transfer_to_stocks=alloc["rule_monthly_transfer"])


def hold(policy: InvestmentPolicy, wealth: Wealth, *, contribution: float | None = None, as_of: date | None = None,
         use_llm: bool = True, config: dict | None = None) -> Meeting:
    config = copy.deepcopy(config or DEFAULT_CONFIG)
    out = Path(config["results_dir"]) / "comite"
    out.mkdir(parents=True, exist_ok=True)
    contribution = policy.monthly_contribution if contribution is None else contribution
    today = as_of or date.today()
    book = wealth.stock_book()
    with run_config(config):
        companies = b3_market.companies()
        sectors = {code: c.sector for c in companies.values() for code in c.codes}
        held = {p.ticker.upper() for p in book.positions}
        px = b3_market.histories(held)
        values: dict[str, float] = {}
        for p in book.positions:
            close = px[p.ticker]["Close"].loc[:pd.Timestamp(today)] if p.ticker in px else pd.Series(dtype=float)
            values[p.ticker] = values.get(p.ticker, 0.0) + (p.quantity * float(close.iloc[-1]) if len(close) else 0.0)
        alloc = analysts.allocation(policy, wealth, values, contribution)
        models = analysts.Models(config, policy.budget_usd) if use_llm else None

        # The equities analyst runs the rule with the rule's money; the other four work meanwhile.
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(analysts.fixed_income, policy, wealth, today, models),
                    pool.submit(analysts.risk, policy, wealth, alloc, values, sectors, models),
                    pool.submit(analysts.macro, policy, today, models)]
            eq = routine.review(book, contribution=alloc["rule_contribution_to_stocks"] + alloc["rule_monthly_transfer"],
                                as_of=today, use_llm=use_llm, budget_usd=policy.budget_usd / 3,
                                config={**config, "results_dir": str(out)})
            tax = analysts.tax(policy, wealth, eq.plan, models)
            fixed, risk, macro = (j.result() for j in jobs)
        equities = analysts.Report(analysts.ROLES["equities"], equity_facts(eq))
        reports = [fixed, equities, risk, tax, macro]

        if models is not None and models.spent + eq.spent_usd < policy.budget_usd:
            decision = director.ask(models.llm(policy.models.director, effort="high"), policy, alloc, reports,
                                    orders_json(eq.plan))
        else:
            decision = rule_decision(alloc)
        new_buys = {o.code for o in eq.plan.orders if o.side == "compra" and o.code not in held}
        checked = director.check(decision, alloc, fixed.facts["liquid_beyond_reserve"], new_buys,
                                 {x.name: x.value for x in wealth.fixed_income})

        # The stock orders with the director's money and deferrals.
        skip = {d.code for d in checked.deferred}
        targets = [c for c in eq.target if c not in skip]
        prices_px = b3_market.histories(set(eq.target) | held)
        prices = {c: float(f["Close"].loc[:pd.Timestamp(today)].iloc[-1]) for c, f in prices_px.items()
                  if len(f.loc[:pd.Timestamp(today)])}
        final = plan_orders(book, targets, prices, contribution=checked.contribution_to_stocks + checked.transfer,
                            sell=eq.rebalance)
        if eq.rebalance or not Shadow(out / "sombra_diretor.json").events:
            Shadow(out / "sombra_diretor.json").record(eq.day.isoformat(), targets)

    spent = (models.spent if models else 0.0) + eq.spent_usd
    letter = render(eq.day, policy, alloc, checked, final, reports, spent, models)
    path = out / f"carta_{eq.day.isoformat()}.md"
    path.write_text(letter, encoding="utf-8")
    with (out / "decisoes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": eq.day.isoformat(), "allocation": alloc,
                             "rule": {"contribution_to_stocks": alloc["rule_contribution_to_stocks"],
                                      "transfer": alloc["rule_monthly_transfer"], "targets": eq.target},
                             "director": {"contribution_to_stocks": checked.contribution_to_stocks,
                                          "transfer": checked.transfer, "deferred": [d.code for d in checked.deferred],
                                          "fixed_income_actions": [a.model_dump() for a in checked.fixed_income_actions],
                                          "escalations": checked.decision.escalations},
                             "adjustments": checked.adjustments, "cost_usd": round(spent, 4)},
                            ensure_ascii=False) + "\n")
    return Meeting(letter, path, spent, checked, reports)


def render(day: date, policy: InvestmentPolicy, alloc: dict, checked: director.Checked, final, reports: list,
           spent: float, models) -> str:
    d = checked.decision
    out = [f"# Comitê de Investimentos: reunião de {day.isoformat()}", "",
           "> Recomendações para sua decisão, dentro da sua política de investimentos. Não é garantia de resultado.",
           "", f"## Carta do Diretor Financeiro ({policy.models.director})", "", d.letter.strip(), "",
           "## Decisões do mês, conferidas pela política", "",
           "| | Hoje | Alvo da política |", "|---|---|---|",
           f"| Reserva de emergência | {brl(alloc['reserve_target'])} reservados | {policy.reserve_months} meses |",
           f"| Ações (do patrimônio investível) | {brl(alloc['stocks'])} ({pct(alloc['stock_share_of_investable']).lstrip('+')}) "
           f"| {pct(policy.stock_target, 0).lstrip('+')} |",
           f"| Fundos listados (FII, Fiagro, Infra) | {brl(alloc['listed_funds'])} "
           f"({pct(alloc['listed_funds_share_of_investable']).lstrip('+')}) | até {pct(policy.listed_funds_max, 0).lstrip('+')} |",
           "", f"- Aporte do mês: {brl(alloc['contribution'])}, dos quais **{brl(checked.contribution_to_stocks)} "
           "para ações**.",
           f"- Transferência da renda fixa para ações: **{brl(checked.transfer)}** "
           f"(regra: {brl(alloc['rule_monthly_transfer'])} por mês em {alloc['transition_months']} meses)."]
    if alloc["listed_funds_above_ceiling"]:
        out.append("- Fundos listados acima do teto: não recebem dinheiro novo até voltar à faixa (nada é vendido).")
    out += ["", "### Ordens de ações", ""]
    if final.orders:
        out += ["| Ordem | Ação | Quantidade | Valor | Motivo |", "|---|---|---|---|---|"]
        out += [f"| {o.side} | {o.code} | {o.quantity} | {brl(o.value)} | {o.reason} |" for o in final.orders]
    else:
        out.append("Nenhuma.")
    if final.deferred:
        out.append("\nVendas adiadas para manter a isenção de IR: " + ", ".join(f"{o.code} {o.quantity}"
                                                                           for o in final.deferred))
    if checked.deferred:
        out.append("\nCompras adiadas pelo diretor: " + "; ".join(f"{x.code} ({x.reason})" for x in checked.deferred))
    if checked.fixed_income_actions:
        out += ["", "### Renda fixa", "", "| Ação | Título | Valor | Motivo |", "|---|---|---|---|"]
        out += [f"| {a.action} | {a.instrument} | {brl(a.amount)} | {a.reason} |" for a in checked.fixed_income_actions]
    if d.escalations:
        out += ["", "### Para você decidir ou confirmar", ""] + [f"- {e}" for e in d.escalations]
    if checked.adjustments:
        out += ["", "### Ajustes feitos pela checagem da política", ""] + [f"- {a}" for a in checked.adjustments]

    out += ["", "## Relatórios dos analistas", ""]
    for r in reports:
        model = {"Analista de Renda Fixa": policy.models.fixed_income, "Analista de Risco": policy.models.risk,
                 "Analista Tributário": policy.models.tax, "Analista Macro": policy.models.macro}.get(r.role)
        out += [f"### {r.role}" + (f" ({model})" if model and r.view else ""), ""]
        if r.view:
            out += [r.view.summary.strip(), ""]
            out += [f"- {x}" for x in r.view.recommendations] + [f"- Risco: {x}" for x in r.view.risks]
        elif r.role == analysts.ROLES["equities"]:
            f = r.facts
            out.append("Seleção da regra: " + ", ".join(
                t["code"] + (f" (risco de corte {t['dividend_cut_probability']:.0f}%)"
                             if t["dividend_cut_probability"] is not None else "") for t in f["rule_target"]) + ".")
            for code, a in f["weekly_alerts"].items():
                out.append(f"- **{code}**: " + (f"{a['level']}: {a['summary']}" if isinstance(a, dict) else a))
        if r.error:
            out.append(f"- Leitura do modelo indisponível ({r.error}); os fatos seguiram para o diretor.")
        out += [f"- Aviso: {w}" for w in r.warnings] + [""]
    if models is not None:
        detail = "; ".join(f"{m}: US$ {meter.spent:.3f}" for m, meter in models.meters.items())
        out.append(f"_Custo dos modelos nesta reunião: US$ {spent:.3f} ({detail}; alertas e risco de corte inclusos)._")
    return "\n".join(out) + "\n"
