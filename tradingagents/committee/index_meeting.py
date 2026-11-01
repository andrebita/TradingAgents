"""A committee meeting for the portfolio by index funds (review 5).

Code builds the month's plan (``indices.build``); four analysts read it (fixed income, risk, tax, macro); the
director picks the Brazil ETF from the policy's menu and may hold back up to two migration sales; code checks
that and rebuilds the plan. The letter opens with the portfolio asset by asset, then the money, the tax, the
crisis status and what the investor must approve; the director's letter and the reports follow.
"""

from __future__ import annotations

import contextvars
import copy
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.committee import analysts, director, indices
from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import Wealth
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import b3_market
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dividend_portfolio.monitor import Budget
from tradingagents.dividend_portfolio.report import brl, pct

logger = logging.getLogger(__name__)
PRICE_MAX_AGE_DAYS = 7
SLEEVE_NAMES = {"brasil": "Ações Brasil", "internacional": "Ações globais (ETF)",
                "renda_fixa": "Renda fixa (inclui a reserva)", "fundos_listados": "Fundos listados (FII, Fiagro, Infra)"}
STRESS = {"brasil": -0.50, "internacional": -0.35, "fundos_listados": -0.30, "renda_fixa": 0.0}


def risk_facts(policy: InvestmentPolicy, plan: indices.Plan) -> tuple[dict, list[str]]:
    total_today = sum(v["today"] for k, v in plan.sleeves.items()) - plan.reserve_target
    total_target = sum(v["target"] for k, v in plan.sleeves.items()) - plan.reserve_target

    def fall(which: str, total: float) -> float:
        investable = {k: v[which] - (plan.reserve_target if k == "renda_fixa" else 0.0) for k, v in plan.sleeves.items()}
        return sum(STRESS[k] * v for k, v in investable.items()) / total if total else 0.0

    facts = {"simultaneous_stress": {"assumed_falls": STRESS, "today": round(fall("today", total_today), 3),
                                     "target": round(fall("target", total_target), 3)},
             "allocation_tests": {"policy_C_beat_selic_5y": 0.78, "policy_C_without_fx_drift": 0.60,
                                  "brazil_40_selic_60": 0.53, "brazil_60_selic_40": 0.51},
             "investor_drawdown_tolerance": policy.max_drawdown_tolerance, "crisis": plan.crisis,
             "sleeves": plan.sleeves}
    warnings = []
    if policy.max_drawdown_tolerance is None:
        warnings.append("Tolerância a quedas não informada na política.")
    elif -facts["simultaneous_stress"]["target"] > policy.max_drawdown_tolerance:
        warnings.append(f"Num estresse simultâneo, a carteira-alvo cai {-facts['simultaneous_stress']['target']:.0%}, "
                        f"mais que a sua tolerância ({policy.max_drawdown_tolerance:.0%}).")
    if plan.crisis.get("status") != "normal":
        warnings.append(f"Situação de mercado: {plan.crisis['status']}. Vale o plano de crise escrito.")
    return facts, warnings


def tax_facts(plan: indices.Plan) -> dict:
    sales = [o for o in plan.orders if o.side in ("venda", "resgatar")]
    return {"sales": [{"asset": o.asset, "value": round(o.value, 2), "tax": o.tax, "reason": o.reason} for o in sales],
            "estimated_tax": round(sum(o.tax or 0.0 for o in sales), 2),
            "unknown_tax": [o.asset for o in sales if o.tax is None and o.reason.startswith("migração")],
            "rules": {"stocks": "isentas até R$ 20 mil de vendas no mês; acima, 15% sobre o ganho",
                      "etf": "15% sobre o ganho na venda, sem isenção",
                      "fii_fiagro": "20% sobre o ganho na venda; rendimentos isentos com 100+ cotistas e menos de "
                                    "10% das cotas", "fi_infra": "alíquota zero se enquadrado na Lei 12.431",
                      "tesouro": "tabela regressiva: 22,5% até 180 dias, 20% até 360, 17,5% até 720, 15% depois",
                      "jcp": "17,5% retido desde 2026"}}


def previous_vehicle(out: Path, default: str) -> str:
    log = out / "decisoes.jsonl"
    if log.exists():
        for line in reversed(log.read_text(encoding="utf-8").splitlines()):
            record = json.loads(line)
            if record.get("brazil_vehicle"):
                return record["brazil_vehicle"]
    return default


