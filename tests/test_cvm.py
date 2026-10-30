"""CVM filings vendor: B3 ticker handling, issuer lookup, point-in-time windowing,
re-deliveries, and router fall-through for non-B3 tickers.

The CVM files are built in a temporary cache directory, so these run offline.
"""
import copy
import io
import zipfile
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import cvm

CNPJ = "60.872.504/0001-23"
IPE_HEADER = ("CNPJ_Companhia;Nome_Companhia;Codigo_CVM;Data_Referencia;Categoria;Tipo;Especie;Assunto;"
              "Data_Entrega;Tipo_Apresentacao;Protocolo_Entrega;Versao;Link_Download")


def _ipe_row(delivered, category, subject="", protocol="1", version="1", cnpj=CNPJ, ref=None):
    return (f"{cnpj};ITAU UNIBANCO HOLDING S.A.;19348;{ref or delivered};{category};;;{subject};"
            f"{delivered};AP;{protocol};{version};http://example/{protocol}/{version}")


def _zip(path, member, lines):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, "\n".join(lines).encode("latin-1"))
    path.write_bytes(buffer.getvalue())


@pytest.fixture
def cache(tmp_path):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_cache_dir": str(tmp_path)})
    folder = tmp_path / "cvm"
    folder.mkdir()
    _zip(folder / "fca_cia_aberta_2026.zip", "fca_cia_aberta_valor_mobiliario_2026.csv", [
        "CNPJ_Companhia;Nome_Empresarial;Codigo_Negociacao",
        f"{CNPJ};ITAU UNIBANCO HOLDING S.A.;ITUB4",
        f"{CNPJ};ITAU UNIBANCO HOLDING S.A.;ITUB3",
    ])
    _zip(folder / "ipe_cia_aberta_2026.zip", "ipe_cia_aberta_2026.csv", [
        IPE_HEADER,
        _ipe_row("2026-08-04", "Fato Relevante", "Projeções 2026", protocol="10"),
        _ipe_row("2026-08-04", "Comunicado ao Mercado", "Resultado 2T26", protocol="11"),
        _ipe_row("2026-08-20", "Comunicado ao Mercado", "Resultado 2T26 corrigido", protocol="11", version="2"),
        _ipe_row("2026-08-07", "Valores Mobiliários negociados e detidos (art. 11)", protocol="12"),
        _ipe_row("2026-08-07", "Valores Mobiliários negociados e detidos (art. 11)", protocol="13"),
        _ipe_row("2026-08-12", "Fato Relevante", "Aquisição futura", protocol="14"),
        _ipe_row("2026-08-05", "Fato Relevante", "Outra empresa", protocol="15", cnpj="00.000.000/0001-00"),
        _ipe_row("2026-06-15", "Aviso aos Acionistas", "Pagamento de JCP", protocol="16"),
    ])
    with mock.patch.object(cvm.requests, "get", side_effect=AssertionError("no network in tests")):
        yield tmp_path
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    cvm._read_rows.cache_clear()


@pytest.mark.unit
def test_b3_code_reads_sa_tickers_only():
    assert cvm.b3_code("itub4.sa") == "ITUB4"
    assert cvm.b3_code("PETR4F.SA") == "PETR4"
    assert cvm.b3_code("KLBN11.SA") == "KLBN11"
    assert cvm.b3_code("AAPL") is None


@pytest.mark.unit
def test_a_non_b3_ticker_is_not_served():
    with pytest.raises(NoMarketDataError):
        cvm.get_news("AAPL", "2026-08-01", "2026-08-10")


@pytest.mark.unit
def test_filings_are_served_up_to_the_run_date(cache):
    out = cvm.get_news("ITUB4.SA", "2026-08-01", "2026-08-10")
    assert "ITAU UNIBANCO HOLDING S.A." in out and "Projeções 2026" in out
    # Delivered after the run date: not public yet.
    assert "Aquisição futura" not in out and "corrigido" not in out
    # Another issuer's filing is not this company's.
    assert "Outra empresa" not in out
    # The two holdings reports are counted on one line, not listed.
    assert "2 monthly report(s) of securities held by insiders" in out


@pytest.mark.unit
def test_a_re_delivery_counts_from_its_own_delivery_date(cache):
    before = cvm.get_news("ITUB4.SA", "2026-08-01", "2026-08-10")
    after = cvm.get_news("ITUB4.SA", "2026-08-01", "2026-08-25")
    assert "Resultado 2T26" in before and "corrigido" not in before
    assert "Resultado 2T26 corrigido (re-delivery, version 2)" in after
    assert after.count("Resultado 2T26") == 1


