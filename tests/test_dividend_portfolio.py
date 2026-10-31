"""Dividend portfolio routine: selection rule, tax-aware orders, data cleaning, shadow
portfolio, anonymized package, LLM cost metering and the report. Offline: prices,
statements and models are synthetic or mocked.
"""
from datetime import date
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from tradingagents.dividend_portfolio import fundamentals, market, monitor, selection
from tradingagents.dividend_portfolio.orders import EXEMPT_MONTHLY_SALES, plan_orders
from tradingagents.dividend_portfolio.report import brl, render
from tradingagents.dividend_portfolio.shadow import Shadow
from tradingagents.portfolio import PortfolioContext, Position

DAYS = pd.bdate_range("2022-01-03", "2026-06-30")


def _frame(price=10.0, volume=1_000_000, dividends=None, drift=0.0):
    close = price * (1 + drift) ** np.arange(len(DAYS))
    frame = pd.DataFrame({"Close": close, "Adj Close": close, "Volume": float(volume), "Dividends": 0.0}, index=DAYS)
    for day, amount in (dividends or {}).items():
        frame.loc[frame.index[frame.index >= pd.Timestamp(day)][0], "Dividends"] = amount
    return frame


def _yearly(amount, years=(2023, 2024, 2025, 2026), month=4):
    return {f"{y}-{month:02d}-15": amount for y in years}


@pytest.fixture
def universe():
    companies = {f"cnpj{i}": market.Company(f"cnpj{i}", f"Empresa {i}", {code}, "Bancos")
                 for i, code in enumerate(["AAAA3", "BBBB4", "CCCC3", "DDDD11", "EEEE3"])}
    px = {
        "AAAA3": _frame(10, 9e6, _yearly(1.0)),                         # 10% yield, steady
        "BBBB4": _frame(10, 8e6, _yearly(0.6)),                         # 6%, steady
        "CCCC3": _frame(10, 7e6, {"2025-04-15": 3.0, "2026-04-15": 3.0}),  # 30%, but skipped 2024
        "DDDD11": _frame(10, 6e6, _yearly(0.8)),                        # 8%, steady
        "EEEE3": _frame(10, 1e3, _yearly(2.0)),                         # 20%, too illiquid for a universe of 4
    }
    return companies, px


@pytest.mark.unit
def test_the_target_is_the_highest_steady_yields_among_the_most_traded(universe):
    companies, px = universe
    rules = selection.Rules(universe=4, holdings=2)
    snap = selection.snapshot(companies, px, pd.Timestamp("2026-06-30"), rules)
    assert set(snap["code"]) == {"AAAA3", "BBBB4", "CCCC3", "DDDD11"}     # EEEE3 is not among the 4 most traded
    assert not snap.set_index("code").loc["CCCC3", "constant"]             # missed a year
    assert list(selection.target(snap, rules)["code"]) == ["AAAA3", "DDDD11"]
    assert snap.set_index("code").loc["AAAA3", "dy24"] == pytest.approx(0.1)


@pytest.mark.unit
def test_the_snapshot_reads_nothing_after_its_date(universe):
    companies, px = universe
    px["BBBB4"] = _frame(10, 8e6, {**_yearly(0.6, (2023, 2024, 2025), month=6), "2026-05-20": 5.0})
    snap = selection.snapshot(companies, px, pd.Timestamp("2026-05-01"), selection.Rules(universe=4))
    assert snap.set_index("code").loc["BBBB4", "dy12"] == pytest.approx(0.06)   # May's 5.0 is not known yet


@pytest.mark.unit
def test_a_reversed_spike_is_neutralized_and_a_jump_that_stays_is_flagged_not_changed():
    frame = _frame(10)
    adj = frame.columns.get_loc("Adj Close")
    frame.iloc[100, adj] *= 3.0                       # +200% for one day, then back: a data error
    frame.iloc[200:, adj] *= 3.0                      # +200% that stays: a spin-off misadjusted, or real
    frame.iloc[300:, adj] *= 0.23                     # -77%: within the limits, a real crash
    cleaned, neutral, suspect = market.clean(frame)
    assert neutral == [DAYS[100].date().isoformat(), DAYS[101].date().isoformat()]
    assert suspect == [DAYS[200].date().isoformat()] == cleaned.attrs["suspect_days"]
    r = cleaned["Adj Close"].pct_change()
    assert r.iloc[100] == 0 and r.iloc[101] == 0
    assert r.iloc[200] == pytest.approx(2.0) and r.iloc[300] == pytest.approx(-0.77)


