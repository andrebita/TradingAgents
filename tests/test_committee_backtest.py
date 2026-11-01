"""The backtest: the real portfolio from the B3 exports and the committee's plan through past prices. Fictitious
exports and a synthetic market (flat ETFs, a Tesouro Selic growing with the CDI)."""
from datetime import date

import pandas as pd
import pytest

from tradingagents.committee import b3_history as h, backtest as bt
from tradingagents.committee.policy import IndexPlan, InvestmentPolicy

MOVEMENT_HEADERS = list(h.MOVEMENT_COLUMNS)
MOVES = [
    ["Credito", "15/03/2024", "Dividendo", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 100, 0.5, 50.0],
    ["Credito", "20/04/2024", "Bonificação em Ativos", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 10, "-", "-"],
    ["Debito", "20/04/2024", "Fração em Ativos", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 0.5, "-", "-"],
    ["Credito", "21/04/2024", "Leilão de Fração", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 0.5, 10.0, 5.0],
    ["Credito", "01/05/2024", "Atualização", "NEWW11 - FUNDO NOVO", "CORRETORA A", 40, "-", "-"],
    ["Credito", "05/02/2024", "Compra", "Tesouro Selic 2029", "CORRETORA A", 2, 1000.0, 2000.0],
    ["Debito", "05/08/2024", "Venda", "Tesouro Selic 2029", "CORRETORA A", 1, 1100.0, 1100.0],
    ["Debito", "15/09/2024", "Amortização", "Tesouro Educa+ 2026", "CORRETORA A", 1, 80.0, 80.0],
    ["Debito", "01/07/2024", "Cobrança de Taxa Semestral", "Tesouro Selic 2029", "CORRETORA A", 0, "-", 3.0],
    ["Credito", "10/06/2024", "Empréstimo", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 100, "-", 1.5],
    ["Debito", "10/06/2024", "Empréstimo", "BBBB3 - EMPRESA B S/A", "CORRETORA A", 10, "-", 0.5],
    ["Credito", "10/06/2024", "Transferência - Liquidação", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 100, 10.0, 1000.0],
    ["Credito", "11/06/2024", "Transferência", "AAAA3 - EMPRESA A S/A", "CORRETORA B", 100, "-", "-"],
    ["Debito", "11/06/2024", "Transferência", "AAAA3 - EMPRESA A S/A", "CORRETORA A", 100, "-", "-"],
    ["Credito", "01/03/2025", "APLICAÇÃO", "CDB - CDB123 - BANCO X", "BANCO X", 1000, 1.0, 1000.0],
    ["Credito", "12/06/2024", "Dividendo - Transferido", "AAAA3 - EMPRESA A S/A", "CORRETORA B", 100, 0.1, 10.0],
]


def _trades():
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-10", "2024-02-01", "2024-06-03", "2024-06-03"]),
        "side": ["compra", "compra", "venda", "venda"], "code": ["AAAA3", "OLDD11", "OLDD11", "AAAA3"],
        "quantity": [100.0, 50.0, 10.0, 20.0], "value": [1000.0, 5000.0, 1200.0, 300.0]})


@pytest.fixture
def moves(tmp_path):
    path = tmp_path / "movimentacao.xlsx"
    pd.DataFrame(MOVES, columns=MOVEMENT_HEADERS).to_excel(path, index=False)
    return h.read_movements(path)


@pytest.mark.unit
def test_movements_are_read_with_their_sign_and_dashes_as_missing(moves):
    first = moves.iloc[0]
    assert first["code"] == "Tesouro Selic 2029" and first["sign"] == 1.0      # sorted by date
    assert moves["price"].isna().sum() >= 4
    assert h.scope_of("AAAA3") == "listed" and h.scope_of("Tesouro IPCA+ 2045") == "tesouro"
    assert h.scope_of("CDB") is None


@pytest.mark.unit
def test_quantities_follow_trades_events_and_the_conversions_the_investor_lists(moves):
    conv = [h.Conversion(date(2024, 7, 1), "OLDD11", "NEWW11", 40.0)]
    extra = [h.ExtraTrade(date(2024, 3, 1), "RECB15", 10, 100.0)]
    ev = h.listed_events(_trades(), moves, conv, extra, [h.Adjustment(date(2024, 7, 1), "AAAA3", 1)])
    assert h.holdings(ev, "2024-04-30") == {"AAAA3": 109.5, "OLDD11": 50.0, "RECB15": 10.0}
    # The settlement, the transfer between brokers and the lending row do not move quantities; the conversion does.
    assert h.holdings(ev, "2024-07-31") == {"AAAA3": 90.5, "NEWW11": 40.0, "RECB15": 10.0}
    tes = h.tesouro_events(moves)
    assert h.holdings(tes, "2024-12-31", key="bond") == {"Tesouro Selic 2029": 1.0}


@pytest.mark.unit
def test_flows_are_what_came_from_the_pocket_and_went_back_to_it(moves):
    f = h.external_flows(_trades(), moves, [h.ExtraTrade(date(2024, 3, 1), "RECB15", 10, 100.0)])
    by = f.groupby("source")["amount"].sum()
    assert by["negociação"] == pytest.approx(1000 + 5000 - 1200 - 300)
    assert by["oferta"] == 100.0 and by["Compra"] == 2000.0 and by["Venda"] == -1100.0
    assert by["Dividendo"] == -50.0 and by["Leilão de Fração"] == -5.0
    assert by["Amortização"] == -80.0                       # a debit of the bond, paid to the investor
    assert by["Cobrança de Taxa Semestral"] == 3.0
    assert by["Empréstimo"] == pytest.approx(-1.5 + 0.5)    # fee received as lender, paid as borrower
    assert "APLICAÇÃO" not in set(f["source"]) and "Dividendo - Transferido" not in set(f["source"])


