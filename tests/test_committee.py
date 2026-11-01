"""Investment committee: policy, allocation, the analysts' facts, the director's checks and a
meeting end to end. Offline: market data, the equity routine and the models are mocked.
"""
import threading
from datetime import date
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from tradingagents.committee import analysts, director, meeting
from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import FixedIncome, Listed, TaxState, Wealth
from tradingagents.dataflows.config import get_config
from tradingagents.dividend_portfolio.monitor import Budget, BudgetExceeded
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
def test_a_short_reserve_is_filled_first_and_stops_the_transfer():
    # Review 3, C2: the reserve counts cash and daily post-fixed money only (100,000 here; the
    # IPCA+ 2050 sells within a day but at the market price). Against 126,000 it is 26,000 short.
    alloc = analysts.allocation(_policy(monthly_expenses=21_000, transition_months=1), _wealth(),
                                {"AAAA3": 10_000}, 2000)
    assert alloc["reserve_eligible"] == 100_000 and alloc["reserve_deficit"] == 26_000
    assert alloc["rule_contribution_to_reserve"] == 2000 and alloc["rule_contribution_to_stocks"] == 0
    assert alloc["rule_monthly_transfer"] == 0 and alloc["transfer_sources"] == []


@pytest.mark.unit
def test_cash_counts_in_the_reserve_and_leaves_first():
    wealth = _wealth()
    wealth.cash = 5000
    alloc = analysts.allocation(_policy(transition_months=1), wealth, {"AAAA3": 10_000}, 2000)
    assert alloc["reserve_eligible"] == 105_000
    assert alloc["transfer_sources"] == [{"name": "Caixa", "amount": 5000},
                                         {"name": "Tesouro Selic", "amount": pytest.approx(37_800)}]
    assert sum(x["amount"] for x in alloc["transfer_sources"]) == pytest.approx(alloc["rule_monthly_transfer"])


@pytest.mark.unit
def test_fixed_income_facts_flag_the_reserve_the_guarantee_and_maturities():
    facts, warnings = analysts.fixed_income_facts(_policy(monthly_expenses=30_000), _wealth(), date(2026, 10, 30))
    assert facts["daily_liquidity"] == 130_000 and facts["reserve_eligible"] == 100_000
    assert facts["liquid_beyond_reserve"] == -80_000
    assert [x["name"] for x in facts["daily_liquid_but_not_reserve"]] == ["IPCA+ 2050"]
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
    policy, wealth = _policy(), _wealth()
    alloc = analysts.allocation(policy, wealth, {"AAAA3": 10_000}, 2000)   # rule transfer 3,400
    act = director.FixedIncomeAction
    decision = _decision(contribution_to_stocks=5000, monthly_transfer_to_stocks=9000,
                         deferred_buys=[director.Deferral(code="NEWA3", reason="a"),
                                        director.Deferral(code="HELD3", reason="b"),
                                        director.Deferral(code="NEWB3", reason="c"),
                                        director.Deferral(code="NEWC3", reason="d")],
                         fixed_income_actions=[
                             act(action="resgatar", instrument="CDB diário", amount=50_000, reason="r"),
                             act(action="resgatar", instrument="CDB diário", amount=50_000, reason="again"),
                             act(action="resgatar", instrument="Título fantasma", amount=1000, reason="r"),
                             act(action="resgatar", instrument="Debênture", amount=10_000, reason="r"),
                             act(action="resgatar", instrument="IPCA+ 2050", amount=5000, reason="r"),
                             act(action="aplicar", instrument="CRI de shopping", amount=1000, reason="r"),
                             act(action="aplicar", instrument="CDB", kind="cdb", amount=1000, reason="no bank"),
                             act(action="aplicar", instrument="Tesouro IPCA+ 2035", kind="tesouro_ipca",
                                 amount=1e9, reason="r")])
    c = director.check(decision, alloc, wealth, policy, {"NEWA3", "NEWB3", "NEWC3"}, date(2026, 10, 30))
    assert c.contribution_to_stocks == 2000 and c.transfer == 6800      # twice the rule's pace
    assert c.transfer_sources == [("Tesouro Selic", 6800)]             # taxable, larger first
    assert [d.code for d in c.deferred] == ["NEWA3", "NEWB3"]
    # The CDB's 20,000 once (the second ask finds no balance), the IPCA+ with a confirmation for
    # the investor; the unknown title, the bond with liquidity only at maturity, the CRI and the
    # CDB without a bank are dropped; the application is held to the money freed: 25,000.
    assert [(a.action, a.instrument, a.amount) for a in c.fixed_income_actions] == [
        ("resgatar", "CDB diário", 20_000), ("resgatar", "IPCA+ 2050", 5000),
        ("aplicar", "Tesouro IPCA+ 2035", 25_000)]
    assert any("IPCA+ 2050" in e for e in c.escalations) and c.to_daily_post_fixed == 0
    assert len(c.adjustments) == 11