@pytest.mark.unit
def test_a_fall_that_does_not_come_back_is_not_turned_into_a_rise():
    # Review 3, B2: [100, 5, 9.5] used to become [100, 100, 190].
    frame = pd.DataFrame({"Close": [100.0, 5.0, 9.5], "Adj Close": [100.0, 5.0, 9.5], "Volume": 1.0,
                          "Dividends": 0.0}, index=pd.bdate_range("2026-01-05", periods=3))
    cleaned, neutral, suspect = market.clean(frame)
    assert list(cleaned["Adj Close"]) == [100.0, 5.0, 9.5] and not neutral and suspect == ["2026-01-06"]


@pytest.mark.unit
def test_a_code_with_a_recent_unexplained_jump_and_an_excluded_sector_stay_out(universe):
    companies, px = universe
    px["AAAA3"] = px["AAAA3"].copy()
    px["AAAA3"].attrs["suspect_days"] = ["2026-03-02"]
    companies["cnpj3"].sector = "Telecomunicações"                  # DDDD11
    rules = selection.Rules(universe=4, holdings=2, excluded_sectors=("telecom",))
    snap = selection.snapshot(companies, px, pd.Timestamp("2026-06-30"), rules)
    assert "AAAA3" not in set(snap["code"])                         # so EEEE3 joins the 4 most traded
    assert list(selection.target(snap, rules)["code"]) == ["EEEE3", "BBBB4"]   # DDDD11 is telecom
    # A year later the jump no longer keeps it out.
    assert not selection.is_suspect(px["AAAA3"], pd.Timestamp("2027-03-03"), rules)


def _book(*positions, cash=0.0):
    return PortfolioContext(cash=cash, positions=[Position(ticker=t, quantity=q, average_price=a) for t, q, a in positions])


@pytest.mark.unit
def test_sales_stay_within_the_monthly_exemption_and_the_rest_waits():
    plan = plan_orders(_book(("OLDA3", 1000, 8.0), ("OLDB4.SA", 2000, 12.0)), ["NEWA3"],
                       {"OLDA3": 10.0, "OLDB4": 10.0, "NEWA3": 50.0})
    sold = {o.code: o.quantity for o in plan.orders if o.side == "venda"}
    assert sold == {"OLDA3": 1000, "OLDB4": 1000}                   # R$ 10k + R$ 10k = the exempt R$ 20k
    assert [(o.code, o.quantity) for o in plan.deferred] == [("OLDB4", 1000)]
    assert plan.sales_this_month == EXEMPT_MONTHLY_SALES and plan.estimated_tax == 0
    # Exempt month: OLDA3's gain is untaxed, OLDB4's loss is carried forward.
    assert plan.net_gain == pytest.approx(2000 - 2000) and plan.loss_carryforward_after == 0


@pytest.mark.unit
def test_above_the_exemption_the_net_gain_after_carried_losses_is_taxed():
    plan = plan_orders(_book(("OLDA3", 3000, 5.0)), ["NEWA3"], {"OLDA3": 10.0, "NEWA3": 50.0},
                       stay_exempt=False, loss_carryforward=4000.0)
    assert plan.taxable and plan.sales_this_month == 30_000
    assert plan.estimated_tax == pytest.approx(0.15 * (15_000 - 4000))
    assert plan.loss_carryforward_after == 0


@pytest.mark.unit
def test_losses_in_an_exempt_month_are_carried_forward():
    plan = plan_orders(_book(("OLDA3", 1000, 15.0)), ["NEWA3"], {"OLDA3": 10.0, "NEWA3": 50.0},
                       loss_carryforward=1000.0)
    assert plan.estimated_tax == 0 and plan.loss_carryforward_after == pytest.approx(6000.0)


