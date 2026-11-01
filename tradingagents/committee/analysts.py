"""The committee's analysts: facts computed in code, then read by each analyst's model.

Each analyst returns a ``Report``: the facts the director sees (plain data, so
the numbers are the code's, never the model's), the analyst's reading of them
and the warnings the code itself raised. A failed model call leaves the facts
and says so; the meeting goes on.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import Wealth
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.dataflows.vendors import b3_market
from tradingagents.dividend_portfolio.monitor import Budget
from tradingagents.llm_clients import create_tier_client

logger = logging.getLogger(__name__)

ROLES = {"fixed_income": "Analista de Renda Fixa", "equities": "Analista de Ações", "risk": "Analista de Risco",
         "tax": "Analista Tributário", "macro": "Analista Macro"}


class View(BaseModel):
    """An analyst's reading, for the director."""

    summary: str = Field(description="Four to eight sentences in Portuguese: what the facts say for this investor.")
    recommendations: list[str] = Field(description="Concrete recommendations in Portuguese, within the policy.")
    risks: list[str] = Field(description="Risks or open questions in Portuguese.")


@dataclass
class Report:
    role: str
    facts: dict
    view: View | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


ANALYST_MAX_OUTPUT = 4_000
DIRECTOR_MAX_OUTPUT = 24_000


@dataclass
class Models:
    """The clients the members need, and the meeting's budget, held before every call."""

    config: dict
    budget: Budget

    def __post_init__(self):
        # Cloud sessions drop ANTHROPIC_* variables; the key may come under another name.
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]

    def llm(self, model: str, effort: str = "medium", max_output: int = ANALYST_MAX_OUTPUT):
        tier = {**self.config, "llm_provider": "anthropic", "deep_think_llm": model, "anthropic_effort": effort,
                "max_tokens": max_output}
        return create_tier_client(tier, "deep").get_llm()

    @property
    def spent(self) -> float:
        return self.budget.spent


def ask_view(models: Models, model: str, role: str, brief: str, facts: dict, policy: InvestmentPolicy) -> View:
    prompt = (f"You are the {role} of a private investment committee in Brazil, reporting to the financial director. "
              f"{brief}\n\nThe investor's policy:\n{policy.model_dump_json(indent=1, exclude={'models'})}\n\n"
              f"The facts, computed by the committee's code (trust these numbers; do not recompute them):\n"
              f"{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}\n\n"
              "Write for the director in Portuguese. Stay within the policy. Do not forecast prices or rates: "
              "the committee's backtests found no edge in timing the market.")
    return models.budget.invoke(models.llm(model).with_structured_output(View), prompt, model=model,
                                max_output=ANALYST_MAX_OUTPUT)


def _with_view(report: Report, models: Models | None, model: str, brief: str, policy: InvestmentPolicy) -> Report:
    if models is None:
        return report
    try:
        report.view = ask_view(models, model, report.role, brief, report.facts, policy)
    except Exception as exc:  # the facts still reach the director
        logger.warning("%s failed: %s", report.role, exc)
        report.error = f"{type(exc).__name__}: {exc}"[:300]
    return report


# --- Allocation: the numbers every analyst shares ---------------------------------------

