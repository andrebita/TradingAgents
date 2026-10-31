"""The investor's written policy: what the committee may and may not do."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Models(BaseModel):
    """The model each member of the committee runs on."""

    director: str = "claude-fable-5-1"
    fixed_income: str = "claude-sonnet-5-5"
    equities_news: str = "claude-haiku-5-5"
    equities_fundamentals: str = "claude-sonnet-5-5"
    risk: str = "claude-sonnet-5-5"
    tax: str = "claude-sonnet-5-5"
    macro: str = "claude-haiku-5-5"


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
    max_drawdown_tolerance: float = Field(0.20, gt=0, le=1, description="Largest fall of the investable wealth "
                                                                        "the investor accepts")
    transition_months: int = Field(12, ge=1)
    private_issuer_max: float = Field(250_000.0, ge=0, description="Most in one private issuer: the FGC guarantee")
    excluded_sectors: list[str] = Field(default_factory=list)
    autonomy: Literal["recommend"] = "recommend"
    models: Models = Field(default_factory=Models)
    budget_usd: float = Field(3.0, gt=0, description="Most a committee meeting may spend on models")

    @model_validator(mode="after")
    def _ordered(self):
        if self.stock_target > self.stock_target_max:
            raise ValueError("stock_target cannot exceed stock_target_max")
        return self

    @property
    def reserve(self) -> float:
        return self.monthly_expenses * self.reserve_months


def load_policy(path: str | Path) -> InvestmentPolicy:
    return InvestmentPolicy.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