@pytest.mark.unit
def test_new_money_goes_to_the_stocks_furthest_below_their_share_without_overspending():
    # R$ 4,200 in all, R$ 1,400 a stock: AAAA3's R$ 1,200 is within the 20% band.
    book = _book(("AAAA3", 120, 10.0), cash=1000.0)
    plan = plan_orders(book, ["AAAA3", "BBBB4", "CCCC3"], {"AAAA3": 10.0, "BBBB4": 20.0, "CCCC3": 5.0},
                       contribution=2000.0)
    bought = {o.code: o.quantity * o.price for o in plan.orders if o.side == "compra"}
    spent = sum(bought.values())
    assert spent <= 3000.0 + 1e-9 and plan.cash_left == pytest.approx(3000.0 - spent)
    assert plan.target_value == pytest.approx(1400.0)
    assert bought["BBBB4"] == pytest.approx(1400, abs=20) and bought["CCCC3"] == pytest.approx(1400, abs=5)
    assert bought.get("AAAA3", 0) <= 200                # only what is left, once the new stocks are filled


@pytest.mark.unit
def test_between_rebalances_nothing_is_sold_and_new_money_goes_to_the_target():
    book = _book(("AAAA3", 100, 10.0), ("OLDA3", 100, 10.0))
    plan = plan_orders(book, ["AAAA3", "BBBB4"], {"AAAA3": 10.0, "BBBB4": 10.0, "OLDA3": 10.0},
                       contribution=500.0, sell=False)
    assert all(o.side == "compra" for o in plan.orders) and {o.code for o in plan.orders} <= {"AAAA3", "BBBB4"}


@pytest.mark.unit
def test_a_book_in_cash_is_built_up_between_rebalances():
    plan = plan_orders(PortfolioContext(cash=0.0), ["AAAA3", "BBBB4"], {"AAAA3": 10.0, "BBBB4": 20.0},
                       contribution=2000.0, sell=False)
    assert {o.code: o.value for o in plan.orders} == {"AAAA3": 1000.0, "BBBB4": 1000.0}


@pytest.mark.unit
def test_the_shadow_replays_its_targets_and_ignores_repeats(tmp_path):
    shadow = Shadow(tmp_path / "sombra.json")
    assert shadow.record("2026-01-02", ["AAAA3"])
    assert not shadow.record("2026-01-02", ["BBBB4"]) and not shadow.record("2026-02-01", ["AAAA3"])
    assert shadow.record("2026-03-02", ["BBBB4"])
    assert Shadow(tmp_path / "sombra.json").events == shadow.events
    px = {"AAAA3": _frame(10, drift=0.001), "BBBB4": _frame(10, drift=-0.001)}
    bench = pd.Series(1.0, index=DAYS)
    perf = shadow.performance(px, {"bova11": bench})
    a = px["AAAA3"]["Adj Close"]
    b = px["BBBB4"]["Adj Close"]
    first, switch = (DAYS[DAYS.searchsorted(pd.Timestamp(d), side="right")] for d in ("2026-01-02", "2026-03-02"))
    expected = a.loc[switch] / a.loc[first] * b.iloc[-1] / b.loc[switch] - 1
    assert perf["portfolio"] == pytest.approx(expected) and perf["bova11"] == 0


@pytest.mark.unit
def test_a_shadow_holding_without_a_price_counts_as_cash(tmp_path):
    shadow = Shadow(tmp_path / "sombra.json")
    shadow.record("2026-01-02", ["AAAA3", "GONE3"])
    px = {"AAAA3": _frame(10, drift=0.001)}
    perf = shadow.performance(px, {"bova11": pd.Series(1.0, index=DAYS)})
    a = px["AAAA3"]["Adj Close"]
    first = DAYS[DAYS.searchsorted(pd.Timestamp("2026-01-02"), side="right")]
    # Half in AAAA3, half in cash: not all of it in AAAA3.
    assert perf["portfolio"] == pytest.approx((a.iloc[-1] / a.loc[first] - 1) / 2)
    assert perf["unpriced_as_cash"] == ["GONE3"]