def hold(policy: InvestmentPolicy, wealth: Wealth, *, contribution: float | None = None, as_of: date | None = None,
         use_llm: bool = True, config: dict | None = None):
    from tradingagents.committee.meeting import Meeting

    config = copy.deepcopy(config or DEFAULT_CONFIG)
    out = Path(config["results_dir"]) / "comite"
    out.mkdir(parents=True, exist_ok=True)
    ip = policy.index_plan
    contribution = policy.monthly_contribution if contribution is None else contribution
    today = as_of or date.today()
    with run_config(config):
        codes = {x.code.strip().upper() for x in wealth.listed} | set(ip.brazil_menu) | {ip.international_vehicle}
        px = b3_market.histories(codes)
        prices, history = {}, {}
        for code in codes:
            close = px[code]["Close"].loc[:pd.Timestamp(today)] if code in px else pd.Series(dtype=float)
            if len(close) and (pd.Timestamp(today) - close.index[-1]).days <= PRICE_MAX_AGE_DAYS:
                prices[code] = float(close.iloc[-1])
            if code in set(ip.brazil_menu) | {ip.international_vehicle} and len(close):
                history[code] = close
        try:
            from tradingagents.dataflows.vendors import tesouro_direto

            offers = tesouro_direto.offers(today)
        except Exception as exc:  # the plan still works, naming Tesouro Selic without a bond
            logger.warning("Tesouro Direto offers unavailable: %s", exc)
            offers = None
        previous = previous_vehicle(out, ip.brazil_default)
        plan = indices.build(policy, wealth, prices, today, contribution, offers=offers, history=history,
                             brazil_vehicle=previous)
        models = analysts.Models(config, Budget(policy.budget_usd)) if use_llm else None

        with ThreadPoolExecutor(max_workers=4) as pool:
            def submit(fn, *args):
                return pool.submit(contextvars.copy_context().run, fn, *args)

            risk_f, risk_w = risk_facts(policy, plan)
            jobs = [submit(analysts.fixed_income, policy, wealth, today, models),
                    submit(analysts._with_view, analysts.Report(analysts.ROLES["risk"], risk_f, warnings=risk_w),
                           models, policy.models.risk,
                           "Explain how the target mix behaves in a simultaneous fall and what the investor's "
                           "tolerance implies. Do not forecast.", policy),
                    submit(analysts._with_view, analysts.Report(analysts.ROLES["tax"], tax_facts(plan)), models,
                           policy.models.tax, "Check this month's sales and redemptions for income tax and say what "
                           "the investor must record or confirm.", policy),
                    submit(analysts.macro, policy, today, models)]
            reports = [j.result() for j in jobs]

        decision = director.IndexDecision(letter="Reunião sem LLM: o plano acima é o do código, sem leitura do "
                                                 "diretor.", brazil_vehicle=previous,
                                          brazil_vehicle_reason="Sem diretor nesta reunião: vale a escolha anterior "
                                                                "ou o padrão do protocolo.")
        director_ran = False
        if models is not None:
            try:
                decision = director.ask_index(models.llm(policy.models.director, effort="high",
                                                         max_output=analysts.DIRECTOR_MAX_OUTPUT),
                                              policy, plan.facts(), reports, previous, budget=models.budget,
                                              max_output=analysts.DIRECTOR_MAX_OUTPUT)
                director_ran = True
            except Exception as exc:  # a refusal, an invalid answer or the budget: the code's plan stands
                logger.warning("The director failed: %s", exc)
                decision.letter = f"O diretor não respondeu nesta reunião ({type(exc).__name__}). O plano acima é o do código."
                decision.escalations = [f"Rodar a reunião de novo: o diretor falhou ({type(exc).__name__})."]
        checked = director.check_index(decision, policy, {o.asset for o in plan.migration_sales()})
        plan = indices.build(policy, wealth, prices, today, contribution, offers=offers, history=history,
                             brazil_vehicle=checked.vehicle, held_back=checked.held_back)

    spent = models.spent if models else 0.0
    letter = render(today, policy, wealth, plan, checked, reports, spent, models, director_ran)
    path = out / f"carta_{today.isoformat()}.md"
    path.write_text(letter, encoding="utf-8")
    with (out / "decisoes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": today.isoformat(), "mode": "indices", "brazil_vehicle": checked.vehicle,
                             "default_vehicle": ip.brazil_default, "held_back": sorted(checked.held_back),
                             "adjustments": checked.adjustments, "plan": plan.facts(),
                             "escalations": list(checked.decision.escalations), "cost_usd": round(spent, 4)},
                            ensure_ascii=False, default=str) + "\n")
    return Meeting(letter, path, spent, checked, reports)


ACTION = {"compra": "Comprar", "venda": "Vender", "resgatar": "Resgatar", "aplicar": "Aplicar"}


