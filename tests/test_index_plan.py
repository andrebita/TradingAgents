"""The portfolio by index funds (review 5): policy, Tesouro offers, the month's plan, the director's menu and a
meeting without models. Offline: prices, offers and the macro analyst are synthetic or mocked.
"""
from datetime import date
from unittest import mock

import pandas as pd
import pytest

from tradingagents.committee import director, index_meeting, indices
from tradingagents.committee.policy import IndexPlan, InvestmentPolicy
from tradingagents.committee.wealth import FixedIncome, Listed, Wealth
from tradingagents.dataflows.vendors import tesouro_direto

TODAY = date(2026, 11, 2)


def _policy(**kw):
    plan = IndexPlan(horizon_end=date(2031, 11, 1), migration_start=date(2026, 11, 1), **kw)
    return InvestmentPolicy(monthly_expenses=10_000, monthly_contribution=3000, index_plan=plan)


def _wealth():
    return Wealth(as_of=date(2026, 10, 31), fixed_income=[
        FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                    value=80_000, liquidity_days=1),
        FixedIncome(name="Tesouro IPCA+ 2045", kind="tesouro_ipca", issuer="Tesouro", indexer="IPCA", value=20_000,
                    maturity=date(2045, 5, 15), liquidity_days=1, applied_on=date(2023, 1, 10), applied_value=16_000),
        FixedIncome(name="Tesouro IPCA+ 2029", kind="tesouro_ipca", issuer="Tesouro", indexer="IPCA", value=10_000,
                    maturity=date(2029, 5, 15), liquidity_days=1),
        FixedIncome(name="CDB 2026", kind="cdb", issuer="Banco A", value=5_000, maturity=date(2026, 10, 30), fgc=True)],
        listed=[Listed(code="PETR4", quantity=500, average_price=30.0), Listed(code="ITSA4", quantity=100),
                Listed(code="FUND11", quantity=120, kind="fii", average_price=90.0)])


PRICES = {"PETR4": 40.0, "ITSA4": 10.0, "FUND11": 100.0, "BOVA11": 160.0, "DIVO11": 120.0, "WRLD11": 150.0}


@pytest.mark.unit
def test_the_index_plan_adds_up_and_its_default_is_on_the_menu():
    with pytest.raises(ValueError):
        IndexPlan(brazil=0.3, international=0.4, fixed_income=0.4)
    with pytest.raises(ValueError):
        IndexPlan(brazil_default="SMAL11")


@pytest.mark.unit
def test_the_selic_bond_to_buy_is_the_best_spread_within_five_years():
    raw = ("Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;"
           "PU Venda Manha;PU Base Manha\n"
           "Tesouro Selic;01/03/2029;30/10/2026;0,06;0,07;20185,67;20180,00;20180,00\n"
           "Tesouro Selic;01/03/2031;30/10/2026;0,10;0,11;20120,75;20110,00;20110,00\n"
           "Tesouro Selic;01/03/2033;30/10/2026;0,15;0,16;20000,00;19990,00;19990,00\n"
           "Tesouro IPCA+;15/05/2035;30/10/2026;7,88;8,00;2509,91;2500,00;2500,00\n"
           "Tesouro Selic;01/03/2031;29/10/2026;0,09;0,10;20110,00;20100,00;20100,00\n").encode("latin-1")
    offers = tesouro_direto.parse(raw)
    assert len(offers) == 4 and offers[0].as_of == date(2026, 10, 30)
    best = tesouro_direto.selic_to_buy(offers, TODAY)
    assert best.name == "Tesouro Selic 2031" and best.rate == pytest.approx(0.10)    # 2033 is past 5 years


@pytest.mark.unit
def test_the_plan_migrates_at_its_pace_and_fills_the_largest_gap():
    plan = indices.build(_policy(), _wealth(), PRICES, TODAY, 3000)
    by = {(o.side, o.asset): o for o in plan.orders}
    # Single stocks within the exemption, largest first: PETR4 (R$ 20,000) is cut to R$ 19,000.
    assert by[("venda", "PETR4")].quantity == 475
    assert ("venda", "ITSA4") not in by and any("ITSA4" in p for p in plan.pending)
    # Listed funds over 12 months: a twelfth of R$ 12,000, in whole quotas; 20% on the gain.
    fund = by[("venda", "FUND11")]
    assert fund.quantity == 10 and fund.tax == pytest.approx(0.20 * 10 * 10)
    # The IPCA+ 2045 matures after the horizon: redeemed, 15% on the R$ 4,000 gain (over 720 days).
    bond = by[("resgatar", "Tesouro IPCA+ 2045")]
    assert bond.tax == pytest.approx(600)
    assert any(h.startswith("Tesouro IPCA+ 2029") for h in plan.holds)
    # The matured CDB goes to Selic; PETR4's proceeds buy the Brazil ETF (a swap within the sleeve), the rest of
    # the equity money goes to the largest gap, the global ETF.
    assert any(o.side == "aplicar" and o.reason == "vencimento de CDB 2026" for o in plan.orders)
    # Up to the Brazil target: 20% of R$ 91,000 investable is R$ 18,200, less the R$ 2,000 left in stocks.
    assert by[("compra", "BOVA11")].value == pytest.approx(16_200, abs=160)
    assert by[("compra", "WRLD11")].quantity > 0
    # Sales only: nothing in the plan spends money it does not have.
    spent = sum(o.value for o in plan.orders if o.side == "compra")
    sources = sum(o.value - (o.tax or 0) for o in plan.orders if o.side == "venda") + 3000 + plan.transfer
    assert spent <= sources + 1


