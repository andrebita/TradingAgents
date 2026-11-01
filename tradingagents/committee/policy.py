"""The investor's written policy: what the committee may and may not do."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

FixedIncomeKind = Literal["tesouro_selic", "tesouro_ipca", "tesouro_pre", "cdb", "lci", "lca"]
FIXED_INCOME_KINDS = ("tesouro_selic", "tesouro_ipca", "tesouro_pre", "cdb", "lci", "lca")


class Models(BaseModel):
    """The model each member of the committee runs on."""

    director: str = "claude-fable-5-1"
    fixed_income: str = "claude-sonnet-5-5"
    equities_news: str = "claude-haiku-5-5"
    equities_fundamentals: str = "claude-sonnet-5-5"
    risk: str = "claude-sonnet-5-5"
    tax: str = "claude-sonnet-5-5"
    macro: str = "claude-haiku-5-5"


class IndexPlan(BaseModel):
    """The portfolio by index funds (review 5): sleeves of the investable wealth and their vehicles.

    The selection contest found no stock-picking rule that beat the index funds, so the Brazil sleeve holds an
    index ETF chosen by the director from ``brazil_menu`` (``brazil_default`` without one), the international
    sleeve a global ETF, the rest Tesouro Selic. Holdings outside the plan (single stocks, listed funds, bonds
    maturing after the horizon) are migrated out at the pace below.
    """

    brazil: float = Field(0.20, ge=0, le=1, description="Brazilian equities, through an index ETF")
    international: float = Field(0.40, ge=0, le=1, description="Global equities, through a B3-listed ETF")
    fixed_income: float = Field(0.40, ge=0, le=1, description="Tesouro Selic")
    brazil_menu: list[str] = Field(default_factory=lambda: ["BOVA11", "DIVO11"])
    brazil_default: str = "BOVA11"
    international_vehicle: str = "WRLD11"
    horizon_end: date | None = Field(None, description="When the money may be needed: bonds maturing later are "
                                                       "moved to Tesouro Selic; earlier ones are held to maturity")
    migration_start: date | None = Field(None, description="When the investor adopted the plan; paces the exits")
    listed_funds_exit_months: int = Field(12, ge=1, description="Months to sell the listed funds down to zero")
    stock_sales_cap: float = Field(19_000.0, gt=0, description="Most single-stock sales a month, within the "
                                                               "R$ 20,000 exemption")
    crisis_alert: float = Field(0.15, gt=0, le=1, description="Fall of an equity ETF from its 12-month high that "
                                                              "puts the crisis plan on the table")
    crisis_plan: float = Field(0.30, gt=0, le=1, description="Fall that calls an extraordinary meeting")

    @model_validator(mode="after")
    def _sums(self):
        if abs(self.brazil + self.international + self.fixed_income - 1) > 1e-6:
            raise ValueError("the index plan's sleeves must add up to 100%")
        if self.brazil_default not in self.brazil_menu:
            raise ValueError("brazil_default must be in brazil_menu")
        return self


class InvestmentPolicy(BaseModel):
    objective: str = "Crescimento do patrimônio no longo prazo (5 anos ou mais)"
    monthly_expenses: float = Field(gt=0, description="Monthly expenses, R$; sizes the emergency reserve")
    reserve_months: int = Field(6, ge=0)
    monthly_contribution: float = Field(0.0, ge=0)
    # Shares of the investable wealth: everything outside the emergency reserve.
    stock_target: float = Field(0.40, ge=0, le=1, description="Target share in stocks now")
    stock_target_max: float = Field(0.60, ge=0, le=1, description="Ceiling the target may climb to")
    stock_step_per_year: float = Field(0.10, ge=0, le=1, description="Most the target may rise at a yearly review")
    listed_funds_max: float = Field(0.15, ge=0, le=1, description="Ceiling for listed real-estate, agro and "
                                                                  "infrastructure funds")
    band: float = Field(0.05, ge=0, le=1, description="Drift tolerated before moving money between classes")
    max_drawdown_tolerance: float | None = Field(None, gt=0, le=1, description="Largest fall of the investable "
                                                 "wealth the investor accepts; the committee asks until it is set")
    transition_months: int = Field(12, ge=1)
    private_issuer_max: float = Field(250_000.0, ge=0, description="Most in one private issuer: the FGC guarantee")
    fixed_income_kinds: list[FixedIncomeKind] = Field(
        default_factory=lambda: list(FIXED_INCOME_KINDS),
        description="The kinds new fixed-income money may go to, e.g. only ['tesouro_selic']")
    excluded_sectors: list[str] = Field(default_factory=list)
    autonomy: Literal["recommend"] = "recommend"
    models: Models = Field(default_factory=Models)
    budget_usd: float = Field(3.0, gt=0, description="Most a committee meeting may spend on models")
    index_plan: IndexPlan | None = Field(None, description="The portfolio by index funds; without it, the "
                                                           "dividend rule of the earlier versions")

    @model_validator(mode="after")
    def _ordered(self):
        if self.stock_target > self.stock_target_max:
            raise ValueError("stock_target cannot exceed stock_target_max")
        return self

    @property
    def reserve(self) -> float:
        return self.monthly_expenses * self.reserve_months

    def for_models(self) -> str:
        """The policy as the committee's models read it: without the model names, and, under the index plan,
        without the dividend rule's fields (stock target, listed-funds ceiling, sectors), which the plan does not
        use and which led an analyst to argue for the old targets."""
        exclude = {"models"} | (LEGACY_FIELDS if self.index_plan else {"index_plan"})
        return self.model_dump_json(indent=1, exclude=exclude)


LEGACY_FIELDS = {"stock_target", "stock_target_max", "stock_step_per_year", "listed_funds_max", "excluded_sectors"}


def load_policy(path: str | Path) -> InvestmentPolicy:
    return InvestmentPolicy.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
