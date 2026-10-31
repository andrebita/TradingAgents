"""How much of the investor's wealth goes to stocks: a fixed target share, kept with new money.

No timing: switching between stocks and CDI on trend or on valuation both lost
to holding (README, "Ações ou CDI pelo valuation"). The investor picks a target
share of stocks; each contribution goes first to the class below its share, and
a shortfall larger than the band is closed in equal monthly transfers from
fixed income over ``transition_months``, so a book starting in fixed income
enters gradually. A surplus of stocks is closed by sending new money to fixed
income rather than by selling.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AllocationPlan:
    stocks: float                 # stocks held plus cash in the brokerage, R$
    fixed_income: float
    target_share: float
    current_share: float
    contribution_to_stocks: float
    contribution_to_fixed_income: float
    monthly_transfer: float       # from fixed income to stocks this month (negative: none, surplus noted)
    gap: float                    # stocks to add (+) or in surplus (-) after this month's contribution
    status: str = "ok"            # "shortfall", "surplus" (beyond the band) or "ok"
    transition_months: int = 12

    @property
    def to_stocks_this_month(self) -> float:
        return self.contribution_to_stocks + max(self.monthly_transfer, 0.0)


def plan_allocation(stocks: float, fixed_income: float, contribution: float, target_share: float, *,
                    band: float = 0.05, transition_months: int = 12) -> AllocationPlan:
    """Where this month's contribution goes and how much moves from fixed income to stocks."""
    if not 0 <= target_share <= 1:
        raise ValueError("the target share of stocks must be between 0 and 100%")
    total = stocks + fixed_income + contribution
    desired = target_share * total
    to_stocks = min(max(desired - stocks, 0.0), contribution)
    to_fixed = contribution - to_stocks
    gap = desired - (stocks + to_stocks)
    plan = AllocationPlan(stocks, fixed_income, target_share, stocks / (stocks + fixed_income or 1.0),
                          to_stocks, to_fixed, 0.0, gap, transition_months=transition_months)
    if gap > band * total:
        plan.status = "shortfall"
        plan.monthly_transfer = min(gap / max(transition_months, 1), fixed_income)
    elif gap < -band * total:
        plan.status = "surplus"
    return plan