def allocation(policy: InvestmentPolicy, wealth: Wealth, market_values: dict[str, float],
               contribution: float) -> dict:
    """Where the wealth stands against the policy, and the rule's move for this month.

    Investable wealth is everything outside the emergency reserve. The reserve counts
    only cash and post-fixed positions with daily liquidity, and comes first: the
    contribution tops it up before anything else, and no money leaves fixed income
    for stocks while it is short. Then the contribution goes to stocks while they are
    below target; a shortfall beyond the band is closed from the money beyond the
    reserve in equal monthly transfers over the transition. Listed funds above their
    ceiling get no new money (nothing is sold). ``stock_ceiling`` (target plus band)
    is the most the stocks may reach with the month's money.

    ``market_values``: code to R$ of the listed holdings with a recent price; the
    others count at the value the investor gave. A position with neither is listed
    in ``unvalued`` and stops the transfer: a missing price is never zero.
    """
    valuation = wealth.valuation(market_values)
    stocks = valuation.by_class["acoes"]
    funds = valuation.by_class["fundos_listados"]
    international = valuation.by_class["internacional"]
    fixed = sum(x.value for x in wealth.fixed_income) + wealth.cash + valuation.by_class["renda_fixa"]
    total = stocks + funds + international + fixed
    investable = max(total - policy.reserve, 0.0)
    eligible = wealth.reserve_eligible()
    deficit = max(policy.reserve - eligible, 0.0)
    to_reserve = min(deficit, contribution)
    base = investable + contribution
    target_stocks = policy.stock_target * base
    to_stocks = min(max(target_stocks - stocks, 0.0), contribution - to_reserve)
    gap = target_stocks - stocks - to_stocks
    transfer = 0.0
    if gap > policy.band * investable and not deficit and not valuation.missing:
        transfer = min(gap / policy.transition_months, eligible - policy.reserve)

    def share(v: float) -> float:
        return v / investable if investable else 0.0

    return {"total": round(total, 2), "reserve_target": policy.reserve, "investable": round(investable, 2),
            "cash": round(wealth.cash, 2), "reserve_eligible": round(eligible, 2), "reserve_deficit": round(deficit, 2),
            "liquid_beyond_reserve": round(eligible - policy.reserve, 2),
            "rule_contribution_to_reserve": round(to_reserve, 2),
            "stock_ceiling": round((policy.stock_target + policy.band) * base, 2),
            "transfer_sources": [{"name": n, "amount": v} for n, v in wealth.transfer_sources(transfer)],
            "stocks": round(stocks, 2), "listed_funds": round(funds, 2), "international": round(international, 2),
            "fixed_income": round(fixed, 2), "listed_valuation": valuation.sources, "unvalued": valuation.missing,
            "stock_share_of_investable": round(share(stocks), 4), "stock_target": policy.stock_target,
            "listed_funds_share_of_investable": round(share(funds), 4), "listed_funds_max": policy.listed_funds_max,
            "listed_funds_above_ceiling": share(funds) > policy.listed_funds_max,
            "contribution": contribution, "rule_contribution_to_stocks": round(to_stocks, 2),
            "rule_contribution_to_fixed_income": round(contribution - to_stocks, 2),
            "stock_gap_after_contribution": round(gap, 2), "rule_monthly_transfer": round(transfer, 2),
            "transition_months": policy.transition_months}


# --- Fixed income ---------------------------------------------------------------------------

def fixed_income_facts(policy: InvestmentPolicy, wealth: Wealth, today: date) -> tuple[dict, list[str]]:
    fi = wealth.fixed_income
    liquid = [x for x in fi if x.liquidity_days is not None and x.liquidity_days <= 1]
    liquid_value = sum(x.value for x in liquid)
    eligible = wealth.reserve_eligible()
    by_indexer: dict[str, float] = {}
    for x in fi:
        by_indexer[x.indexer] = by_indexer.get(x.indexer, 0.0) + x.value
    issuers: dict[str, float] = {}
    for x in fi:
        if not x.sovereign:
            issuers[x.issuer] = issuers.get(x.issuer, 0.0) + x.value
    soon = [{"name": x.name, "maturity": x.maturity, "value": x.value} for x in fi
            if x.maturity and (x.maturity - today).days <= 365]
    long_dated = [{"name": x.name, "maturity": x.maturity, "value": x.value} for x in fi
                  if x.indexer in ("IPCA", "PRE") and x.maturity and (x.maturity - today).days > 10 * 365]
    warnings = []
    if eligible < policy.reserve:
        warnings.append(f"Reserva de emergência descoberta: R$ {eligible:,.0f} em caixa e pós-fixados de liquidez "
                        f"diária contra R$ {policy.reserve:,.0f} da política. O aporte completa a reserva primeiro, "
                        "e nada vai da renda fixa para ações até ela estar completa.")
    for issuer, value in issuers.items():
        if value > policy.private_issuer_max:
            warnings.append(f"{issuer}: R$ {value:,.0f} acima do limite do FGC por instituição.")
    uncovered = [x.name for x in fi if not x.sovereign and not x.fgc and x.kind != "fund"]
    facts = {"total": round(sum(x.value for x in fi), 2),
             "sovereign": round(sum(x.value for x in fi if x.sovereign), 2),
             "cash": round(wealth.cash, 2), "reserve_target": policy.reserve,
             "reserve_eligible": round(eligible, 2), "liquid_beyond_reserve": round(eligible - policy.reserve, 2),
             "reserve_positions": [x.name for x in fi if x.reserve_eligible],
             "daily_liquidity": round(liquid_value, 2),
             "daily_liquid_but_not_reserve": [{"name": x.name, "value": x.value, "why": "marcado a mercado"}
                                              for x in liquid if not x.reserve_eligible],
             "by_indexer": {k: round(v, 2) for k, v in by_indexer.items()},
             "private_issuers": {k: round(v, 2) for k, v in issuers.items()},
             "without_fgc_guarantee": uncovered, "maturing_within_12_months": soon,
             "long_dated_marked_to_market": long_dated,
             "tax_exempt": [x.name for x in fi if x.tax_exempt]}
    return facts, warnings


