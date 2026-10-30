"""The decision horizon every agent is told, and the Portfolio Manager's
probability of beating the benchmark over it."""
import copy

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_the_horizon_names_the_window_and_the_regional_benchmark():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**copy.deepcopy(DEFAULT_CONFIG), "holding_period_days": 20}
    text = graph.decision_horizon("ITUB4.SA")
    assert "^BVSP" in text and "20 trading days" in text and "about 4 week(s)" in text


@pytest.mark.unit
@pytest.mark.parametrize("given, read", [("58%", 58), (0.58, 58), (58, 58), ("N/A", None), (140, None)])
def test_the_probability_reads_as_a_percentage(given, read):
    decision = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="s",
                                 investment_thesis="t", probability_outperform=given)
    assert decision.probability_outperform == read


@pytest.mark.unit
def test_the_probability_is_rendered_even_when_absent():
    base = {"rating": PortfolioRating.HOLD, "executive_summary": "s", "investment_thesis": "t"}
    assert "**Probability of beating the benchmark**: 58%" in render_pm_decision(
        PortfolioDecision(**base, probability_outperform=58))
    assert "**Probability of beating the benchmark**: not provided" in render_pm_decision(
        PortfolioDecision(**base))