@pytest.mark.unit
def test_the_package_is_anonymous_and_common_size():
    docs = [{"kind": "dfp", "refer": "2025-12-31", "months": 12, "dividends_paid": 60.0,
             "values": {"3.01": ("Receita", 1000.0), "3.11": ("Lucro", 100.0), "1": ("Ativo Total", 4000.0),
                        "2.03": ("Patrimônio Líquido", 2000.0), "6.01": ("Caixa operacional", 150.0)}},
            {"kind": "dfp", "refer": "2024-12-31", "months": 12, "dividends_paid": 50.0,
             "values": {"3.01": ("Receita", 800.0), "3.11": ("Lucro", 80.0), "1": ("Ativo Total", 3600.0)}}]
    text = fundamentals.package("Bancos", docs, [80, 90, 95, 100], 0.08, 0.075)
    assert "2025" not in text and "2024" not in text
    assert "| 3.11 Lucro | 10.0 | 10.0 |" in text                  # net income, % of revenue
    assert "| 2.03 Patrimônio Líquido | 50.0 |" in text            # equity, % of total assets
    assert "| Revenue | 25.0 |" in text and "| Payout | 60.0 | 62.5 |" in text
    assert "80, 90, 95, 100" in text and "8.0%" in text


@pytest.mark.unit
def test_the_cost_meter_adds_up_reported_usage():
    meter = monitor.CostMeter("claude-haiku-5-5", budget_usd=0.001)
    message = SimpleNamespace(usage_metadata={"input_tokens": 4000, "output_tokens": 1000})
    meter.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=message)]]))
    assert meter.spent == pytest.approx((4000 * 0.10 + 1000 * 0.50) / 1e6) and not meter.exhausted
    meter.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=message)] * 2]))
    assert meter.exhausted


@pytest.mark.unit
def test_a_quiet_week_costs_no_model_call():
    quiet = ("No filing by this company in the window is in CVM's register.\n"
             "None of these sites has an article naming AAAA3 in the window.")
    llm = mock.MagicMock()
    with mock.patch.object(monitor, "create_tier_client") as factory, \
            mock.patch.object(monitor, "route_to_vendor", return_value=quiet):
        factory.return_value.get_llm.return_value.with_structured_output.return_value = llm
        assistant = monitor.Assistant({"llm_provider": "openai"})
        assert assistant.news("AAAA3", date(2026, 6, 30)) is None
    llm.invoke.assert_not_called()


@pytest.mark.unit
def test_the_report_shows_orders_tax_and_alerts_in_portuguese():
    target = pd.DataFrame([{"code": "AAAA3", "name": "Empresa A", "sector": "Bancos", "dy12": 0.1, "dy24": 0.095}])
    plan = plan_orders(_book(("OLDA3", 1000, 8.0)), ["AAAA3"], {"OLDA3": 10.0, "AAAA3": 25.0})
    alert = monitor.NewsAlert(level="alerta", summary="Corte de dividendos anunciado.", dividend_news=True)
    text = render("2026-06-30", True, target, [{"code": "OLDA3", "quantity": 1000, "price": 10.0, "value": 10000.0,
                                                "in_target": False}], plan,
                  {"OLDA3": alert}, {"AAAA3": monitor.CutRisk(key_points=["x"], probability_cut=35)},
                  {"since": "2026-01-05", "to": "2026-06-30", "portfolio": 0.08, "bova11": 0.05, "divo11": 0.06},
                  0.012, True)
    assert "rebalanceamento semestral de 2026-06-30" in text
    assert "| venda | OLDA3 | 1000 | R$ 10,00 | R$ 10.000,00 | saiu da seleção |" in text
    assert "| AAAA3 | Empresa A | Bancos | 10,0% | 9,5% | 35% |" in text
    assert "🔴 alerta · dividendos: Corte de dividendos anunciado." in text
    assert "| Carteira-sombra | +8,0% |" in text and brl(1234567.891) == "R$ 1.234.567,89"


@pytest.mark.unit
def test_a_week_without_orders_says_why():
    plan = plan_orders(_book(("OLDA3", 10, 8.0)), ["AAAA3"], {"OLDA3": 10.0, "AAAA3": 25.0}, sell=False)
    target = pd.DataFrame([{"code": "AAAA3", "name": "A", "sector": "Bancos", "dy12": 0.1, "dy24": 0.1}])
    text = render("2026-10-30", False, target, [], plan, {}, {}, None, 0.0, False)
    assert "fora de junho e dezembro nada é vendido" in text and "já segue a regra" not in text


