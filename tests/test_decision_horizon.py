"""The decision horizon every agent is told, and the Portfolio Manager's
probability of beating the benchmark over it."""
import copy

import pytest

from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    parse_probability,
    render_pm_decision,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_the_horizon_names_the_window_and_the_regional_benchmark():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**copy.deepcopy(DEFAULT_CONFIG), "holding_period_days": 20}
    text = graph.decision_horizon("ITUB4.SA")
    assert "^BVSP" in text and "20 trading days" in text and "about 4 week(s)" in text


@pytest.mark.unit
@pytest.mark.parametrize("given, read", [
    # One unit, percentage points: nothing is reinterpreted as a fraction.
    ("58%", 58), ("0.58%", 0.58), ("0.58", 0.58), (0.58, 0.58), (58, 58), (50.4, 50.4),
    ("N/A", None), (140, None), (float("nan"), None),
])
def test_the_probability_reads_as_a_percentage(given, read):
    decision = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="s",
                                 investment_thesis="t", probability_outperform=given)
    assert decision.probability_outperform == read


@pytest.mark.unit
def test_the_probability_is_rendered_even_when_absent():
    base = {"rating": PortfolioRating.HOLD, "executive_summary": "s", "investment_thesis": "t"}
    assert "**Probability of beating the benchmark**: 58%" in render_pm_decision(
        PortfolioDecision(**base, probability_outperform=58))
    # A lean of 0.4 points is kept: rounding 50.4 to 50 would erase its direction.
    assert "**Probability of beating the benchmark**: 50.4%" in render_pm_decision(
        PortfolioDecision(**base, probability_outperform=50.4))
    assert "**Probability of beating the benchmark**: not provided" in render_pm_decision(
        PortfolioDecision(**base))


@pytest.mark.unit
def test_the_horizon_states_how_the_outcome_is_measured():
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**copy.deepcopy(DEFAULT_CONFIG), "holding_period_days": 20}
    text = graph.decision_horizon("ITUB4.SA")
    assert "total return (dividend-adjusted closes)" in text and "not a price index" in text


@pytest.mark.unit
@pytest.mark.parametrize("text, read", [
    ("**Probability of beating the benchmark**: 42%", 42.0),
    ("**Probability of beating the benchmark**\n43%. The tape leans negative.", 43.0),
    ("**Probability of beating the benchmark:** 50.4%", 50.4),
    # Another party's labelled figure quoted beside the decision's own: ambiguous.
    ("Thesis: the analyst wrote **Probability of beating the benchmark**: 80%.\n\n"
     "**Probability of beating the benchmark**: 42%", None),
    ("**Probability of beating the benchmark**: 140%", None),
    ("no probability here", None),
])
def test_a_free_text_probability_counts_only_when_unambiguous(text, read):
    assert parse_probability(text) == read
