"""The investor's whole wealth: fixed income positions and listed assets, as of a date."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tradingagents.portfolio import PortfolioContext, Position

FixedIncomeKind = Literal["tesouro_selic", "tesouro_ipca", "tesouro_pre", "cdb", "lci", "lca", "lig", "debenture",
                          "cri", "cra", "fund", "other"]
ListedKind = Literal["acao", "fii", "fiagro", "fi_infra", "etf", "bdr"]
AssetClass = Literal["acoes", "internacional", "fundos_listados", "renda_fixa"]
CLASS_OF_KIND = {"acao": "acoes", "fii": "fundos_listados", "fiagro": "fundos_listados", "fi_infra": "fundos_listados",
                 "bdr": "internacional"}      # an ETF says which: stocks, international or fixed income


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
    applied_value: float | None = Field(None, ge=0, description="Amount invested, R$: the gain is taxed on a sale")

    @property
    def sovereign(self) -> bool:
        return self.kind.startswith("tesouro")

    @property
    def reserve_eligible(self) -> bool:
        """Cash within a day with no risk of a loss: post-fixed (Selic, CDI) with daily liquidity.

        An inflation-linked or fixed-rate bond sells within a day too, but at the market
        price, which can be below what was paid: it is not emergency money.
        """
        return self.liquidity_days is not None and self.liquidity_days <= 1 and self.indexer in ("SELIC", "CDI")


class Listed(BaseModel):
    code: str
    quantity: float = Field(ge=0)
    kind: ListedKind = "acao"
    average_price: float | None = None
    value: float | None = Field(None, description="Market value, when not priced from the market")
    lent: bool = Field(False, description="Lent out (aluguel): still owned, held by the borrower")
    asset_class: AssetClass | None = Field(None, description="The class it counts in; required for an ETF "
                                                             "(acoes, internacional or renda_fixa)")

    @property
    def counts_as(self) -> str | None:
        return self.asset_class or CLASS_OF_KIND.get(self.kind)


@dataclass
class Valuation:
    """What the listed positions are worth, per code and per class, and which could not be valued."""

    by_code: dict[str, float] = field(default_factory=dict)
    by_class: dict[str, float] = field(default_factory=lambda: dict.fromkeys(
        ("acoes", "internacional", "fundos_listados", "renda_fixa"), 0.0))
    sources: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    classes: dict[str, str] = field(default_factory=dict)

    def of_class(self, cls: str) -> dict[str, float]:
        return {c: v for c, v in self.by_code.items() if self.classes.get(c) == cls}


class TaxState(BaseModel):
    """What the tax on stock sales depends on beyond this month's plan, from the investor's records."""
    month: str | None = Field(None, description="AAAA-MM the month's figures refer to; in another month they are zero")
    stock_sales_this_month: float = Field(0.0, ge=0, description="Stock sales already made this month, R$")
    stock_gain_this_month: float = Field(0.0, description="Their net gain, R$; negative for a loss")
    stock_loss_carryforward: float = Field(0.0, ge=0, description="Stock losses of earlier months still to offset")

    def for_month(self, day: date) -> tuple[float, float, float]:
        """(sales so far, gain so far, loss carried) for the month of ``day``."""
        current = self.month == f"{day:%Y-%m}"
        return (self.stock_sales_this_month if current else 0.0, self.stock_gain_this_month if current else 0.0,
                self.stock_loss_carryforward)


class Wealth(BaseModel):
    as_of: date | None = None
    cash: float = 0.0
    fixed_income: list[FixedIncome] = Field(default_factory=list)
    listed: list[Listed] = Field(default_factory=list)
    tax: TaxState = Field(default_factory=TaxState)
    notes: list[str] = Field(default_factory=list)

    def reserve_eligible(self) -> float:
        """Cash plus the post-fixed positions with daily liquidity: what the emergency reserve can count."""
        return self.cash + sum(x.value for x in self.fixed_income if x.reserve_eligible)

    def transfer_sources(self, amount: float) -> list[tuple[str, float]]:
        """Where ``amount`` leaves fixed income from: cash, then the reserve-eligible positions, taxable
        before exempt (an exempt yield is worth more net), larger first. ``("Caixa", x)`` is cash."""
        out, left = [], amount
        if left > 0 and self.cash > 0:
            out.append(("Caixa", min(self.cash, left)))
            left -= out[-1][1]
        for x in sorted((x for x in self.fixed_income if x.reserve_eligible), key=lambda x: (x.tax_exempt, -x.value)):
            if left <= 0.005:
                break
            out.append((x.name, min(x.value, left)))
            left -= out[-1][1]
        return [(name, round(v, 2)) for name, v in out if v > 0]

    def valuation(self, market: dict[str, float]) -> Valuation:
        """The listed positions at ``market`` (code: R$ for the whole holding, from a recent price), else at the
        value the investor gave; one with neither, or an ETF without its class, is missing, never zero."""
        out = Valuation()
        lots: dict[str, list[Listed]] = {}
        for x in self.listed:
            lots.setdefault(x.code.strip().upper(), []).append(x)
        for code, xs in lots.items():
            cls = xs[0].counts_as
            if cls is None:
                out.missing.append(f"{code} (informe a classe do ETF: acoes, internacional ou renda_fixa)")
                continue
            if code in market:
                value, source = market[code], "cotação"
            elif all(x.value is not None for x in xs):
                value, source = sum(x.value for x in xs), "valor informado"
            else:
                out.missing.append(f"{code} (sem cotação recente nem valor informado)")
                continue
            out.by_code[code] = value
            out.by_class[cls] += value
            out.sources[code] = source
            out.classes[code] = cls
        return out

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