@pytest.mark.unit
def test_the_director_picks_from_the_menu_and_holds_back_real_sales_only():
    policy = _policy()
    plan = indices.build(policy, _wealth(), PRICES, TODAY, 3000)
    assets = {o.asset for o in plan.migration_sales()}
    d = director.IndexDecision(letter="c", brazil_vehicle="SMAL11", brazil_vehicle_reason="r", held_back_sales=[
        director.HoldBack(asset="FUND11", reason="a"), director.HoldBack(asset="XPTO11", reason="b"),
        director.HoldBack(asset="PETR4", reason="c"), director.HoldBack(asset="Tesouro IPCA+ 2045", reason="d")])
    c = director.check_index(d, policy, assets)
    assert c.vehicle == "BOVA11" and c.held_back == {"FUND11", "PETR4"} and len(c.adjustments) == 3
    half = director.check_index(d.model_copy(update={"brazil_vehicle": "metade", "held_back_sales": []}), policy,
                                assets)
    assert half.vehicle == "metade"
    replanned = indices.build(policy, _wealth(), PRICES, TODAY, 3000, brazil_vehicle="metade",
                              held_back=c.held_back)
    assert not {"FUND11", "PETR4"} & {o.asset for o in replanned.migration_sales()}


@pytest.mark.unit
def test_a_fall_from_the_12_month_high_puts_the_crisis_plan_on_the_table():
    days = pd.bdate_range("2025-11-01", "2026-10-30")
    bova = pd.Series(200.0, index=days)
    bova.iloc[-5:] = 150.0                                   # -25%
    status = indices.crisis_status({"BOVA11": bova, "WRLD11": pd.Series(150.0, index=days)}, TODAY, 0.15, 0.30)
    assert status["status"] == "atenção" and status["falls_from_12m_high"]["BOVA11"] == -0.25 and status["plan"]
    bova.iloc[-5:] = 130.0
    assert indices.crisis_status({"BOVA11": bova}, TODAY, 0.15, 0.30)["status"] == "crise"


def _frames():
    days = pd.bdate_range("2025-10-01", "2026-10-30")
    return {c: pd.DataFrame({"Close": p, "Adj Close": p, "Volume": 1e6, "Dividends": 0.0}, index=days)
            for c, p in PRICES.items()}


