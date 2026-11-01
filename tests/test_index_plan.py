"""The portfolio by index funds (review 5): policy, Tesouro offers, the month's plan, the director's menu and a
meeting without models. Offline: prices, offers and the macro analyst are synthetic or mocked.
"""
import json
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
    assert "ganho de R$ 100,00, IR de R$ 20,00" in fund.reason
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
    assert status["status"] == "atenção" and status["falls_from_high"]["BOVA11"] == -0.25 and status["plan"]
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
    w.listed.append(Listed(code="INFR11", quantity=100, kind="fi_infra", average_price=95.0))
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
    decision = director.IndexDecision(brazil_vehicle="BOVA11", brazil_vehicle_reason="padrão",
                                      held_back_sales=[director.HoldBack(asset="FUND11", reason="sem custo")],
                                      escalations=["Informe o preço médio de FUND11: sem ele o IR não pode ser "
                                                   "calculado."], key_points=["Troca de ações por BOVA11."])
    with mock.patch.object(index_meeting.analysts, "Models") as models, \
            mock.patch.object(index_meeting.analysts, "_with_view", side_effect=lambda r, *a, **k: r), \
            mock.patch.object(director, "ask_index", return_value=decision) as ask, \
            mock.patch.object(director, "write_letter",
                              return_value="Vendemos R$ 18.050,00 em ações e o risco é de R$ 50 mil.") as write:
        models.return_value.spent = 0.0
        models.return_value.budget.by_model = {}
        m = _hold(tmp_path)
    assert "if_held_back" in ask.call_args.args[2]
    assert "### O que mudou no plano depois da decisão do diretor" in m.letter
    assert "Vender FUND11 (10, R$ 1.000,00) sai do plano: adiada pelo diretor (sem custo)." in m.letter
    # The letter is written on the final plan: FUND11's sale is gone from the data it reads.
    data = write.call_args.args[1]
    assert not any(o["asset"] == "FUND11" for o in data["final_plan"]["orders"])
    assert data["final_plan"]["totals"]["sales"] == pytest.approx(19_000) and data["changes"]
    # The amounts it cites are checked: R$ 18.050 is not in the plan, nor R$ 50 mil.
    assert "## 2. Carta do comitê" in m.letter and "**Conferência automática:**" in m.letter
    assert "R$ 18.050,00" in m.letter.split("**Conferência automática:**")[1]
    assert "**Pontos do diretor:**" in m.letter
    log = (tmp_path / "comite" / "decisoes.jsonl").read_text().splitlines()[-1]
    assert '"held_back": ["FUND11"]' in log and "changes_after_director" in log and "letter_audit" in log


@pytest.mark.unit
def test_the_selic_bond_matures_by_the_horizon_or_just_after_it():
    raw = ("Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;"
           "PU Venda Manha;PU Base Manha\n"
           "Tesouro Selic;01/03/2029;30/10/2026;0,06;0,07;20185,67;20180,00;20180,00\n"
           "Tesouro Selic;01/03/2031;30/10/2026;0,10;0,11;20120,75;20110,00;20110,00\n"
           "Tesouro Selic;01/03/2033;30/10/2026;0,15;0,16;20000,00;19990,00;19990,00\n").encode("latin-1")
    offers = tesouro_direto.parse(raw)
    assert tesouro_direto.selic_to_buy(offers, TODAY, until=date(2030, 1, 1)).name == "Tesouro Selic 2029"
    # In 2029 the five-year rule would reach 2033: the horizon keeps it to the bond that matures first after it.
    assert tesouro_direto.selic_to_buy(offers, date(2029, 6, 1), until=date(2031, 11, 1)).name == "Tesouro Selic 2031"
    assert tesouro_direto.selic_to_buy(offers, date(2031, 6, 1), until=date(2031, 11, 1)).name == "Tesouro Selic 2033"


@pytest.mark.unit
def test_the_transition_closes_on_schedule():
    wealth = Wealth(as_of=date(2026, 10, 31), fixed_income=[
        FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                    value=200_000, liquidity_days=1)])
    first = indices.build(_policy(), wealth, PRICES, date(2026, 11, 2), 0)
    last = indices.build(_policy(), wealth, PRICES, date(2027, 10, 4), 0)
    # Month 1 moves a twelfth of the gap; month 12 moves all that is left, not a twelfth of it.
    gap = 0.6 * 140_000
    assert first.transfer == pytest.approx(gap / 12)
    assert last.transfer == pytest.approx(gap)


