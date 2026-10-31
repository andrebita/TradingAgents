"""The investor's whole wealth: fixed income positions and listed assets, as of a date."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tradingagents.portfolio import PortfolioContext, Position

FixedIncomeKind = Literal["tesouro_selic", "tesouro_ipca", "tesouro_pre", "cdb", "lci", "lca", "lig", "debenture",
                          "cri", "cra", "fund", "other"]
ListedKind = Literal["acao", "fii", "fiagro", "fi_infra", "etf", "bdr"]


class FixedIncome(BaseModel):
    name: str
    kind: FixedIncomeKind
    issuer: str = Field("", description="'Tesouro' for government bonds")
    indexer: Literal["SELIC", "CDI", "IPCA", "PRE", "OTHER"] = "CDI"
    maturity: date | None = None
    value: float = Field(ge=0)
    liquidity_days: int | None = Field(None, description="Days to cash; None: only at maturity")
    tax_exempt: bool = False
    fgc: bool = Field(False, description="Covered by the FGC deposit guarantee")
    applied_on: date | None = Field(None, description="Purchase date, for the regressive income-tax table")

    @property
    def sovereign(self) -> bool:
        return self.kind.startswith("tesouro")


class Listed(BaseModel):
    code: str
    quantity: float = Field(ge=0)
    kind: ListedKind = "acao"
    average_price: float | None = None
    value: float | None = Field(None, description="Market value, when not priced from the market")
    lent: bool = Field(False, description="Lent out (aluguel): still owned, held by the borrower")


class Wealth(BaseModel):
    as_of: date | None = None
    cash: float = 0.0
    fixed_income: list[FixedIncome] = Field(default_factory=list)
    listed: list[Listed] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def stocks(self) -> list[Listed]:
        return [x for x in self.listed if x.kind == "acao"]

    def listed_funds(self) -> list[Listed]:
        return [x for x in self.listed if x.kind in ("fii", "fiagro", "fi_infra")]

    def stock_book(self) -> PortfolioContext:
        """The stocks as the equity routine's book; shares of one code across brokers add up."""
        return PortfolioContext(cash=0.0, currency="BRL", positions=[
            Position(ticker=x.code, quantity=x.quantity, average_price=x.average_price) for x in self.stocks()])


def load_wealth(path: str | Path) -> Wealth:
    return Wealth.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