def _hold(tmp_path, **kw):
    frames = _frames()
    config = {**index_meeting.DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    with mock.patch.object(index_meeting.b3_market, "histories",
                           side_effect=lambda codes: {c: frames[c] for c in codes if c in frames}), \
            mock.patch.object(tesouro_direto, "offers", side_effect=RuntimeError("offline")), \
            mock.patch.object(index_meeting.analysts, "macro",
                              return_value=index_meeting.analysts.Report("Analista Macro", {})):
        from tradingagents.committee import meeting

        return meeting.hold(_policy(), _wealth(), as_of=TODAY, config=config, **kw)


@pytest.mark.unit
def test_an_index_meeting_without_models_writes_the_plan_and_keeps_the_choice(tmp_path):
    m = _hold(tmp_path, use_llm=False)
    assert "## 1. Carteira recomendada" in m.letter and "**Vender** | 475" in m.letter
    assert "ETF da parte Brasil: **BOVA11**" in m.letter and "### Mercado: normal" in m.letter
    assert "O que mudou no plano" not in m.letter
    log = (tmp_path / "comite" / "decisoes.jsonl").read_text().splitlines()
    assert '"brazil_vehicle": "BOVA11"' in log[-1]
    assert index_meeting.previous_vehicle(tmp_path / "comite", "DIVO11") == "BOVA11"


@pytest.mark.unit
def test_the_models_read_the_index_policy_without_the_old_rule():
    seen = _policy().for_models()
    assert "index_plan" in seen and "stock_target" not in seen and "listed_funds_max" not in seen
    old = InvestmentPolicy(monthly_expenses=10_000).for_models()
    assert "stock_target" in old and "index_plan" not in old and "claude" not in old


@pytest.mark.unit
def test_the_stress_is_in_reais_of_the_investable_wealth():
    plan = indices.build(_policy(), _wealth(), PRICES, TODAY, 3000)
    facts, warnings = index_meeting.risk_facts(_policy(), plan)
    target = facts["simultaneous_stress"]["target"]
    # Target: 20% Brazil at -50% and 40% abroad at -35% of the investable wealth plus the contribution.
    assert target["investable_brl"] == pytest.approx(91_000)
    assert target["loss_brl"] == pytest.approx(-0.5 * 18_200 - 0.35 * 36_400)
    assert target["fall"] == pytest.approx(-0.24)
    assert "R$" in warnings[0] and "24%" in warnings[0]


@pytest.mark.unit
def test_the_fixed_income_analyst_reads_the_plans_redemptions_and_rule():
    plan = indices.build(_policy(), _wealth(), PRICES, TODAY, 3000)
    r = index_meeting.fixed_income_report(_policy(), _wealth(), plan, TODAY, None)
    assert "long_dated_marked_to_market" not in r.facts
    assert any(o["side"] == "resgatar" and o["asset"] == "Tesouro IPCA+ 2045" for o in r.facts["plan"]["orders"])
    assert any(h.startswith("Tesouro IPCA+ 2029") for h in r.facts["plan"]["held_to_maturity"])
    assert "redeemed by rule" in index_meeting.FIXED_INCOME_BRIEF


def _with_second_fund():
    w = _wealth()
    w.listed.append(Listed(code="INFR11", quantity=100, kind="fi_infra"))
    return w


@pytest.mark.unit
def test_the_director_sees_what_replaces_a_postponed_sale_and_the_letter_what_changed():
    policy, wealth, prices = _policy(), _with_second_fund(), {**PRICES, "INFR11": 100.0}

    def rebuild(held=frozenset()):
        return indices.build(policy, wealth, prices, TODAY, 3000, held_back=set(held))

    first = rebuild()
    effects = index_meeting.hold_back_effects(first, rebuild)
    # FUND11 (R$ 12,000) leaves first; postponed, its 1/12 of the funds passes to INFR11. A bond has no substitute.
    assert effects["FUND11"].startswith("no lugar: INFR11")
    assert effects["Tesouro IPCA+ 2045"] == "nada entra no lugar neste mês"
    # FI-Infra: exempt on sale, so no average price is needed for the tax; its framing is asked once.
    infra = next(o for o in rebuild({"FUND11"}).orders if o.asset == "INFR11")
    assert infra.tax == 0.0
    changes = index_meeting.order_changes(first, rebuild({"FUND11"}), {"FUND11": "sem preço médio"})
    assert any(c.startswith("Vender FUND11") and "adiada pelo diretor (sem preço médio)" in c for c in changes)
    assert any(c.startswith("Vender INFR11") and "no lugar da venda adiada" in c for c in changes)


@pytest.mark.unit
def test_the_directors_escalations_do_not_repeat_the_plans_pending_list():
    pending = ["Vendas que ficam para o mês seguinte, pelo limite de isenção: AAAA3, BBBB4, CCCC3.",
               "Informe o preço médio de DDDD3, EEEE11: sem ele, o IR dessas vendas (20% sobre o ganho) não pode "
               "ser calculado."]
    escalations = ["Ciência de que ficam para o mês seguinte, pelo limite de isenção, as vendas de AAAA3, BBBB4 e "
                   "CCCC3.",
                   "Informar o preço médio de compra (com corretagem) de DDDD3 e EEEE11; sem ele o IR das vendas não "
                   "pode ser calculado.",
                   "Declarar a maior queda que aceita, em reais e em percentual.",
                   "Confirmar que não há outras vendas de AAAA3, BBBB4 e CCCC3 em outra corretora neste mês."]
    kept, dropped = director.merge_escalations(pending, escalations)
    assert kept == escalations[2:] and dropped == escalations[:2]


@pytest.mark.unit
def test_a_postponed_sale_shows_in_the_letter_and_the_log(tmp_path):
    decision = director.IndexDecision(letter="Carta.", brazil_vehicle="BOVA11", brazil_vehicle_reason="padrão",
                                      held_back_sales=[director.HoldBack(asset="FUND11", reason="sem custo")],
                                      escalations=["Informe o preço médio de FUND11: sem ele o IR não pode ser "
                                                   "calculado."])
    with mock.patch.object(index_meeting.analysts, "Models") as models, \
            mock.patch.object(index_meeting.analysts, "_with_view", side_effect=lambda r, *a, **k: r), \
            mock.patch.object(director, "ask_index", return_value=decision) as ask:
        models.return_value.spent = 0.0
        models.return_value.budget.by_model = {}
        m = _hold(tmp_path)
    assert "if_held_back" in ask.call_args.args[2]
    assert "### O que mudou no plano depois da decisão do diretor" in m.letter
    assert "Vender FUND11 (10, R$ 1.000,00) sai do plano: adiada pelo diretor (sem custo)." in m.letter
    log = (tmp_path / "comite" / "decisoes.jsonl").read_text().splitlines()[-1]
    assert '"held_back": ["FUND11"]' in log and "changes_after_director" in log