def fixed_income(policy, wealth, today, models: Models | None) -> Report:
    facts, warnings = fixed_income_facts(policy, wealth, today)
    brief = ("Assess the emergency reserve, where this month's transfer to stocks should come from (liquid money "
             "beyond the reserve first; never break long-dated bonds marked to market at a loss to fund it), what to "
             "do with maturities, and issuer and guarantee concentration.")
    return _with_view(Report(ROLES["fixed_income"], facts, warnings=warnings), models, policy.models.fixed_income,
                      brief, policy)


# --- Risk -----------------------------------------------------------------------------------

def class_returns(start: str = "2012-02-01") -> pd.DataFrame:
    """Monthly returns of the three classes: stocks (BOVA11, and DIVO11 for the dividend tilt), listed funds
    (XFIX11 from December 2020, before it the average of HGLG11 and KNRI11) and fixed income (CDI net of a 15%
    income tax)."""
    def monthly(series: pd.Series) -> pd.Series:
        return series.groupby(series.index.to_period("M")).last().pct_change()

    bova, divo = monthly(b3_market.index_series("BOVA11.SA")), monthly(b3_market.index_series("DIVO11.SA"))
    xfix = monthly(b3_market.index_series("XFIX11.SA"))
    old = pd.concat([monthly(b3_market.index_series(s)) for s in ("HGLG11.SA", "KNRI11.SA")], axis=1).mean(axis=1)
    funds = xfix.combine_first(old)
    cdi = b3_market.sgs_series(12)
    cdi = (1 + cdi / 100).groupby(cdi.index.to_period("M")).prod() - 1
    # What the investor keeps: 15% income tax on the yield (the long-term rate), as an approximation.
    frame = pd.DataFrame({"stocks_index": bova, "stocks_dividend": divo, "listed_funds": funds, "cdi": 0.85 * cdi})
    return frame.loc[pd.Period(start, "M"):].dropna()


def stress(returns: pd.DataFrame, stock_share: float, funds_share: float, stock_col: str) -> dict:
    mix = (stock_share * returns[stock_col] + funds_share * returns["listed_funds"]
           + (1 - stock_share - funds_share) * returns["cdi"])
    curve = (1 + mix).cumprod()
    drawdown = curve / curve.cummax().clip(lower=1.0) - 1   # from the starting value too
    rolling = (1 + mix).rolling(12).apply(np.prod, raw=True) - 1
    yearly = (1 + mix).groupby(mix.index.year).prod() - 1
    worst_year = int(yearly.idxmin())
    return {"annual_return": round(float(curve.iloc[-1] ** (12 / len(mix)) - 1), 4),
            "worst_drawdown": round(float(drawdown.min()), 4),
            "worst_drawdown_month": str(drawdown.idxmin()),
            "worst_12_months": round(float(rolling.min()), 4),
            "worst_calendar_year": {"year": worst_year, "return": round(float(yearly.min()), 4)},
            "share_of_negative_12_month_windows": round(float((rolling < 0).mean()), 3)}