@pytest.mark.unit
def test_a_book_in_fixed_income_moves_to_its_stock_target_in_monthly_steps():
    from tradingagents.dividend_portfolio.allocation import plan_allocation

    a = plan_allocation(stocks=650.0, fixed_income=99_350.0, contribution=1000.0, target_share=0.3,
                        transition_months=12)
    assert a.contribution_to_stocks == 1000.0 and a.contribution_to_fixed_income == 0.0
    assert a.status == "shortfall" and a.gap == pytest.approx(0.3 * 101_000 - 1650)
    assert a.monthly_transfer == pytest.approx(a.gap / 12) and a.to_stocks_this_month == pytest.approx(1000 + a.gap / 12)


@pytest.mark.unit
def test_a_stock_surplus_is_fixed_with_new_money_not_sales():
    from tradingagents.dividend_portfolio.allocation import plan_allocation

    a = plan_allocation(stocks=50_000.0, fixed_income=50_000.0, contribution=2000.0, target_share=0.3)
    assert a.status == "surplus" and a.contribution_to_stocks == 0 and a.contribution_to_fixed_income == 2000
    assert a.monthly_transfer == 0 and a.to_stocks_this_month == 0
    within = plan_allocation(stocks=31_000.0, fixed_income=69_000.0, contribution=0.0, target_share=0.3)
    assert within.status == "ok" and within.monthly_transfer == 0
    with pytest.raises(ValueError):
        plan_allocation(1.0, 1.0, 0.0, 1.5)


@pytest.mark.unit
def test_a_held_class_of_a_selected_company_stands_in_for_the_selected_code():
    from tradingagents.dividend_portfolio.routine import same_company_codes

    companies = {"bradesco": market.Company("bradesco", "Bradesco", {"BBDC3", "BBDC4"}),
                 "itau": market.Company("itau", "Itaú", {"ITUB3", "ITUB4"})}
    target = pd.DataFrame([{"cnpj": "bradesco", "code": "BBDC4"}, {"cnpj": "itau", "code": "ITUB4"}])
    out = same_company_codes(target, companies, {"BBDC3", "PETR4"}, {"BBDC3", "BBDC4", "ITUB4"})
    assert list(out["code"]) == ["BBDC3", "ITUB4"] and list(target["code"]) == ["BBDC4", "ITUB4"]


@pytest.mark.unit
def test_lots_across_brokers_are_one_position_at_the_weighted_average_cost():
    # Review 3, C3: the first lot's price (8) would show a R$ 400 gain that is not there.
    plan = plan_orders(_book(("OLDA3", 100, 8.0), ("OLDA3", 100, 12.0)), ["NEWA3"], {"OLDA3": 10.0, "NEWA3": 10.0})
    sale = [o for o in plan.orders if o.side == "venda"]
    assert [(o.code, o.quantity) for o in sale] == [("OLDA3", 200)] and sale[0].gain == pytest.approx(0.0)
    unknown = plan_orders(_book(("OLDA3", 100, 8.0), ("OLDA3", 100, None)), ["NEWA3"], {"OLDA3": 10.0, "NEWA3": 10.0})
    assert [o.gain for o in unknown.orders if o.side == "venda"] == [None]


@pytest.mark.unit
def test_the_tax_on_the_sales_is_kept_aside_not_spent():
    # Review 3, C4: R$ 30,000 of sales with a R$ 10,000 gain owe R$ 1,500, which stays in cash.
    plan = plan_orders(_book(("OLDA3", 3000, 20 / 3)), ["NEWA3"], {"OLDA3": 10.0, "NEWA3": 1.0}, stay_exempt=False)
    assert plan.estimated_tax == pytest.approx(1500)
    bought = sum(o.value for o in plan.orders if o.side == "compra")
    assert bought == pytest.approx(28_500) and plan.cash_left == pytest.approx(0, abs=1)