@pytest.mark.unit
def test_a_fund_sale_without_its_cost_is_blocked_and_the_pace_passes_on():
    wealth = _with_second_fund()
    wealth.listed[2] = Listed(code="FUND11", quantity=120, kind="fii")          # no average price
    plan = indices.build(_policy(), wealth, {**PRICES, "INFR11": 100.0}, TODAY, 3000)
    sold = {o.asset for o in plan.orders if o.side == "venda"}
    assert "FUND11" not in sold and "INFR11" in sold
    assert any(p.startswith("Venda de FUND11 bloqueada") for p in plan.pending)
    assert any("Lei 12.431" in p for p in plan.pending)
    # Unknown tax is never shown as zero; the single-stock sales carry the CPF-wide check.
    assert all(o.tax is not None for o in plan.orders if o.side == "venda")
    assert any("por CPF" in p for p in plan.pending)


@pytest.mark.unit
def test_december_rebalancing_puts_the_proceeds_to_work():
    wealth = Wealth(as_of=date(2026, 11, 30), fixed_income=[
        FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                    value=100_000, liquidity_days=1)],
        listed=[Listed(code="BOVA11", quantity=300, kind="etf", counts_as="acoes", average_price=150.0),
                Listed(code="WRLD11", quantity=200, kind="etf", counts_as="internacional", average_price=140.0)])
    plan = indices.build(_policy(), wealth, PRICES, date(2026, 12, 1), 0)
    sale = next(o for o in plan.orders if o.side == "venda" and o.asset == "BOVA11")
    assert sale.tax == pytest.approx(0.15 * (160 - 150) * sale.quantity)
    # The proceeds, net of the tax, buy the sleeve furthest below target (abroad) and the rest goes to Selic; the
    # month's transition from Selic buys equities too.
    bought = sum(o.value for o in plan.orders if o.side in ("compra", "aplicar"))
    redeemed = sum(o.value for o in plan.orders if o.side == "resgatar")
    assert bought - redeemed == pytest.approx(sale.value - sale.tax, abs=1.0)
    assert any(o.side == "compra" and o.asset == "WRLD11" for o in plan.orders)
    blocked = Wealth(as_of=wealth.as_of, fixed_income=wealth.fixed_income,
                     listed=[Listed(code="BOVA11", quantity=300, kind="etf", counts_as="acoes")])
    plan = indices.build(_policy(), blocked, PRICES, date(2026, 12, 1), 0)
    assert not any(o.side == "venda" for o in plan.orders)
    assert any(p.startswith("Rebalanceamento de BOVA11 bloqueado") for p in plan.pending)


@pytest.mark.unit
def test_an_open_crisis_episode_keeps_its_peak_past_the_12_month_window():
    days = pd.bdate_range("2025-01-01", "2026-10-30")
    bova = pd.Series(200.0, index=days)
    bova.loc["2025-06-01":] = 120.0                         # -40% since mid-2025, flat ever since
    plain = indices.crisis_status({"BOVA11": bova}, TODAY, 0.15, 0.30)
    assert plain["status"] == "normal"                       # the old high left the 12-month window
    episode = {"start": "2025-06-02", "peaks": {"BOVA11": 200.0}}
    kept = indices.crisis_status({"BOVA11": bova}, TODAY, 0.15, 0.30, episode)
    assert kept["status"] == "crise" and kept["episode"]["start"] == "2025-06-02"
    bova.iloc[-1] = 190.0
    closed = indices.crisis_status({"BOVA11": bova}, TODAY, 0.15, 0.30, episode)
    assert closed["status"] == "normal" and closed["episode_closed"]["start"] == "2025-06-02"


@pytest.mark.unit
def test_the_letter_audit_flags_amounts_not_in_the_data():
    data = {"loss": -29_386.6, "sales": [18_996.71], "contribution": 3000,
            "pending": ["O plano conta R$ 18.987,13 destas vendas."]}
    letter = ("Perde R$ 29 mil (R$ 29,4 mil), não R$ 50 mil; vendas de R$ 18.996,71; aporte de R$ 3.000; "
              "R$ 36.024,71; as vendas de R$ 18.987,13 do texto.")
    assert director.audit_letter(letter, data) == ["R$ 50 mil", "R$ 36.024,71"]


@pytest.mark.unit
def test_the_execution_order_frees_money_before_spending_it():
    plan = indices.build(_policy(), _wealth(), PRICES, TODAY, 3000)
    steps = index_meeting.execution_steps(plan)
    assert steps[0].startswith("Na corretora, venda primeiro") and "D+2" in steps[0] and "aluguel" not in steps[0]
    assert "PETR4 estão emprestadas" in index_meeting.execution_steps(plan, {"PETR4", "XXXX3"})[0]
    assert any("até as 13h" in x for x in steps) and steps[-1].startswith("Depois de executar")
    m_letter = index_meeting.render(TODAY, _policy(), _wealth(), plan,
                                    director.check_index(director.IndexDecision(brazil_vehicle="BOVA11",
                                                                                brazil_vehicle_reason="padrão"),
                                                         _policy(), set()),
                                    [], 0.0, None, False, no_letter="Sem modelos.", price_asof=date(2026, 10, 30))
    assert "### Ordem de execução" in m_letter and "fechamentos até 30/10/2026" in m_letter
    assert "| Preço ref. |" in m_letter