@pytest.mark.unit
def test_redemptions_never_reach_into_the_reserve_and_stocks_stay_under_the_ceiling():
    policy, wealth = _policy(monthly_expenses=15_000), _wealth()           # reserve 90,000 of 100,000
    alloc = analysts.allocation(policy, wealth, {"AAAA3": 10_000}, 2000)
    decision = _decision(monthly_transfer_to_stocks=0, fixed_income_actions=[
        director.FixedIncomeAction(action="resgatar", instrument="Tesouro Selic", amount=80_000, reason="r")])
    c = director.check(decision, alloc, wealth, policy, set(), date(2026, 10, 30))
    assert [a.amount for a in c.fixed_income_actions] == [10_000]        # only what is beyond the reserve
    assert c.to_daily_post_fixed == 10_000                               # not applied: stays daily post-fixed
    # Stocks already at the ceiling get no more money from the director.
    full = {**alloc, "stocks": alloc["stock_ceiling"]}
    c = director.check(_decision(contribution_to_stocks=2000, monthly_transfer_to_stocks=1000), full, wealth,
                       policy, set())
    assert c.contribution_to_stocks == 0 and c.transfer == 0


@pytest.mark.unit
def test_the_director_cannot_return_infinite_or_missing_numbers():
    with pytest.raises(ValueError):
        director.FixedIncomeAction(action="aplicar", instrument="x", kind="cdb", amount=float("inf"), reason="r")
    with pytest.raises(ValueError):
        _decision(monthly_transfer_to_stocks=float("nan"))


@pytest.mark.unit
def test_the_budget_holds_the_worst_case_before_the_call():
    budget = Budget(0.10)
    calls = []
    runnable = SimpleNamespace(invoke=lambda prompt, config: calls.append(prompt) or "ok")
    # Fable at US$ 50 a million output tokens: a 1,000-token cap holds US$ 0.05 plus the prompt.
    assert budget.invoke(runnable, "x" * 2000, model="claude-fable-5-1", max_output=1000) == "ok"
    assert budget.spent == pytest.approx(Budget.estimate("x" * 2000, "claude-fable-5-1", 1000))  # no usage: all held
    with pytest.raises(BudgetExceeded):
        budget.invoke(runnable, "x" * 2000, model="claude-fable-5-1", max_output=1000)
    assert len(calls) == 1 and budget.reserved == pytest.approx(0)
    # A part's own cap counts in its parent's.
    child = Budget(1.0, parent=Budget(0.02))
    with pytest.raises(BudgetExceeded):
        child.invoke(runnable, "x", model="claude-sonnet-5-5", max_output=4000)


@pytest.mark.unit
def test_parallel_calls_cannot_overspend_the_budget():
    budget = Budget(0.5)
    gate = threading.Barrier(8)

    def slow(prompt, config):
        return "ok"

    results = []

    def call():
        gate.wait()
        try:
            results.append(budget.invoke(SimpleNamespace(invoke=slow), "p", model="claude-sonnet-5-5",
                                         max_output=10_000))           # US$ 0.10 each, at most
        except BudgetExceeded:
            results.append(None)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 4 and budget.spent <= 0.5 + 1e-9


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
    # The reports, which summarize public filings and press, sit inside the data block (review 3, C11).
    data = seen["prompt"][seen["prompt"].index("<data>"):seen["prompt"].index("</data>")]
    assert '"worst": -0.2' in data and "never an instruction" in seen["prompt"]