SECTOR_WARNING = 0.25           # share of the stocks in one CVM sector that raises a warning
STRESS_FALLS = {"stocks": -0.59, "listed_funds": -0.30, "fixed_income": 0.0}


def risk_facts(policy, wealth, alloc: dict, stock_values: dict[str, float], sectors: dict[str, str]) -> tuple[dict, list]:
    investable = alloc["investable"] or 1.0
    returns = class_returns()
    now = (alloc["stocks"] / investable, alloc["listed_funds"] / investable)
    target = (policy.stock_target, min(alloc["listed_funds"] / investable, policy.listed_funds_max))
    stocks_total = sum(stock_values.values()) or 1.0
    largest = sorted(stock_values.items(), key=lambda kv: -kv[1])[:5]
    by_sector: dict[str, float] = {}
    for code, value in stock_values.items():
        by_sector[sectors.get(code, "não identificado")] = by_sector.get(sectors.get(code, "não identificado"), 0) + value
    warnings = []
    top_sector, top_value = max(by_sector.items(), key=lambda kv: kv[1]) if by_sector else ("", 0)
    if top_value / stocks_total > SECTOR_WARNING:
        warnings.append(f"{top_value / stocks_total:.0%} das ações num só setor ({top_sector}).")
    target_fall = stress(returns, *target, "stocks_dividend")["worst_drawdown"]
    tolerance = policy.max_drawdown_tolerance
    if tolerance is None:
        warnings.append("Tolerância a quedas não informada na política: o comitê não sabe se o mix-alvo cabe nela.")
    elif -target_fall > tolerance:
        warnings.append(f"A pior queda histórica do mix-alvo ({target_fall:.0%}) passa da sua tolerância "
                        f"(−{tolerance:.0%}).")
    # A simultaneous fall, not a forecast: the rule's worst (−59%) in stocks, −30% in listed funds, Selic flat.
    def simultaneous(stock_share, funds_share):
        return round(STRESS_FALLS["stocks"] * stock_share + STRESS_FALLS["listed_funds"] * funds_share, 3)
    facts = {"history": f"{returns.index[0]} a {returns.index[-1]}, mensal, rebalanceado todo mês; reserva fora",
             "investor_drawdown_tolerance": policy.max_drawdown_tolerance,
             "simultaneous_stress": {"assumed_falls": STRESS_FALLS,
                                     "current_mix": simultaneous(*now), "target_mix": simultaneous(*target),
                                     "stock_ceiling_mix": simultaneous(policy.stock_target_max, target[1])},
             "listed_funds_proxy": "XFIX11 desde dez/2020; antes, média de HGLG11 e KNRI11",
             "fixed_income_proxy": "CDI líquido de 15% de IR (aproximação)",
             "current_mix": {"stocks": round(now[0], 3), "listed_funds": round(now[1], 3),
                             "with_index": stress(returns, *now, "stocks_index"),
                             "with_dividend_tilt": stress(returns, *now, "stocks_dividend")},
             "target_mix": {"stocks": target[0], "listed_funds": round(target[1], 3),
                            "with_index": stress(returns, *target, "stocks_index"),
                            "with_dividend_tilt": stress(returns, *target, "stocks_dividend")},
             "fixed_income_only": stress(returns, 0.0, 0.0, "stocks_index"),
             "largest_stocks": [{"code": c, "share_of_stocks": round(v / stocks_total, 3)} for c, v in largest],
             "stocks_by_sector": {k: round(v / stocks_total, 3) for k, v in
                                  sorted(by_sector.items(), key=lambda kv: -kv[1])}}
    return facts, warnings


def risk(policy, wealth, alloc, stock_values, sectors, models: Models | None) -> Report:
    facts, warnings = risk_facts(policy, wealth, alloc, stock_values, sectors)
    brief = ("Explain how the current and the target mix of the investable wealth fared in past crises (worst fall, "
             "worst year, how often 12 months ended negative), whether the investor's drawdown tolerance (in the "
             "facts) holds, and the concentration of the stocks. The history uses index proxies for whole classes; "
             "say that the investor's own positions can fall more.")
    return _with_view(Report(ROLES["risk"], facts, warnings=warnings), models, policy.models.risk, brief, policy)