@pytest.mark.unit
def test_without_a_date_the_bond_rule_rolls_five_years_ahead():
    plan_cfg = IndexPlan(migration_start=date(2026, 11, 1))
    policy = InvestmentPolicy(monthly_expenses=10_000, monthly_contribution=3000, index_plan=plan_cfg)
    plan = indices.build(policy, _wealth(), PRICES, TODAY, 3000)
    bond = next(o for o in plan.orders if o.side == "resgatar" and o.asset == "Tesouro IPCA+ 2045")
    assert "mais de 5 anos" in bond.reason and "régua do plano é a Selic" in bond.reason
    assert any(h.startswith("Tesouro IPCA+ 2029") and "vence em até 5 anos" in h for h in plan.holds)


@pytest.mark.unit
def test_a_single_vehicle_menu_leaves_nothing_to_choose():
    class Capture:
        def with_structured_output(self, schema):
            return self

        def invoke(self, prompt):
            self.prompt = prompt
            return director.IndexDecision(brazil_vehicle="BOVA11", brazil_vehicle_reason="fixo")

    policy = _policy(brazil_menu=["BOVA11"])
    llm = Capture()
    director.ask_index(llm, policy, {}, [], "BOVA11")
    assert "fixed by the investor's policy: return BOVA11" in llm.prompt and "DIVO11 IRR" not in llm.prompt
    assert director.check_index(director.IndexDecision(brazil_vehicle="metade", brazil_vehicle_reason="x"), policy,
                                set()).vehicle == "BOVA11"
    llm2 = Capture()
    director.ask_index(llm2, _policy(), {}, [], "BOVA11")
    assert "DIVO11 IRR" in llm2.prompt


@pytest.mark.unit
def test_a_tiny_partial_sale_waits_for_the_next_month():
    wealth = Wealth(as_of=date(2026, 10, 31), fixed_income=[
        FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                    value=80_000, liquidity_days=1)],
        listed=[Listed(code="PETR4", quantity=474, average_price=30.0), Listed(code="ITSA4", quantity=100)])
    # PETR4 takes R$ 18,960 of the R$ 19,000 room; R$ 40 would sell 4 ITSA4 of 100: it waits instead.
    plan = indices.build(_policy(), wealth, PRICES, TODAY, 3000)
    assert [o.asset for o in plan.orders if o.side == "venda"] == ["PETR4"]
    assert any("ITSA4" in p for p in plan.pending)


@pytest.mark.unit
def test_the_transition_nets_against_the_months_application():
    wealth = Wealth(as_of=date(2026, 10, 31), fixed_income=[
        FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                    value=200_000, liquidity_days=1),
        FixedIncome(name="Tesouro IPCA+ 2045", kind="tesouro_ipca", issuer="Tesouro", indexer="IPCA", value=20_000,
                    maturity=date(2045, 5, 15), liquidity_days=1, applied_on=date(2023, 1, 10), applied_value=20_000)])
    plan = indices.build(_policy(), wealth, PRICES, TODAY, 0)
    assert plan.transfer > 0
    fixed = [o for o in plan.orders if o.sleeve == "renda_fixa" and o.asset != "Tesouro IPCA+ 2045"]
    # The bond's R$ 20,000 goes to Selic less the transition (plus what whole quotas leave over): one application,
    # no redemption of Selic.
    assert [o.side for o in fixed] == ["aplicar"]
    bought = sum(o.value for o in plan.orders if o.side == "compra")
    assert fixed[0].value + bought == pytest.approx(20_000, abs=1.0)
    assert fixed[0].value == pytest.approx(20_000 - plan.transfer, abs=160)


def _mandate(**kw):
    return _policy(equity_min=0.40, **kw)


@pytest.mark.unit
def test_the_equity_share_scales_both_equity_sleeves_and_never_exceeds_the_mix():
    plan = indices.build(_mandate(), _wealth(), PRICES, TODAY, 3000, equity_share=0.45)
    # 91,000 investable plus contribution: 15% Brazil, 30% global, 55% fixed income (plus the reserve).
    assert plan.sleeves["brasil"]["target"] == pytest.approx(0.15 * 91_000)
    assert plan.sleeves["internacional"]["target"] == pytest.approx(0.30 * 91_000)
    assert plan.sleeves["renda_fixa"]["target"] == pytest.approx(0.55 * 91_000 + 60_000)
    assert indices.build(_mandate(), _wealth(), PRICES, TODAY, 3000, equity_share=0.9).equity_share == pytest.approx(0.6)
    with pytest.raises(ValueError):
        IndexPlan(equity_min=0.7)