@pytest.mark.unit
def test_pending_sales_are_retried_between_rebalances_and_a_kept_code_is_left_alone():
    # Review 3, C6: deferred sales used to wait for the next rebalance, six months on.
    book = _book(("OLDA3", 3000, 10.0), ("HELD3", 100, 10.0), ("ODDA3", 100, 10.0))
    prices = {"OLDA3": 10.0, "HELD3": 10.0, "ODDA3": 10.0, "NEWA3": 10.0}
    plan = plan_orders(book, ["HELD3", "NEWA3"], prices, sell=False, pending={"OLDA3"}, keep={"HELD3"})
    assert [(o.code, o.quantity, o.reason) for o in plan.orders if o.side == "venda"] == \
        [("OLDA3", 2000, "venda pendente")]                          # ODDA3 is not pending: not sold
    assert [(o.code, o.quantity) for o in plan.deferred] == [("OLDA3", 1000)]
    assert {o.code for o in plan.orders if o.side == "compra"} == {"NEWA3"}   # HELD3 is under review


def _days_to(end):
    return pd.bdate_range("2022-01-03", end)


def _frame_to(end, dividends=None, volume=5e6):
    days = _days_to(end)
    frame = pd.DataFrame({"Close": 10.0, "Adj Close": 10.0, "Volume": volume, "Dividends": 0.0}, index=days)
    for day, amount in (dividends or {}).items():
        frame.loc[frame.index[frame.index >= pd.Timestamp(day)][0], "Dividends"] = amount
    return frame


@pytest.mark.unit
def test_the_routine_freezes_its_target_retries_pending_sales_and_leaves_funds_alone(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.dividend_portfolio import routine

    end = "2026-08-31"
    companies = {f"c{i}": market.Company(f"c{i}", f"Empresa {i}", {code}, "Bancos")
                 for i, code in enumerate(["AAAA3", "BBBB3", "CCCC3", "OLDD3"])}
    px = {"AAAA3": _frame_to(end, _yearly(1.0)), "BBBB3": _frame_to(end, _yearly(0.8)),
          # A large July dividend makes CCCC3 the rule's pick in August, but not in June.
          "CCCC3": _frame_to(end, {**_yearly(0.6), "2026-07-15": 5.0}), "OLDD3": _frame_to(end)}
    index = _frame_to(end)["Close"]
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    rules = selection.Rules(universe=4, holdings=2)

    def run(book, day):
        with mock.patch.object(routine.market, "companies", return_value=companies), \
                mock.patch.object(routine.market, "histories", side_effect=lambda codes: {c: px[c] for c in codes if c in px}), \
                mock.patch.object(routine.market, "index_series", return_value=index):
            return routine.review(book, as_of=day, use_llm=False, config=config, rules=rules)

    fund = ("FUND11", 100, 100.0)
    june = run(_book(("OLDD3", 3000, 10.0), fund), date(2026, 6, 30))
    assert june.rebalance and june.target == ["AAAA3", "BBBB3"] and june.outside == ["FUND11"]
    assert {(o.code, o.quantity) for o in june.plan.orders if o.side == "venda"} == {("OLDD3", 2000)}
    assert june.pending == {"OLDD3"}

    # August: the investor sold 2,000 OLDD3; the rule alone would now pick CCCC3.
    august = run(_book(("OLDD3", 1000, 10.0), ("AAAA3", 1000, 10.0), ("BBBB3", 1000, 10.0), fund), date(2026, 8, 31))
    assert not august.rebalance and august.target == ["AAAA3", "BBBB3"]
    assert [(o.code, o.quantity, o.reason) for o in august.plan.orders if o.side == "venda"] == \
        [("OLDD3", 1000, "venda pendente")]
    assert {o.code for o in august.plan.orders if o.side == "compra"} <= {"AAAA3", "BBBB3"}
    assert "FUND11" not in {o.code for o in august.plan.orders}


@pytest.mark.unit
def test_a_ticker_with_a_digit_in_its_root_is_a_listed_share():
    # B3 S.A. trades as B3SA3: the old four-letter pattern left it out of the universe.
    from tradingagents.dataflows.vendors.b3_market import CODE
    assert all(CODE.match(c) for c in ("B3SA3", "PETR4", "TAEE11", "BPAC11"))
    assert not any(CODE.match(c) for c in ("000000", "4030", "ABCD12", "PETR"))