# --- Tax ------------------------------------------------------------------------------------

REGRESSIVE = [(180, 0.225), (360, 0.20), (720, 0.175), (None, 0.15)]


def tax_facts(policy, wealth, equity_plan) -> dict:
    taxable = [x for x in wealth.fixed_income if not x.tax_exempt]
    return {"tax_records_month": wealth.tax.month,
            "stock_loss_carryforward_before": wealth.tax.stock_loss_carryforward,
            "stock_sales_this_month": round(getattr(equity_plan, "sales_this_month", 0.0), 2),
            "stock_sales_exemption": 20_000.0,
            "estimated_tax_on_stock_sales": round(getattr(equity_plan, "estimated_tax", 0.0), 2),
            "loss_carryforward_after": round(getattr(equity_plan, "loss_carryforward_after", 0.0), 2),
            "deferred_sales": [f"{o.code} {o.quantity}" for o in getattr(equity_plan, "deferred", [])],
            "fixed_income_regressive_table": "até 180 dias 22,5%; até 360 20%; até 720 17,5%; acima 15%; "
                                             "IOF nos primeiros 30 dias",
            "purchase_dates_known": all(x.applied_on for x in taxable),
            "taxable_fixed_income": [{"name": x.name, "value": x.value, "liquidity_days": x.liquidity_days}
                                     for x in taxable],
            "tax_exempt_fixed_income": [x.name for x in wealth.fixed_income if x.tax_exempt],
            "dividends": "isentos para pessoa física até R$ 50 mil por mês da mesma empresa; acima disso, 10% "
                         "retidos sobre o total pago no mês (Lei 15.270/2025, desde jan/2026), salvo lucros até "
                         "2025 com distribuição aprovada até 31/12/2025; tributação mínima anual para rendas "
                         "acima de R$ 600 mil",
            "interest_on_capital": "JCP com 17,5% retido na fonte desde 1º/1/2026 (LC 224/2025; antes, 15%)",
            "listed_funds": "FII e Fiagro: rendimentos isentos se o fundo tiver ao menos 100 cotistas e você tiver "
                            "menos de 10% das cotas (Lei 11.033, art. 3º); venda com 20% sobre o ganho, sem a "
                            "isenção de R$ 20 mil. FI-Infra da Lei 12.431: alíquota zero nos rendimentos e na "
                            "venda, se o enquadramento for confirmado no regulamento",
            "etfs": "venda de ETF de ações: 15% sobre o ganho, sem a isenção de R$ 20 mil"}


def tax(policy, wealth, equity_plan, models: Models | None) -> Report:
    facts = tax_facts(policy, wealth, equity_plan)
    brief = ("Say which money to move first for this month's transfer to stocks at the least tax (liquid post-fixed "
             "positions beyond the reserve, maturities; exempt positions earn more net, so they go last), whether "
             "the stock sales fit the monthly exemption, and what the investor should record for the tax return.")
    return _with_view(Report(ROLES["tax"], facts), models, policy.models.tax, brief, policy)


# --- Macro ----------------------------------------------------------------------------------

MACRO = ("selic", "ipca_12m", "focus_selic", "focus_ipca", "usd_brl")


def macro(policy, today: date, models: Models | None) -> Report:
    facts = {}
    for alias in MACRO:
        try:
            facts[alias] = route_to_vendor("get_macro_indicators", alias, today.isoformat(), 120)
        except VendorError as exc:
            facts[alias] = f"indisponível: {exc}"
    brief = ("Summarize rates, inflation and the market's expectations as context for the director: the real rate "
             "on fixed income, what it means for the investor's choice between post-fixed, inflation-linked and "
             "fixed-rate bonds at the investor's horizon. Context only: no market timing.")
    return _with_view(Report(ROLES["macro"], facts), models, policy.models.macro, brief, policy)