@pytest.mark.unit
def test_the_director_moves_the_equity_share_within_floor_ceiling_and_step():
    def decide(share):
        return director.IndexDecision(brazil_vehicle="BOVA11", brazil_vehicle_reason="r", equity_share=share)

    assert director.check_index(decide(0.55), _mandate(), set(), 0.60).equity_share == pytest.approx(0.55)
    assert director.check_index(decide(0.30), _mandate(), set(), 0.60).equity_share == pytest.approx(0.50)  # step
    assert director.check_index(decide(0.30), _mandate(), set(), 0.45).equity_share == pytest.approx(0.40)  # floor
    assert director.check_index(decide(0.90), _mandate(), set(), 0.55).equity_share == pytest.approx(0.60)  # mix
    assert director.check_index(decide(None), _mandate(), set(), 0.50).equity_share == pytest.approx(0.50)
    assert director.check_index(decide(0.30), _policy(), set()).equity_share is None    # no mandate, no share
    assert "equity_share:" in director.equity_rule(_mandate(), 0.55) and director.equity_rule(_policy(), None) == ""


@pytest.mark.unit
def test_a_meeting_under_a_mandate_logs_the_share_beside_the_fixed_mix(tmp_path):
    decision = director.IndexDecision(brazil_vehicle="BOVA11", brazil_vehicle_reason="padrão", equity_share=0.5,
                                      equity_share_reason="A Selic real está alta.")
    frames = _frames()
    config = {**index_meeting.DEFAULT_CONFIG, "results_dir": str(tmp_path)}
    with mock.patch.object(index_meeting.b3_market, "histories",
                           side_effect=lambda codes: {c: frames[c] for c in codes if c in frames}), \
            mock.patch.object(tesouro_direto, "offers", side_effect=RuntimeError("offline")), \
            mock.patch.object(index_meeting.analysts, "macro",
                              return_value=index_meeting.analysts.Report("Analista Macro", {})), \
            mock.patch.object(index_meeting.analysts, "Models") as models, \
            mock.patch.object(index_meeting.analysts, "_with_view", side_effect=lambda r, *a, **k: r), \
            mock.patch.object(director, "ask_index", return_value=decision) as ask, \
            mock.patch.object(director, "write_letter", return_value="Carta."):
        models.return_value.spent = 0.0
        models.return_value.budget.by_model = {}
        from tradingagents.committee import meeting

        m = meeting.hold(_mandate(), _wealth(), as_of=TODAY, config=config)
    assert "fixed_income_yields" in ask.call_args.args[2] and ask.call_args.kwargs["previous_share"] is None
    assert "Parcela em ações: **50%**" in m.letter and "o plano fixo é 60%" in m.letter
    log = json.loads((tmp_path / "comite" / "decisoes.jsonl").read_text().splitlines()[-1])
    assert log["equity_share"] == pytest.approx(0.5) and log["equity_share_fixed_mix"] == pytest.approx(0.6)
    assert index_meeting.previous_equity_share(tmp_path / "comite") == pytest.approx(0.5)


@pytest.mark.unit
def test_with_the_reserve_short_the_targets_are_cut_from_what_is_left_after_topping_it_up():
    """Found by the backtest: a contribution that first fills the reserve must not also count in the base the
    equity targets are cut from, and money outside the reserve is investable even while the reserve is short."""
    policy = _policy()                                    # reserve: 6 x R$ 10,000
    wealth = Wealth(as_of=TODAY, fixed_income=[FixedIncome(name="Tesouro Selic 2031", kind="tesouro_selic",
                    issuer="Tesouro", indexer="SELIC", value=20_000, liquidity_days=1)])
    plan = indices.build(policy, wealth, PRICES, TODAY, 100_000)
    # R$ 40,000 tops the reserve up; the base is the other R$ 60,000: 20% Brazil, 40% global, 40% Selic.
    assert plan.reserve_deficit == pytest.approx(40_000) and plan.investable == pytest.approx(0)
    assert plan.sleeves["brasil"]["target"] == pytest.approx(12_000)
    assert plan.sleeves["internacional"]["target"] == pytest.approx(24_000)
    assert plan.sleeves["renda_fixa"]["target"] == pytest.approx(24_000 + 60_000)
    bought = sum(o.value for o in plan.orders if o.side == "compra")
    assert bought <= 36_000 and bought > 35_000
    # Equities held while the reserve is short are investable: they count in the base, not against the reserve.
    wealth.listed = [Listed(code="WRLD11", quantity=100, kind="etf", asset_class="internacional", average_price=100)]
    plan = indices.build(policy, wealth, PRICES, TODAY, 0.0)
    assert plan.investable == pytest.approx(15_000)
