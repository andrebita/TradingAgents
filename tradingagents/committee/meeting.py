"""A committee meeting: analysts, then the director, then the policy's check, the record and the letter."""

from __future__ import annotations

import contextvars
import copy
import json
import logging
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
from tradingagents.dividend_portfolio import routine, selection
from tradingagents.dividend_portfolio.monitor import Budget, CutRisk, NewsAlert
from tradingagents.dividend_portfolio.orders import plan_orders
from tradingagents.dividend_portfolio.report import brl, pct
from tradingagents.dividend_portfolio.shadow import Shadow

logger = logging.getLogger(__name__)


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


def rule_decision(alloc: dict, failure: str | None = None) -> director.DirectorDecision:
    """The rule's figures, as the decision of a meeting without the director (by choice or by failure)."""
    if failure:
        letter = (f"O diretor não respondeu nesta reunião ({failure}). As decisões abaixo são as da regra, "
                  "conferidas pela política, sem a leitura dele.")
    else:
        letter = "Reunião sem LLM: as decisões abaixo são as da regra, sem leitura do diretor."
    return director.DirectorDecision(
        letter=letter, contribution_to_stocks=alloc["rule_contribution_to_stocks"],
        monthly_transfer_to_stocks=alloc["rule_monthly_transfer"],
        escalations=[f"Rodar a reunião de novo: o diretor falhou ({failure})."] if failure else [])


def hold(policy: InvestmentPolicy, wealth: Wealth, *, contribution: float | None = None, as_of: date | None = None,
         use_llm: bool = True, config: dict | None = None) -> Meeting:
    config = copy.deepcopy(config or DEFAULT_CONFIG)
    out = Path(config["results_dir"]) / "comite"
    out.mkdir(parents=True, exist_ok=True)
    contribution = policy.monthly_contribution if contribution is None else contribution
    today = as_of or date.today()
    book = wealth.stock_book()
    sold, gain, carried = wealth.tax.for_month(today)
    rules = selection.Rules(excluded_sectors=tuple(policy.excluded_sectors))
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
        models = analysts.Models(config, Budget(policy.budget_usd)) if use_llm else None
        # The routine's alerts get at most a third of the meeting's budget, so the director's call is left room.
        equity_budget = Budget(policy.budget_usd / 3, parent=models.budget) if models else None

        # The equities analyst runs the rule with the rule's money; the other four work meanwhile, each in a
        # copy of this context (the run's config lives in a context variable that threads do not inherit).
        with ThreadPoolExecutor(max_workers=4) as pool:
            def submit(fn, *args):
                return pool.submit(contextvars.copy_context().run, fn, *args)

            jobs = [submit(analysts.fixed_income, policy, wealth, today, models),
                    submit(analysts.risk, policy, wealth, alloc, values, sectors, models),
                    submit(analysts.macro, policy, today, models)]
            eq = routine.review(book, contribution=alloc["rule_contribution_to_stocks"] + alloc["rule_monthly_transfer"],
                                as_of=today, use_llm=use_llm, budget_usd=policy.budget_usd / 3, budget=equity_budget,
                                sold_earlier_this_month=sold, gain_earlier_this_month=gain, loss_carryforward=carried,
                                config={**config, "results_dir": str(out)}, rules=rules)
            tax = analysts.tax(policy, wealth, eq.plan, models)
            fixed, risk, macro = (j.result() for j in jobs)
        equities = analysts.Report(analysts.ROLES["equities"], equity_facts(eq))
        reports = [fixed, equities, risk, tax, macro]

        decision = rule_decision(alloc)
        if models is not None:
            try:
                decision = director.ask(models.llm(policy.models.director, effort="high",
                                                   max_output=analysts.DIRECTOR_MAX_OUTPUT),
                                        policy, alloc, reports, orders_json(eq.plan), budget=models.budget,
                                        max_output=analysts.DIRECTOR_MAX_OUTPUT)
            except Exception as exc:  # a refusal, an invalid answer or the budget: the rule decides
                logger.warning("The director failed: %s", exc)
                decision = rule_decision(alloc, f"{type(exc).__name__}: {exc}"[:200])
        new_buys = {o.code for o in eq.plan.orders if o.side == "compra" and o.code not in held}
        checked = director.check(decision, alloc, wealth, policy, new_buys, today)

        # The stock orders with the director's money and deferrals.
        skip = {d.code for d in checked.deferred}
        targets = [c for c in eq.target if c not in skip]
        final = plan_orders(eq.book, targets, eq.prices,
                            contribution=checked.contribution_to_stocks + checked.transfer, sell=eq.rebalance,
                            pending=eq.pending, keep=eq.keep, sold_earlier_this_month=sold,
                            gain_earlier_this_month=gain, loss_carryforward=carried)
        if eq.rebalance or not Shadow(out / "sombra_diretor.json").events:
            Shadow(out / "sombra_diretor.json").record(eq.day.isoformat(), targets)

    spent = models.spent if models else 0.0
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
                                          "escalations": checked.escalations + list(checked.decision.escalations)},
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
           f"| Reserva de emergência (caixa e pós-fixado diário) | {brl(alloc['reserve_eligible'])} "
           f"| {brl(alloc['reserve_target'])} ({policy.reserve_months} meses) |",
           f"| Ações (do patrimônio investível) | {brl(alloc['stocks'])} ({pct(alloc['stock_share_of_investable']).lstrip('+')}) "
           f"| {pct(policy.stock_target, 0).lstrip('+')} |",
           f"| Fundos listados (FII, Fiagro, Infra) | {brl(alloc['listed_funds'])} "
           f"({pct(alloc['listed_funds_share_of_investable']).lstrip('+')}) | até {pct(policy.listed_funds_max, 0).lstrip('+')} |",
           "", f"- Aporte do mês: {brl(alloc['contribution'])}, dos quais **{brl(checked.contribution_to_stocks)} "
           "para ações**" + (f" e **{brl(checked.to_reserve)} para completar a reserva**" if checked.to_reserve else "")
           + ".",
           f"- Transferência da renda fixa para ações: **{brl(checked.transfer)}** "
           f"(regra: {brl(alloc['rule_monthly_transfer'])} por mês em {alloc['transition_months']} meses)"
           + (", saindo de " + ", ".join(f"{n} ({brl(v)})" for n, v in checked.transfer_sources)
              if checked.transfer_sources else "") + "."]
    if checked.to_daily_post_fixed:
        out.append(f"- Fica na renda fixa pós-fixada de liquidez diária: {brl(checked.to_daily_post_fixed)}.")
    if alloc["reserve_deficit"]:
        out.append(f"- Reserva de emergência incompleta: faltam {brl(alloc['reserve_deficit'])}. Nada sai da renda "
                   "fixa para ações até completá-la.")
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
    if d.escalations or checked.escalations:
        out += ["", "### Para você decidir ou confirmar", ""]
        out += [f"- {e}" for e in checked.escalations + list(d.escalations)]
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
        detail = "; ".join(f"{m}: US$ {v:.3f}" for m, v in models.budget.by_model.items())
        out.append(f"_Custo dos modelos nesta reunião: US$ {spent:.3f} ({detail}; alertas e risco de corte inclusos)._")
    return "\n".join(out) + "\n"