@pytest.mark.unit
def test_taxes_on_sales_tesouro_by_lot_and_listed_by_month(moves):
    t = h.tesouro_lots_taxes(h.tesouro_events(moves))
    # One of two bonds bought at R$ 1,000 sold at R$ 1,100 after 182 days: 20% of R$ 100.
    assert t["tax"].iloc[0] == pytest.approx(20.0)
    ev = h.listed_events(_trades(), moves)
    lt = h.listed_taxes(_trades(), ev, fund_codes={"OLDD11"})
    funds = lt[lt["class"] == "fundos"].iloc[0]
    assert funds["gain"] == pytest.approx(1200 - 1000) and funds["tax"] == pytest.approx(40.0)
    stocks = lt[lt["class"] == "acoes"].iloc[0]
    assert stocks["sales"] == 300.0 and stocks["tax"] == 0.0       # within the R$ 20,000 exemption


@pytest.mark.unit
def test_bond_names_meet_the_treasury_price_file():
    assert h.bond_key("Tesouro Selic 2031") == ("Tesouro Selic", 2031)
    assert h.bond_key("Tesouro IPCA+ com Juros Semestrais 2045") == ("Tesouro IPCA+ com Juros Semestrais", 2045)
    assert h.bond_key("Tesouro Educa+ 2026") == ("Tesouro Educa+", 2030)
    with pytest.raises(ValueError):
        h.bond_key("CDB Banco X")


@pytest.mark.unit
def test_returns_money_and_time_weighted():
    values = pd.Series({pd.Timestamp("2024-01-31"): 0.0, pd.Timestamp("2024-02-29"): 1010.0,
                        pd.Timestamp("2024-03-31"): 2030.0})
    flows = pd.DataFrame({"date": pd.to_datetime(["2024-02-01", "2024-03-31"]), "amount": [1000.0, 1000.0]})
    r = h.modified_dietz(values, flows)
    assert r.iloc[1] == pytest.approx(0.0198, abs=1e-3)
    irr = h.xirr([(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1100.0)])
    assert irr == pytest.approx(0.0997, abs=1e-3)
    assert h.drawdown(pd.Series([1.0, 1.2, 0.9, 1.3])) == pytest.approx(-0.25)


def _market(days):
    cdi = pd.Series(0.04, index=days)                                     # % a day
    selic = pd.Series([1000.0 * 1.0004 ** i for i in range(len(days))], index=days)
    rows = []
    for d, pu in selic.items():
        rows.append({"kind": "Tesouro Selic", "maturity": pd.Timestamp("2029-03-01"), "date": d, "rate": 0.1,
                     "buy": pu, "sell": pu, "base": pu})
    prices = h.TesouroPrices(pd.DataFrame(rows))
    closes = {"BOVA11": pd.Series(100.0, index=days), "WRLD11": pd.Series(50.0, index=days)}
    return bt.Market(closes, prices, cdi)


@pytest.mark.unit
def test_the_plan_runs_through_the_months_with_the_investors_money():
    days = pd.bdate_range("2024-01-01", "2024-08-31")
    market = _market(days)
    meetings = h.month_ends(days, "2024-01-01", "2024-08-31")
    policy = InvestmentPolicy(monthly_expenses=1000, reserve_months=6,
                              index_plan=IndexPlan(brazil_menu=["BOVA11"], migration_start=date(2024, 1, 1)))
    flows = pd.DataFrame({"date": pd.to_datetime(["2024-01-05", "2024-05-10"]), "amount": [106_000.0, -30_000.0]})
    r = bt.simulate(policy, flows, market, meetings)
    first = r.months.iloc[0]
    # January: R$ 6,000 to the reserve, then 20/40/40 of the other R$ 100,000 (plus a few days of CDI).
    assert first["brasil"] == pytest.approx(20_000, rel=0.015)
    assert first["internacional"] == pytest.approx(40_000, rel=0.015)
    assert first["selic"] == pytest.approx(46_000, rel=0.015)
    assert first["value"] == pytest.approx(106_000 * 1.0004 ** 19, rel=0.002)
    # May's withdrawal comes out of Tesouro Selic first, with income tax on its gain.
    may = r.months[r.months["date"].dt.month == 5].iloc[0]
    assert may["withdrawal"] == pytest.approx(30_000, rel=0.01) and may["brasil"] == pytest.approx(first["brasil"])
    assert r.book.taxes > 0 and r.book.cash > -1.0
    s = bt.summary(pd.concat([pd.Series({days[0] - pd.Timedelta(days=1): 0.0}), r.months.set_index("date")["value"]]),
                   flows, bt.latent_tax(r.book, market, meetings[-1]), bt.taxes_paid(r))
    assert s["xirr"] > 0 and s["end_value_after_tax"] < s["end_value"]
    # The director's floor: less in equities, the rest in Selic.
    low = bt.simulate(policy, flows, market, meetings, equity_share=0.40)
    assert low.months.iloc[0]["brasil"] == pytest.approx(first["brasil"] * 40 / 60, abs=200)