@pytest.mark.unit
def test_an_empty_window_is_a_real_absence_with_earlier_context(cache):
    out = cvm.get_news("ITUB3.SA", "2026-07-01", "2026-07-20")
    assert "real absence of disclosures" in out
    assert "Pagamento de JCP" in out


@pytest.mark.unit
def test_an_unknown_code_is_not_served(cache):
    def cached_only(url, name, year):
        return cache / "cvm" / name if (cache / "cvm" / name).exists() else None

    with mock.patch.object(cvm, "_cached_zip", side_effect=cached_only), pytest.raises(NoMarketDataError):
        cvm.get_news("XXXX3.SA", "2026-08-01", "2026-08-10")


@pytest.mark.unit
def test_the_default_news_chain_passes_other_tickers_to_yahoo():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    with mock.patch.dict(router.VENDOR_METHODS["get_news"], {"yfinance": lambda *a, **k: "YAHOO"}):
        assert router.route_to_vendor("get_news", "AAPL", "2026-08-01", "2026-08-10") == "YAHOO"


STMT_HEADER = ("CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;GRUPO_DFP;MOEDA;ESCALA_MOEDA;ORDEM_EXERC;"
               "DT_INI_EXERC;DT_FIM_EXERC;CD_CONTA;DS_CONTA;VL_CONTA;ST_CONTA_FIXA")


def _dre(refer, version, ini, value, code="3.11", name="Lucro do Período"):
    return f"{CNPJ};{refer};{version};ITAU;19348;G;REAL;MIL;ÚLTIMO;{ini};{refer};{code};{name};{value};S"


def _statements_zip(path, kind, year, index_rows, dre_rows):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{kind}_cia_aberta_{year}.csv", "\n".join(
            ["CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC", *index_rows]
        ).encode("latin-1"))
        archive.writestr(f"{kind}_cia_aberta_DRE_con_{year}.csv",
                         "\n".join([STMT_HEADER, *dre_rows]).encode("latin-1"))
    path.write_bytes(buffer.getvalue())


@pytest.fixture
def statements(cache):
    folder = cache / "cvm"
    _statements_zip(folder / "itr_cia_aberta_2026.zip", "itr", 2026, [
        f"{CNPJ};2026-03-31;1;ITAU;19348;ITR;1;2026-05-05;x",
        # Q2 first delivered 4 Aug, restated 20 Aug: CVM keeps only version 2.
        f"{CNPJ};2026-06-30;1;ITAU;19348;ITR;2;2026-08-04;x",
        f"{CNPJ};2026-06-30;2;ITAU;19348;ITR;3;2026-08-20;x",
    ], [
        _dre("2026-03-31", "1", "2026-01-01", "11875000"),
        _dre("2026-06-30", "2", "2026-04-01", "12324000"),
        _dre("2026-06-30", "2", "2026-01-01", "24199000"),   # year to date: not the quarter
    ])
    for year in (2023, 2024, 2025):
        _statements_zip(folder / f"dfp_cia_aberta_{year}.zip", "dfp", year, [
            f"{CNPJ};{year}-12-31;1;ITAU;19348;DFP;9;{year + 1}-02-10;x",
        ], [_dre(f"{year}-12-31", "1", f"{year}-01-01", "45849000")])
    for name in ("itr_cia_aberta_2025.zip", "dfp_cia_aberta_2026.zip"):
        _statements_zip(folder / name, name[:3], int(name[-8:-4]), [], [])
    return cache


@pytest.mark.unit
def test_statements_serve_the_quarter_on_file_in_r_millions(statements):
    out = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-08-30")
    assert "2026-01-01..2026-03-31" in out and "11,875.0" in out
    assert "2025-01-01..2025-12-31" in out and "45,849.0" in out


@pytest.mark.unit
def test_a_period_restated_after_the_run_date_is_withheld(statements):
    before = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-08-10")
    assert "12,324.0" not in before and "Withheld: 2026-06-30" in before
    after = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-08-30")
    assert "2026-04-01..2026-06-30" in after and "12,324.0" in after and "24,199.0" not in after


@pytest.mark.unit
def test_a_period_not_yet_filed_is_not_served(statements):
    out = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-05-01")
    assert "2026-03-31" not in out and "2025-01-01..2025-12-31" in out
