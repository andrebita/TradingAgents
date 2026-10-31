"""What the LLM reads for the portfolio: the week's filings and press, and the dividend-cut risk.

Both are alerts for the investor, never orders: the backtests found no return in
acting on them (README, "Filtro de armadilhas").
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel, Field, field_validator

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.dividend_portfolio import fundamentals
from tradingagents.llm_clients import create_tier_client

logger = logging.getLogger(__name__)

# US$ per million input and output tokens; an unknown model is priced as the dearer one.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0)}
DEFAULT_PRICE = (2.0, 10.0)


class CostMeter(BaseCallbackHandler):
    """Adds up what each model call costs, from the usage the provider reports."""

    def __init__(self, model: str, budget_usd: float):
        self.price = PRICES.get(model, DEFAULT_PRICE)
        self.budget_usd = budget_usd
        self.spent = 0.0
        self.calls = 0

    def on_llm_end(self, response, **kwargs):
        for generation in (g for batch in response.generations for g in batch):
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
            self.spent += (usage.get("input_tokens", 0) * self.price[0]
                           + usage.get("output_tokens", 0) * self.price[1]) / 1e6
        self.calls += 1

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.budget_usd


class NewsAlert(BaseModel):
    """The week's news on one holding, for its owner."""

    level: str = Field(description="One of: normal, atenção, alerta. 'alerta' only for news that threatens the "
                                   "dividend or the company's solvency or governance.")
    summary: str = Field(description="Two or three sentences in Portuguese: what happened and why it matters to a "
                                     "long-term holder who buys for dividends.")
    dividend_news: bool = Field(description="Whether the news is about dividends, interest on capital or payout.")

    @field_validator("level")
    @classmethod
    def _level(cls, v):
        v = v.strip().lower().replace("atencao", "atenção")
        if v not in ("normal", "atenção", "alerta"):
            raise ValueError("level must be normal, atenção or alerta")
        return v


class CutRisk(BaseModel):
    """The dividend-sustainability read of one anonymized company."""

    key_points: list[str] = Field(description="Three to five facts from the statements that drive the estimate.")
    probability_cut: float = Field(
        description="Probability in percent (0-100) that dividends per share over the next 12 months will be at "
                    "least 30% lower than over the last 12 months.")

    @field_validator("probability_cut")
    @classmethod
    def _in_range(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("probability must be between 0 and 100 percent")
        return v


NEWS_PROMPT = """You watch a long-term dividend portfolio of Brazilian stocks for its owner. Below is everything filed with the securities regulator (CVM) and published by the Brazilian financial press about {code} from {start} to {end}.

Classify the week for the holder and summarize it in Portuguese. Routine items (meeting minutes, monthly insider-holdings reports, conference notices) are 'normal'. Use 'atenção' for results, guidance changes, large investments, debt raises, regulatory news; 'alerta' only for what threatens the dividend, solvency or governance (dividend cut or suspension, losses, covenant breach, restructuring, fraud or investigation, controller change).

{news}"""

CUT_PROMPT = """You are a credit and dividend analyst. The company below is anonymized: no name, no dates, figures as percentages. Judge only from what is shown; do not try to identify the company.

How likely is it that its dividends per share over the next 12 months will be at least 30% lower than over the last 12 months? Weigh earnings trend and quality, payout against earnings and cash generation, leverage and liquidity, and any one-off that inflated the recent payout. Most companies that paid steadily keep paying; reserve high probabilities for clear warning signs.

{package}"""


@dataclass
class Assistant:
    """The two reads, each on its own model and budget."""

    config: dict
    news_model: str = "claude-haiku-5-5"
    cut_model: str = "claude-sonnet-5-5"
    budget_usd: float = 1.0

    def __post_init__(self):
        # Cloud sessions drop ANTHROPIC_* variables; the key may come under another name.
        if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]
        self.meter = CostMeter(self.news_model, self.budget_usd)
        self.cut_meter = CostMeter(self.cut_model, self.budget_usd)
        quick = {**self.config, "llm_provider": "anthropic", "quick_think_llm": self.news_model}
        deep = {**self.config, "llm_provider": "anthropic", "deep_think_llm": self.cut_model,
                "anthropic_effort": "low"}
        self._news = create_tier_client(quick, "quick", callbacks=[self.meter]).get_llm() \
            .with_structured_output(NewsAlert)
        self._cut = create_tier_client(deep, "deep", callbacks=[self.cut_meter]).get_llm() \
            .with_structured_output(CutRisk)

    @property
    def spent(self) -> float:
        return self.meter.spent + self.cut_meter.spent

    def news(self, code: str, end: date, days: int = 7) -> NewsAlert | str | None:
        """The week's alert for ``code``; None when nothing was filed or published, a string on failure."""
        start = end - timedelta(days=days)
        try:
            text = route_to_vendor("get_news", f"{code}.SA", start.isoformat(), end.isoformat())
        except VendorError as exc:
            return f"notícias indisponíveis: {exc}"
        if "No filing by this company in the window" in text and "None of these sites" in text:
            return None
        if self.spent >= self.budget_usd:
            return "orçamento de LLM da rotina esgotado"
        try:
            return self._news.invoke(NEWS_PROMPT.format(code=code, start=start, end=end, news=text))
        except Exception as exc:  # a refusal or an invalid answer is reported, not fatal
            logger.warning("News read failed for %s: %s", code, exc)
            return f"leitura falhou: {type(exc).__name__}"

    def cut_risk(self, cnpj: str, sector: str, frame: pd.DataFrame, as_of: date) -> CutRisk | str:
        """The anonymized-statement estimate of a dividend cut for one company."""
        docs = fundamentals.statements(cnpj, as_of)
        if not docs:
            return "sem demonstrativos na CVM"
        div = frame["Dividends"].loc[:pd.Timestamp(as_of)]
        t = pd.Timestamp(as_of)
        years = [float(div[(div.index > t - pd.DateOffset(months=12 * (k + 1)))
                           & (div.index <= t - pd.DateOffset(months=12 * k))].sum()) for k in range(4)][::-1]
        close = float(frame["Close"].loc[:t].iloc[-1])
        package = fundamentals.package(sector, docs, [100 * y / years[-1] if years[-1] else 0 for y in years],
                                       years[-1] / close, (years[-1] + years[-2]) / 2 / close)
        if self.spent >= self.budget_usd:
            return "orçamento de LLM da rotina esgotado"
        try:
            return self._cut.invoke(CUT_PROMPT.format(package=package))
        except Exception as exc:
            logger.warning("Cut read failed for %s: %s", cnpj, exc)
            return f"leitura falhou: {type(exc).__name__}"
