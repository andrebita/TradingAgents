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

PRICE_MAX_AGE_DAYS = 7      # an older close does not value a position; the investor's value or nothing does


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
        letter = (f"O diretor não respondeu nesta reunião ({failure}). O plano acima é o da regra, conferido pela "
                  "política, sem a leitura do diretor.")
    else:
        letter = "Reunião sem LLM: o plano acima é o da regra, sem leitura do diretor."
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
        quantities: dict[str, float] = {}
        for x in wealth.listed:
            quantities[x.code.strip().upper()] = quantities.get(x.code.strip().upper(), 0.0) + x.quantity
        px = b3_market.histories(set(quantities))
        market, closes = {}, []
        for code, quantity in quantities.items():
            close = px[code]["Close"].loc[:pd.Timestamp(today)] if code in px else pd.Series(dtype=float)
            if len(close) and (pd.Timestamp(today) - close.index[-1]).days <= PRICE_MAX_AGE_DAYS:
                market[code] = quantity * float(close.iloc[-1])
                closes.append(close.index[-1].date())
        alloc = analysts.allocation(policy, wealth, market, contribution)
        values = wealth.valuation(market).of_class("acoes")
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
        director_ran = False
        if models is not None:
            try:
                decision = director.ask(models.llm(policy.models.director, effort="high",
                                                   max_output=analysts.DIRECTOR_MAX_OUTPUT),
                                        policy, alloc, reports, orders_json(eq.plan), budget=models.budget,
                                        max_output=analysts.DIRECTOR_MAX_OUTPUT)
                director_ran = True
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
    data = {"wealth_as_of": wealth.as_of, "prices_as_of": max(closes) if closes else None,
            "director": policy.models.director if director_ran else None}
    letter = render(eq.day, policy, wealth, alloc, checked, final, reports, spent, models, data)
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


def flows(alloc: dict, checked: director.Checked) -> list[tuple[str, str, float]]:
    """The month's money movements, (from, to, R$), as the policy check allowed them."""
    out = []
    if checked.to_reserve:
        out.append(("Aporte", "Reserva de emergência (pós-fixado diário)", checked.to_reserve))
    if checked.contribution_to_stocks:
        out.append(("Aporte", "Ações (ordens abaixo)", checked.contribution_to_stocks))
    out += [(name, "Ações (ordens abaixo)", amount) for name, amount in checked.transfer_sources]
    out += [(a.instrument, "Renda fixa (resgate)", a.amount) for a in checked.fixed_income_actions
            if a.action == "resgatar"]
    out += [("Aporte e resgates", a.instrument, a.amount) for a in checked.fixed_income_actions
            if a.action == "aplicar"]
    if checked.to_daily_post_fixed:
        out.append(("Aporte e resgates", "Renda fixa pós-fixada de liquidez diária", checked.to_daily_post_fixed))
    return out