def render(day: date, policy: InvestmentPolicy, wealth: Wealth, plan: indices.Plan, checked, reports: list,
           spent: float, models, director_ran: bool) -> str:
    ip = policy.index_plan
    d = checked.decision
    out = [f"# Comitê de Investimentos: reunião de {day.isoformat()}", "",
           "> Recomendações para sua decisão, dentro da sua política. Não é garantia de resultado. Nada é executado "
           "sem você.", "", "## 1. Carteira recomendada", "",
           f"Plano: {ip.brazil:.0%} ações Brasil, {ip.international:.0%} ações globais e {ip.fixed_income:.0%} "
           "Tesouro Selic, sobre o patrimônio investível; a reserva de emergência fica à parte.", "",
           "### O que fazer com cada ativo", "",
           "| Ativo | Classe | Ação | Quantidade | Valor | Motivo |", "|---|---|---|---|---|---|"]
    acted = set()
    for o in plan.orders:
        acted.add(o.asset)
        q = "" if o.quantity is None else f"{o.quantity:,.0f}".replace(",", ".")
        out.append(f"| {o.asset} | {SLEEVE_NAMES.get(o.sleeve, o.sleeve)} | **{ACTION.get(o.side, o.side)}** | {q} "
                   f"| {brl(o.value)} | {o.reason} |")
    for h in plan.holds:
        out.append(f"| {h.split(':')[0]} | Renda fixa | Manter | | | {h.split(':', 1)[1].strip()} |")
    for code, h in indices.consolidate(wealth).items():
        if code in acted:
            continue
        if code in set(ip.brazil_menu) | {ip.international_vehicle}:
            out.append(f"| {code} | ETF do plano | Manter | {h.quantity:,.0f} | | no plano |")
        elif h.kind == "acao":
            out.append(f"| {code} | Ações Brasil | Manter por ora | {h.quantity:,.0f} | | venda nos próximos meses, "
                       "dentro da isenção de R$ 20 mil |")
        else:
            out.append(f"| {code} | {SLEEVE_NAMES['fundos_listados']} | Manter por ora | {h.quantity:,.0f} | | "
                       f"venda aos poucos, em {ip.listed_funds_exit_months} meses |")
    out += ["", "### Onde o patrimônio está", "", "| Classe | Hoje | Depois do plano | Alvo |", "|---|---|---|---|"]
    for k, v in plan.sleeves.items():
        out.append(f"| {SLEEVE_NAMES[k]} | {brl(v['today'])} | {brl(v['after'])} | {brl(v['target'])} |")
    out += ["", f"- Reserva de emergência (caixa e pós-fixado diário): {brl(plan.reserve_eligible)} de "
                f"{brl(plan.reserve_target)}" + (f"; faltam {brl(plan.reserve_deficit)}, completados primeiro."
                                                  if plan.reserve_deficit else ".")]
    if plan.transfer:
        out.append(f"- Transição: {brl(plan.transfer)} da Selic acima da reserva vão para ações neste mês.")
    out.append(f"- ETF da parte Brasil: **{checked.vehicle}** ({d.brazil_vehicle_reason.strip()})")
    tax = sum(o.tax or 0.0 for o in plan.orders)
    out += ["", f"IR estimado nas vendas e resgates do mês: **{brl(tax)}**."]
    out += ["", f"### Mercado: {plan.crisis.get('status', 'normal')}", ""]
    falls = plan.crisis.get("falls_from_12m_high", {})
    if falls:
        out.append("Queda desde a máxima de 12 meses: " + ", ".join(f"{c} {pct(v, 1)}" for c, v in falls.items()) + ".")
    if plan.crisis.get("plan"):
        out += ["", "**Plano de crise, escrito antes da crise:**", ""] + [f"- {x}" for x in plan.crisis["plan"]]
    escalations = plan.pending + list(d.escalations)
    if escalations:
        out += ["", "### Para você decidir ou confirmar", ""] + [f"- {e}" for e in escalations]
    out += ["", "### Aprovação", "", "- [ ] Compras e vendas de ETFs e ações", "- [ ] Resgates e aplicações no Tesouro"]
    if checked.adjustments:
        out += ["", "### O que a checagem mudou na decisão do diretor", ""] + [f"- {a}" for a in checked.adjustments]
    if director_ran:
        out += ["", f"## 2. Carta do Diretor Financeiro ({policy.models.director})", "",
                "_Escrita antes da checagem; onde divergir do plano acima, vale o plano._", "", d.letter.strip(), ""]
    else:
        out += ["", "## 2. Sem carta do diretor nesta reunião", "", d.letter.strip(), ""]
    out += ["", "## 3. Relatórios dos analistas", ""]
    for r in reports:
        out += [f"### {r.role}", ""]
        if r.view:
            out += [r.view.summary.strip(), ""] + [f"- {x}" for x in r.view.recommendations]
            out += [f"- Risco: {x}" for x in r.view.risks]
        if r.error:
            out.append(f"- Leitura do modelo indisponível ({r.error}).")
        out += [f"- Aviso: {w}" for w in r.warnings] + [""]
    if models is not None:
        detail = "; ".join(f"{m}: US$ {v:.3f}" for m, v in models.budget.by_model.items())
        out.append(f"_Custo dos modelos nesta reunião: US$ {spent:.3f} ({detail})._")
    return "\n".join(out) + "\n"