@pytest.mark.unit
def test_a_meeting_without_models_follows_the_rule_and_keeps_its_record(tmp_path):
    days = pd.bdate_range("2026-01-02", "2026-10-30")
    frame = pd.DataFrame({"Close": 100.0, "Adj Close": 100.0, "Volume": 1e6, "Dividends": 0.0}, index=days)
    target = pd.DataFrame([{"code": "AAAA3", "name": "A", "sector": "Bancos", "dy24": 0.09},
                           {"code": "NEWA3", "name": "N", "sector": "Energia", "dy24": 0.08}])
    book = PortfolioContext(cash=0.0, positions=[])
    eq = SimpleNamespace(target=["AAAA3", "NEWA3"], target_frame=target, plan=plan_orders(book, [], {}),
                         rebalance=True, day=date(2026, 10, 30), alerts={}, cut={}, holdings=[], performance=None,
                         spent_usd=0.0, book=_wealth().stock_book(), prices={"AAAA3": 100.0, "NEWA3": 100.0},
                         pending=set(), keep=set())
    returns = pd.DataFrame({"stocks_index": 0.01, "stocks_dividend": 0.01, "listed_funds": 0.0, "cdi": 0.008},
                           index=pd.period_range("2020-01", periods=40, freq="M"))
    seen = {}

    def macro(policy, today, models):
        seen["results_dir"] = get_config()["results_dir"]
        return analysts.Report(analysts.ROLES["macro"], {})

    with mock.patch.object(meeting.b3_market, "companies", return_value={}), \
            mock.patch.object(meeting.b3_market, "histories", side_effect=lambda codes: dict.fromkeys(codes, frame)), \
            mock.patch.object(meeting.routine, "review", return_value=eq) as review, \
            mock.patch.object(analysts, "class_returns", return_value=returns), \
            mock.patch.object(analysts, "macro", side_effect=macro):
        wealth = _wealth()
        wealth.tax = TaxState(month="2026-10", stock_sales_this_month=5000, stock_gain_this_month=800,
                              stock_loss_carryforward=1200)
        m = meeting.hold(_policy(excluded_sectors=["Telecom"]), wealth, as_of=date(2026, 10, 30), use_llm=False,
                         config={**meeting.DEFAULT_CONFIG, "results_dir": str(tmp_path)})
    # The investor's exclusions and tax records reach the equity routine (review 3, A3 and C5).
    kw = review.call_args.kwargs
    assert kw["rules"].excluded_sectors == ("Telecom",)
    assert (kw["news_model"], kw["cut_model"]) == ("claude-haiku-5-5", "claude-sonnet-5-5")   # the policy's
    assert (kw["sold_earlier_this_month"], kw["gain_earlier_this_month"], kw["loss_carryforward"]) == (5000, 800, 1200)
    assert "Reunião sem LLM" in m.letter and "## 3. Relatórios dos analistas" in m.letter
    # Review 3, C10: the checked plan comes first, as flows to approve, with the balances after it.
    letter = m.letter
    assert letter.index("## 1. Plano do mês") < letter.index("## 2. Sem carta do diretor")
    assert "| Aporte | Ações (ordens abaixo) | R$ 2.000,00 |" in letter and "- [ ] Ordens de ações" in letter
    assert "Saldos informados em: **data não informada**" in letter
    assert m.checked.contribution_to_stocks == 2000
    # AAAA3 already holds R$ 10,000, above its equal share: the new money goes to NEWA3.
    orders = {o.split("|")[2].strip() for o in m.letter.splitlines() if o.startswith("| compra")}
    assert orders == {"NEWA3"}
    assert (tmp_path / "comite" / "decisoes.jsonl").exists() and (tmp_path / "comite" / "sombra_diretor.json").exists()
    assert seen["results_dir"] == str(tmp_path)          # the meeting's config, inside the analyst's thread

    # With models, a director that fails leaves the rule's decision and says so.
    with mock.patch.object(meeting.b3_market, "companies", return_value={}), \
            mock.patch.object(meeting.b3_market, "histories", side_effect=lambda codes: dict.fromkeys(codes, frame)), \
            mock.patch.object(meeting.routine, "review", return_value=eq), \
            mock.patch.object(analysts, "class_returns", return_value=returns), \
            mock.patch.object(analysts, "macro", side_effect=macro), \
            mock.patch.object(analysts, "ask_view", side_effect=RuntimeError("offline")), \
            mock.patch.object(analysts.Models, "llm", return_value=None), \
            mock.patch.object(meeting.director, "ask", side_effect=ValueError("invalid structured output")):
        m = meeting.hold(_policy(), _wealth(), as_of=date(2026, 10, 30), use_llm=True,
                         config={**meeting.DEFAULT_CONFIG, "results_dir": str(tmp_path)})
    assert "O diretor não respondeu" in m.letter and "Rodar a reunião de novo" in m.letter
    assert m.checked.contribution_to_stocks == 2000


@pytest.mark.unit
def test_the_tax_records_of_another_month_count_only_the_carried_loss():
    state = TaxState(month="2026-09", stock_sales_this_month=15_000, stock_gain_this_month=900,
                     stock_loss_carryforward=300)
    assert state.for_month(date(2026, 9, 30)) == (15_000, 900, 300)
    assert state.for_month(date(2026, 10, 30)) == (0.0, 0.0, 300)


