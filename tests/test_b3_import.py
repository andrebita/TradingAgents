"""The B3 trades export: reading it, the average cost and the comparison with the wealth file (fictitious)."""
import pandas as pd
import pytest

from tradingagents.committee import b3_import

ROWS = [
    ["10/01/2024", "Compra", "Mercado à Vista", "-", "CORRETORA A S/A.", "AAAA3", 100, 10.0, 1000.0],
    ["10/01/2024", "Compra", "Mercado Fracionário", "-", "CORRETORA A S/A", "AAAA3F", 50, 13.0, 650.0],
    ["05/03/2024", "Venda", "Mercado à Vista", "-", "CORRETORA A S/A", "AAAA3", 30, 20.0, 600.0],
    ["02/02/2024", "Compra", "Mercado à Vista", "-", "CORRETORA B S/A", "FUND11", 10, 100.0, 1000.0],
    ["03/06/2024", "Compra", "Mercado à Vista", "-", "CORRETORA B S/A", "OLDD4", 20, 5.0, 100.0],
    ["03/06/2024", "Venda", "Mercado à Vista", "-", "CORRETORA B S/A", "BBBB3", 5, 7.0, 35.0],
]


HEADERS = ["Data do Negócio", "Tipo de Movimentação", "Mercado", "Prazo/Vencimento", "Instituição",
           "Código de Negociação", "Quantidade", "Preço", "Valor"]


def _export(tmp_path, rows=ROWS, columns=None):
    path = tmp_path / "negociacao.xlsx"
    pd.DataFrame(rows, columns=columns or HEADERS).to_excel(path, index=False)
    return path


@pytest.mark.unit
def test_the_export_is_read_with_the_fractional_suffix_dropped(tmp_path):
    trades = b3_import.read_trades(_export(tmp_path))
    assert set(trades["code"]) == {"AAAA3", "FUND11", "OLDD4", "BBBB3"}
    assert trades["date"].is_monotonic_increasing and trades.iloc[0]["side"] == "compra"
    assert set(trades["broker"]) == {"CORRETORA A S/A", "CORRETORA B S/A"}


@pytest.mark.unit
def test_another_file_is_refused(tmp_path):
    with pytest.raises(ValueError):
        b3_import.read_trades(_export(tmp_path, [["01/01/2024"]], ["Data"]))


@pytest.mark.unit
def test_a_sale_keeps_the_average_and_renames_and_splits_apply(tmp_path):
    trades = b3_import.read_trades(_export(tmp_path))
    costs = b3_import.average_costs(trades, renames={"OLDD4": "NEWW3"}, splits={"FUND11": [("2024-05-01", 10)]})
    a = costs["AAAA3"]
    # 150 bought for R$ 1,650 (average R$ 11); 30 sold: 120 left at the same average.
    assert a.quantity == 120 and a.average_price == pytest.approx(11.0)
    # A 1:10 split: ten times the quotas at a tenth of the average.
    assert costs["FUND11"].quantity == 100 and costs["FUND11"].average_price == pytest.approx(10.0)
    assert "NEWW3" in costs and "OLDD4" not in costs
    assert costs["BBBB3"].oversold and costs["BBBB3"].average_price is None


@pytest.mark.unit
def test_only_matching_quantities_get_an_average(tmp_path):
    costs = b3_import.average_costs(b3_import.read_trades(_export(tmp_path)))
    r = b3_import.reconcile(costs, {"AAAA3": 120, "FUND11": 12, "CCCC3": 7})
    assert [m["code"] for m in r["matched"]] == ["AAAA3"] and r["matched"][0]["average_price"] == 11.0
    assert r["different"][0]["code"] == "FUND11"
    assert {x["code"] for x in r["only_in_trades"]} == {"OLDD4", "BBBB3"}
    assert r["only_in_wealth"] == [{"code": "CCCC3", "held": 7}]
