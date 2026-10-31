"""The financial director: decides within the policy, and code holds him to it.

He reads the policy, the allocation and the five analysts' reports and returns a
structured decision with a letter to the investor. Every number is then checked
against the policy: what does not hold is brought back to the rule's value and
listed, so the investor sees both the director's intent and what was allowed.
His one discretion over stocks is to defer buying a newly selected company
(at most two a meeting); the shadow record measures whether that helps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from tradingagents.committee.policy import InvestmentPolicy

MAX_VETOES = 2


class FixedIncomeAction(BaseModel):
    action: Literal["resgatar", "aplicar", "manter"]
    instrument: str = Field(description="An existing position's exact name, or for new money a kind of bond "
                                        "(e.g. 'Tesouro Selic', 'Tesouro IPCA+ 2035', 'CDB liquidez diária').")
    amount: float = Field(ge=0, description="R$")
    reason: str = Field(description="One sentence in Portuguese.")


class Deferral(BaseModel):
    code: str = Field(description="A newly selected stock whose purchase to defer this month.")
    reason: str = Field(description="One sentence in Portuguese.")


class DirectorDecision(BaseModel):
    letter: str = Field(description="The letter to the investor, in Portuguese, plain language, 300-700 words: "
                                    "where the wealth stands, what the committee recommends this month and why, "
                                    "the main risks and what to watch, and the decisions that are the investor's.")
    contribution_to_stocks: float = Field(ge=0, description="R$ of this month's contribution to stocks.")
    monthly_transfer_to_stocks: float = Field(ge=0, description="R$ moved this month from fixed income to stocks.")
    fixed_income_actions: list[FixedIncomeAction] = Field(default_factory=list)
    deferred_buys: list[Deferral] = Field(default_factory=list,
                                          description=f"At most {MAX_VETOES}; only stocks not held yet.")
    escalations: list[str] = Field(default_factory=list,
                                   description="Decisions or confirmations that only the investor can give.")


@dataclass
class Checked:
    """The decision as the policy allows it, and what was changed to get there."""

    decision: DirectorDecision
    contribution_to_stocks: float
    transfer: float
    deferred: list[Deferral]
    fixed_income_actions: list[FixedIncomeAction]
    adjustments: list[str] = field(default_factory=list)


PROMPT = """You are the senior financial director of a private investment committee in Brazil. Your client is one individual investor; his objective and limits are in his written policy below. You recommend; he decides.

How the committee works, from its own tests (2012-2026 backtests, pre-registered):
- No member forecasts prices or rates: models did not beat the market at that, and timing between stocks and fixed income lost to holding a fixed mix.
- Stocks follow a rule (the 10 highest 24-month dividend yields among the 50 most traded steady payers, rebalanced in June and December). It earned about 11-14% a year against 7.5% for the Ibovespa, with deeper falls, and its edge is not statistically proven.
- Your value is discipline, risk control, tax and liquidity: keep the reserve, move toward the target mix at the agreed pace, avoid costly mistakes, explain.

Your discretion, all checked by code against the policy:
- contribution_to_stocks: up to this month's contribution; the rule's figure is in the allocation.
- monthly_transfer_to_stocks: from 0 to twice the rule's monthly transfer, and never below the emergency reserve.
- deferred_buys: at most {max_vetoes} newly selected stocks whose first purchase you defer this month, with a reason. Use it sparingly: deferring stocks the market already marked down has lost return in the tests.
- fixed_income_actions: which positions fund the transfer, what to do with maturities, where new fixed-income money goes. Use exact position names.
- escalations: whatever only the investor can decide or confirm (missing data, raising the stock target at a yearly review, anything outside the policy).

The policy:
{policy}

Allocation (computed by code):
{allocation}

The analysts' reports (facts computed by code, then each analyst's reading):
{reports}

The rule's stock orders for this month, before your decision:
{orders}

Write the letter for an investor who is not a specialist: concrete amounts in R$, no jargon without a short explanation, honest about uncertainty."""


def ask(llm, policy: InvestmentPolicy, alloc: dict, reports: list, orders: list[dict]) -> DirectorDecision:
    def report_json(r):
        return {"role": r.role, "facts": r.facts, "warnings": r.warnings, "error": r.error,
                "reading": r.view.model_dump() if r.view else None}

    prompt = PROMPT.format(max_vetoes=MAX_VETOES, policy=policy.model_dump_json(indent=1, exclude={"models"}),
                           allocation=json.dumps(alloc, ensure_ascii=False, indent=1),
                           reports=json.dumps([report_json(r) for r in reports], ensure_ascii=False, indent=1,
                                              default=str),
                           orders=json.dumps(orders, ensure_ascii=False, indent=1))
    return llm.with_structured_output(DirectorDecision).invoke(prompt)


def check(decision: DirectorDecision, alloc: dict, liquid_beyond_reserve: float, new_buys: set[str],
          positions: dict[str, float]) -> Checked:
    """Bring each figure within the policy; list every change."""
    notes = []
    contribution = alloc["contribution"]
    to_stocks = decision.contribution_to_stocks
    if to_stocks > contribution:
        notes.append(f"Aporte para ações reduzido de R$ {to_stocks:,.2f} para o aporte do mês, R$ {contribution:,.2f}.")
        to_stocks = contribution
    ceiling = min(2 * alloc["rule_monthly_transfer"], max(liquid_beyond_reserve, 0.0))
    transfer = decision.monthly_transfer_to_stocks
    if transfer > ceiling:
        notes.append(f"Transferência reduzida de R$ {transfer:,.2f} para R$ {ceiling:,.2f}: o máximo é o dobro do "
                     "ritmo da regra, sem tocar a reserva de emergência.")
        transfer = ceiling
    deferred = []
    for d in decision.deferred_buys:
        code = d.code.strip().upper().removesuffix(".SA")
        if code not in new_buys:
            notes.append(f"Adiamento de {code} desconsiderado: só vale para compras novas desta seleção.")
        elif len(deferred) >= MAX_VETOES:
            notes.append(f"Adiamento de {code} desconsiderado: no máximo {MAX_VETOES} por reunião.")
        else:
            deferred.append(Deferral(code=code, reason=d.reason))
    actions = []
    for a in decision.fixed_income_actions:
        if a.action == "resgatar" and a.instrument in positions and a.amount > positions[a.instrument]:
            notes.append(f"Resgate de {a.instrument} limitado ao saldo, R$ {positions[a.instrument]:,.2f}.")
            a = a.model_copy(update={"amount": positions[a.instrument]})
        actions.append(a)
    return Checked(decision, to_stocks, transfer, deferred, actions, notes)