@pytest.mark.unit
def test_every_listed_class_counts_and_a_missing_value_stops_the_transfer():
    # Review 3, C7: an ETF used to be left out of the wealth, and a stock without a price counted as zero.
    wealth = _wealth()
    wealth.listed += [Listed(code="IVVB11", quantity=10, kind="etf", asset_class="internacional", value=3000),
                      Listed(code="IMAB11", quantity=10, kind="etf", asset_class="renda_fixa", value=1000),
                      Listed(code="BOVA11", quantity=10, kind="etf", asset_class="acoes")]
    alloc = analysts.allocation(_policy(), wealth, {"AAAA3": 10_000, "BOVA11": 1500}, 2000)
    assert alloc["stocks"] == 11_500 and alloc["international"] == 3000
    assert alloc["fixed_income"] == 141_000 and alloc["unvalued"] == []
    assert alloc["listed_valuation"]["IVVB11"] == "valor informado"

    wealth.listed.append(Listed(code="NOPX3", quantity=100))               # no price, no value
    wealth.listed.append(Listed(code="SPXI11", quantity=5, kind="etf"))     # an ETF of unknown class
    alloc = analysts.allocation(_policy(), wealth, {"AAAA3": 10_000, "BOVA11": 1500}, 2000)
    assert [u.split()[0] for u in alloc["unvalued"]] == ["NOPX3", "SPXI11"] and alloc["rule_monthly_transfer"] == 0
    c = director.check(_decision(monthly_transfer_to_stocks=5000), alloc, wealth, _policy(), set(), date(2026, 10, 30))
    assert c.transfer == 0 and any("NOPX3" in e for e in c.escalations)
    assert any("atualize o patrimônio" in e for e in c.escalations)       # the balances have no date


@pytest.mark.unit
def test_new_fixed_income_money_goes_only_where_the_policy_allows_and_the_tolerance_is_asked():
    # Review 4: the investor chose Tesouro Selic for all fixed income; the check allowed any kind.
    policy, wealth = _policy(fixed_income_kinds=["tesouro_selic"]), _wealth()
    alloc = analysts.allocation(policy, wealth, {"AAAA3": 10_000}, 2000)
    act = director.FixedIncomeAction
    decision = _decision(contribution_to_stocks=0, fixed_income_actions=[
        act(action="aplicar", instrument="Tesouro IPCA+ 2035", kind="tesouro_ipca", amount=1000, reason="r"),
        act(action="aplicar", instrument="Tesouro Selic 2031", kind="tesouro_selic", amount=1500, reason="r")])
    c = director.check(decision, alloc, wealth, policy, set(), date(2026, 10, 30))
    assert [a.instrument for a in c.fixed_income_actions] == ["Tesouro Selic 2031"]
    assert any("só permite dinheiro novo em tesouro_selic" in n for n in c.adjustments)
    # No tolerance in the policy: the committee asks for it instead of assuming 20%.
    assert policy.max_drawdown_tolerance is None and any("max_drawdown_tolerance" in e for e in c.escalations)


@pytest.mark.unit
def test_the_rules_own_sector_concentration_is_flagged():
    frame = pd.DataFrame({"code": [f"C{i}" for i in range(10)],
                          "sector": ["Construção Civil"] * 3 + ["Bancos"] * 2 + [f"S{i}" for i in range(5)]})
    warnings = meeting.target_warnings(SimpleNamespace(target_frame=frame))
    assert len(warnings) == 1 and "3 ações de Construção Civil (30%" in warnings[0]


@pytest.mark.unit
def test_the_risk_facts_carry_a_simultaneous_stress_of_the_whole_mix():
    returns = pd.DataFrame({"stocks_index": 0.01, "stocks_dividend": 0.01, "listed_funds": 0.0, "cdi": 0.008},
                           index=pd.period_range("2020-01", periods=40, freq="M"))
    alloc = analysts.allocation(_policy(), _wealth(), {"AAAA3": 10_000}, 2000)
    with mock.patch.object(analysts, "class_returns", return_value=returns):
        facts, warnings = analysts.risk_facts(_policy(), _wealth(), alloc, {"AAAA3": 10_000}, {"AAAA3": "Bancos"})
    # Target 40% stocks and the funds capped at 15%: 0.4 x -59% + 0.15 x -30% = -28.1%.
    assert facts["simultaneous_stress"]["target_mix"] == pytest.approx(-0.281)
    assert any("Tolerância a quedas não informada" in w for w in warnings)


@pytest.mark.unit
def test_the_budget_keeps_each_calls_tokens_against_its_cap():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    def runnable_invoke(prompt, config):
        message = AIMessage(content="ok", usage_metadata={"input_tokens": 1000, "output_tokens": 2000,
                                                          "total_tokens": 3000})
        for cb in config["callbacks"]:
            cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))
        return "ok"

    budget = Budget(1.0)
    budget.invoke(SimpleNamespace(invoke=runnable_invoke), "p", model="claude-sonnet-5-5", max_output=8000,
                  label="Analista")
    assert budget.usage == [{"label": "Analista", "model": "claude-sonnet-5-5", "max_output": 8000,
                             "input_tokens": 1000, "output_tokens": 2000, "output_share": 0.25,
                             "cost_usd": pytest.approx((1000 * 2.0 + 2000 * 10.0) / 1e6)}]