def render(day: date, policy: InvestmentPolicy, wealth: Wealth, alloc: dict, checked: director.Checked, final,
           reports: list, spent: float, models, data: dict | None = None) -> str:
    """The letter: the checked plan first, to approve item by item; the director's letter and the reports after."""
    data = data or {}
    d = checked.decision

    def share(v: float) -> str:
        return pct(v, 1).lstrip("+")

    eligible_out = sum(a.amount for a in checked.fixed_income_actions if a.action == "resgatar"
                       and any(x.name == a.instrument and x.reserve_eligible for x in wealth.fixed_income))
    eligible_in = sum(a.amount for a in checked.fixed_income_actions if a.action == "aplicar"
                      and a.kind == "tesouro_selic")
    reserve_after = (alloc["reserve_eligible"] + checked.to_reserve + checked.to_daily_post_fixed - checked.transfer
                     - eligible_out + eligible_in)
    base = alloc["investable"] + alloc["contribution"]
    stocks_after = alloc["stocks"] + checked.contribution_to_stocks + checked.transfer
    out = [f"# Comitê de Investimentos: reunião de {day.isoformat()}", "",
           "> Recomendações para sua decisão, dentro da sua política de investimentos. Não é garantia de resultado. "
           "Nada é executado sem você.", "",
           "## 1. Plano do mês, conferido pela política", "",
           "### Dados usados", "",
           f"- Saldos informados em: **{data.get('wealth_as_of') or 'data não informada'}**.",
           f"- Cotações de: {data.get('prices_as_of') or 'sem cotação'}; posições sem cotação recente entram pelo "
           "valor informado."]
    if alloc.get("unvalued"):
        out.append("- **Sem valor:** " + "; ".join(alloc["unvalued"]) + ". Sem transferência para ações até completar.")
    out += ["", "### Onde o patrimônio está", "",
            "| | Hoje | Depois do plano | Política |", "|---|---|---|---|",
            f"| Reserva de emergência (caixa e pós-fixado diário) | {brl(alloc['reserve_eligible'])} "
            f"| {brl(reserve_after)} | {brl(alloc['reserve_target'])} ({policy.reserve_months} meses) |",
            f"| Ações, % do investível | {brl(alloc['stocks'])} ({share(alloc['stock_share_of_investable'])}) "
            f"| {brl(stocks_after)} ({share(stocks_after / base if base else 0)}) "
            f"| alvo {share(policy.stock_target)}, teto {share(policy.stock_target + policy.band)} |",
            f"| Fundos listados (FII, Fiagro, Infra) | {brl(alloc['listed_funds'])} "
            f"({share(alloc['listed_funds_share_of_investable'])}) | {brl(alloc['listed_funds'])} "
            f"| até {share(policy.listed_funds_max)}; acima disso, sem dinheiro novo |"]
    if alloc.get("international"):
        out.append(f"| Internacional (ETF, BDR) | {brl(alloc['international'])} | {brl(alloc['international'])} "
                   "| sem alvo na política |")
    if alloc["reserve_deficit"]:
        out.append(f"\nReserva incompleta: faltam {brl(alloc['reserve_deficit'])}. O aporte a completa primeiro, e "
                   "nada sai da renda fixa para ações até lá.")

    moves = flows(alloc, checked)
    out += ["", "### Movimentações", ""]
    if moves:
        out += ["| De | Para | Valor |", "|---|---|---|"] + [f"| {a} | {b} | {brl(v)} |" for a, b, v in moves]
    else:
        out.append("Nenhuma.")

    out += ["", "### Ordens de ações", ""]
    if final.orders:
        out += ["| Ordem | Ação | Quantidade | Valor | Motivo |", "|---|---|---|---|---|"]
        out += [f"| {o.side} | {o.code} | {o.quantity} | {brl(o.value)} | {o.reason} |" for o in final.orders]
    else:
        out.append("Nenhuma.")
    if final.deferred:
        out.append("\nVendas adiadas para manter a isenção de IR (a rotina as retoma no mês seguinte): "
                   + ", ".join(f"{o.code} {o.quantity}" for o in final.deferred))
    if checked.deferred:
        out.append("\nCompras adiadas pelo diretor: " + "; ".join(f"{x.code} ({x.reason})" for x in checked.deferred))
    out += [f"\nIR estimado sobre as vendas de ações: **{brl(final.estimated_tax)}** (vendas do mês "
            f"{brl(final.sales_this_month)}; isenção até R$ 20.000,00). Prejuízo a compensar depois: "
            f"{brl(final.loss_carryforward_after)}."]
    out += [f"- {n}" for n in final.notes]
    if any(a.action == "manter" for a in checked.fixed_income_actions):
        out.append("\nManter: " + "; ".join(f"{a.instrument} ({a.reason})" for a in checked.fixed_income_actions
                                             if a.action == "manter"))

    escalations = checked.escalations + list(d.escalations)
    if escalations:
        out += ["", "### Para você decidir ou confirmar", ""] + [f"- {e}" for e in escalations]
    out += ["", "### Aprovação", "", "- [ ] Movimentações de dinheiro", "- [ ] Ordens de ações"]
    if any(a.action != "manter" for a in checked.fixed_income_actions):
        out.append("- [ ] Resgates e aplicações de renda fixa")
    if checked.adjustments:
        out += ["", "### O que a checagem mudou na decisão do diretor", ""] + [f"- {a}" for a in checked.adjustments]

    if data.get("director"):
        out += ["", f"## 2. Carta do Diretor Financeiro ({data['director']})", "",
                "_Escrita antes da checagem da política: onde divergir do plano acima, vale o plano._", "",
                d.letter.strip(), ""]
    else:
        out += ["", "## 2. Sem carta do diretor nesta reunião", "", d.letter.strip(), ""]
    out += ["", "## 3. Relatórios dos analistas", ""]
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
