"""Investment committee: policy, allocation, the analysts' facts, the director's checks and a
meeting end to end. Offline: market data, the equity routine and the models are mocked.
"""
from datetime import date
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from tradingagents.committee import analysts, director, meeting
from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import FixedIncome, Listed, Wealth
from tradingagents.dividend_portfolio.orders import plan_orders
from tradingagents.portfolio import PortfolioContext


def _policy(**kw):
    return InvestmentPolicy(**{"monthly_expenses": 10_000, "monthly_contribution": 2000, **kw})


def _wealth():
    return Wealth(fixed_income=[
        FixedIncome(name="Tesouro Selic", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC", value=80_000,
                    liquidity_days=1),
        FixedIncome(name="CDB diário", kind="cdb", issuer="Banco A", value=20_000, liquidity_days=0, fgc=True),
        FixedIncome(name="IPCA+ 2050", kind="tesouro_ipca", issuer="Tesouro", indexer="IPCA", value=30_000,
                    maturity=date(2050, 8, 15), liquidity_days=1),
        FixedIncome(name="Debênture", kind="debenture", issuer="Infra SA", indexer="IPCA", value=10_000,
                    maturity=date(2027, 3, 1), tax_exempt=True)],
        listed=[Listed(code="AAAA3", quantity=100), Listed(code="FUND11", quantity=10, kind="fii", value=40_000)])


@pytest.mark.unit
def test_the_policy_keeps_its_targets_in_order():
    assert _policy(reserve_months=6).reserve == 60_000
    with pytest.raises(ValueError):
        _policy(stock_target=0.7, stock_target_max=0.6)


@pytest.mark.unit
def test_the_allocation_puts_new_money_in_stocks_and_paces_the_rest():
    alloc = analysts.allocation(_policy(), _wealth(), {"AAAA3": 10_000}, 2000)
    # 190,000 in all, 60,000 reserve: 130,000 investable; 40% of 132,000 is 52,800 in stocks.
    assert alloc["investable"] == 130_000 and alloc["rule_contribution_to_stocks"] == 2000
    assert alloc["stock_gap_after_contribution"] == pytest.approx(52_800 - 12_000)
    assert alloc["rule_monthly_transfer"] == pytest.approx(40_800 / 12)
    assert alloc["listed_funds_above_ceiling"]            # 40,000 is 31% of the investable wealth


@pytest.mark.unit
def test_no_transfer_draws_on_the_reserve():
    alloc = analysts.allocation(_policy(monthly_expenses=21_000, transition_months=1), _wealth(),
                                {"AAAA3": 10_000}, 0)
    # A 15,600 shortfall in one month, but the reserve (126,000) takes all the daily-liquid money
    # (130,000) except 4,000.
    assert alloc["stock_gap_after_contribution"] == pytest.approx(15_600)
    assert alloc["rule_monthly_transfer"] == pytest.approx(4000)


@pytest.mark.unit
def test_fixed_income_facts_flag_the_reserve_the_guarantee_and_maturities():
    facts, warnings = analysts.fixed_income_facts(_policy(monthly_expenses=30_000), _wealth(), date(2026, 10, 30))
    assert facts["daily_liquidity"] == 130_000 and facts["liquid_beyond_reserve"] == -50_000
    assert any("Reserva de emergência descoberta" in w for w in warnings)
    assert facts["without_fgc_guarantee"] == ["Debênture"]
    assert [m["name"] for m in facts["maturing_within_12_months"]] == ["Debênture"]
    assert [m["name"] for m in facts["long_dated_marked_to_market"]] == ["IPCA+ 2050"]


@pytest.mark.unit
def test_stress_reports_the_worst_fall_of_the_mix():
    idx = pd.period_range("2020-01", periods=24, freq="M")
    returns = pd.DataFrame({"stocks_index": [-0.5] + [0.0] * 23, "stocks_dividend": 0.0, "listed_funds": 0.0,
                            "cdi": 0.01}, index=idx)
    s = analysts.stress(returns, 0.4, 0.0, "stocks_index")
    assert s["worst_drawdown"] == pytest.approx(0.4 * -0.5 + 0.6 * 0.01)
    assert s["worst_drawdown_month"] == "2020-01" and np.isfinite(s["annual_return"])


def _decision(**kw):
    return director.DirectorDecision(**{"letter": "Carta.", "contribution_to_stocks": 2000,
                                        "monthly_transfer_to_stocks": 1000, **kw})


@pytest.mark.unit
def test_the_policy_check_brings_the_director_back_within_bounds():
    alloc = {"contribution": 2000, "rule_monthly_transfer": 1000}
    decision = _decision(contribution_to_stocks=5000, monthly_transfer_to_stocks=9000,
                         deferred_buys=[director.Deferral(code="NEWA3", reason="a"),
                                        director.Deferral(code="HELD3", reason="b"),
                                        director.Deferral(code="NEWB3", reason="c"),
                                        director.Deferral(code="NEWC3", reason="d")],
                         fixed_income_actions=[director.FixedIncomeAction(action="resgatar", instrument="CDB diário",
                                                                          amount=50_000, reason="r")])
    checked = director.check(decision, alloc, liquid_beyond_reserve=1500, new_buys={"NEWA3", "NEWB3", "NEWC3"},
                             positions={"CDB diário": 20_000})
    assert checked.contribution_to_stocks == 2000 and checked.transfer == 1500
    assert [d.code for d in checked.deferred] == ["NEWA3", "NEWB3"]
    assert checked.fixed_income_actions[0].amount == 20_000
    assert len(checked.adjustments) == 5     # contribution, transfer, HELD3, NEWC3, the redemption


@pytest.mark.unit
def test_the_director_reads_the_policy_the_reports_and_the_rule_orders():
    seen = {}

    class LLM:
        def with_structured_output(self, schema):
            assert schema is director.DirectorDecision
            return SimpleNamespace(invoke=lambda prompt: seen.setdefault("prompt", prompt) and _decision())

    report = analysts.Report("Analista de Risco", {"worst": -0.2}, analysts.View(summary="s", recommendations=["r"],
                                                                                 risks=["x"]))
    director.ask(LLM(), _policy(), {"contribution": 2000}, [report], [{"side": "compra", "code": "AAAA3"}])
    assert '"monthly_expenses": 10000.0' in seen["prompt"] and '"worst": -0.2' in seen["prompt"]
    assert '"code": "AAAA3"' in seen["prompt"] and "at most 2 newly selected" in seen["prompt"]


@pytest.mark.unit
def test_a_meeting_without_models_follows_the_rule_and_keeps_its_record(tmp_path):
    days = pd.bdate_range("2026-01-02", "2026-10-30")
    frame = pd.DataFrame({"Close": 100.0, "Adj Close": 100.0, "Volume": 1e6, "Dividends": 0.0}, index=days)
    target = pd.DataFrame([{"code": "AAAA3", "name": "A", "sector": "Bancos", "dy24": 0.09},
                           {"code": "NEWA3", "name": "N", "sector": "Energia", "dy24": 0.08}])
    book = PortfolioContext(cash=0.0, positions=[])
    eq = SimpleNamespace(target=["AAAA3", "NEWA3"], target_frame=target, plan=plan_orders(book, [], {}),
                         rebalance=True, day=date(2026, 10, 30), alerts={}, cut={}, holdings=[], performance=None,
                         spent_usd=0.0)
    returns = pd.DataFrame({"stocks_index": 0.01, "stocks_dividend": 0.01, "listed_funds": 0.0, "cdi": 0.008},
                           index=pd.period_range("2020-01", periods=40, freq="M"))
    with mock.patch.object(meeting.b3_market, "companies", return_value={}), \
            mock.patch.object(meeting.b3_market, "histories", side_effect=lambda codes: dict.fromkeys(codes, frame)), \
            mock.patch.object(meeting.routine, "review", return_value=eq), \
            mock.patch.object(analysts, "class_returns", return_value=returns), \
            mock.patch.object(analysts, "route_to_vendor", return_value="Selic 15%"):
        m = meeting.hold(_policy(), _wealth(), as_of=date(2026, 10, 30), use_llm=False,
                         config={**meeting.DEFAULT_CONFIG, "results_dir": str(tmp_path)})
    assert "Reunião sem LLM" in m.letter and "## Relatórios dos analistas" in m.letter
    assert m.checked.contribution_to_stocks == 2000
    # AAAA3 already holds R$ 10,000, above its equal share: the new money goes to NEWA3.
    orders = {o.split("|")[2].strip() for o in m.letter.splitlines() if o.startswith("| compra")}
    assert orders == {"NEWA3"}
    assert (tmp_path / "comite" / "decisoes.jsonl").exists() and (tmp_path / "comite" / "sombra_diretor.json").exists()
