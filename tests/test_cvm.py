"""CVM filings vendor: B3 ticker handling, issuer lookup, point-in-time windowing,
re-deliveries, and router fall-through for non-B3 tickers.

The CVM files are built in a temporary cache directory, so these run offline.
"""
import copy
import io
import os
import zipfile
from datetime import datetime
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


def _fca(path, year, delivered, code="ITUB4", extra_index=(), extra_rows=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"fca_cia_aberta_{year}.csv", "\n".join([
            "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC",
            f"{CNPJ};{year}-01-01;1;ITAU;19348;FCA;1;{delivered};x",
            *extra_index,
        ]).encode("latin-1"))
        archive.writestr(f"fca_cia_aberta_valor_mobiliario_{year}.csv", "\n".join([
            "CNPJ_Companhia;Data_Referencia;Versao;Nome_Empresarial;Codigo_Negociacao",
            f"{CNPJ};{year}-01-01;1;ITAU UNIBANCO HOLDING S.A.;{code}",
            f"{CNPJ};{year}-01-01;1;ITAU UNIBANCO HOLDING S.A.;ITUB3",
            *extra_rows,
        ]).encode("latin-1"))
    path.write_bytes(buffer.getvalue())


def _cached_only(cache):
    def cached_only(url, name, year, gaps=None):
        return cache / "cvm" / name if (cache / "cvm" / name).exists() else None
    return cached_only


@pytest.fixture
def cache(tmp_path):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    # Lists only; the filings' text has its own tests below.
    set_config({"data_cache_dir": str(tmp_path), "cvm_filing_text_chars": 0})
    folder = tmp_path / "cvm"
    folder.mkdir()
    _fca(folder / "fca_cia_aberta_2026.zip", 2026, delivered="2026-04-09")
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
    assert "complete for the window, so this is an absence of disclosures" in out
    assert "Pagamento de JCP" in out


@pytest.mark.unit
def test_an_unknown_code_is_not_served(cache):
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)), \
            pytest.raises(NoMarketDataError):
        cvm.get_news("XXXX3.SA", "2026-08-01", "2026-08-10")


@pytest.mark.unit
def test_an_fca_form_delivered_after_the_run_date_does_not_name_the_issuer(cache):
    # The 2026 form was delivered on 9 April; before then only earlier forms
    # count, and here there are none.
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)):
        assert cvm.issuer_for("ITUB4", "2026-04-08") is None
        assert cvm.issuer_for("ITUB4", "2026-04-09")[0] == CNPJ


@pytest.mark.unit
def test_a_later_year_form_is_never_read(cache):
    _fca(cache / "cvm" / "fca_cia_aberta_2025.zip", 2025, delivered="2025-04-01", code="OLD3")
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)):
        assert cvm.issuer_for("ITUB4", "2025-12-31") is None   # ITUB4 appears only in the 2026 form


@pytest.mark.unit
def test_a_missing_year_file_makes_an_empty_window_incomplete(cache):
    missing = mock.Mock(status_code=404)
    with mock.patch.object(cvm.requests, "get", return_value=missing):
        out = cvm.get_news("ITUB4.SA", "2026-12-28", "2027-01-04")
    assert "Coverage incomplete:" in out and "CVM has no ipe_cia_aberta_2027.zip file" in out
    assert "may be missing data" in out and "absence of disclosures" not in out


def _fetched_on(cache, day):
    stamp = datetime.fromisoformat(day).timestamp()
    os.utime(cache / "cvm" / "ipe_cia_aberta_2026.zip", (stamp, stamp))


@pytest.mark.unit
def test_a_window_past_the_last_delivery_of_a_fresh_file_is_incomplete(cache):
    # CVM publishes the file about a week behind: fetched on 27 August, it holds
    # deliveries up to 20 August, so a quiet 21-26 August says nothing yet.
    _fetched_on(cache, "2026-08-27")
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)):
        out = cvm.get_news("ITUB4.SA", "2026-08-21", "2026-08-26")
    assert "fetched 2026-08-27, holds deliveries only up to 2026-08-20" in out
    assert "may be missing data" in out and "absence of disclosures" not in out


@pytest.mark.unit
def test_a_window_that_ended_long_before_the_fetch_is_complete(cache):
    # Fetched two months after the window: whatever is missing was never delivered.
    _fetched_on(cache, "2026-10-31")
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)):
        quiet = cvm.get_news("ITUB4.SA", "2026-08-21", "2026-08-26")
        busy = cvm.get_news("ITUB4.SA", "2026-08-01", "2026-08-10")
    assert "complete for the window, so this is an absence of disclosures" in quiet
    assert "Coverage incomplete" not in quiet and "Coverage incomplete" not in busy


def _with_texts(texts, budget=40_000):
    """Serve each filing's text from ``texts`` (by subject); record which were asked for."""
    set_config({"cvm_filing_text_chars": budget})
    asked = []

    def fetch(url):
        asked.append(url)
        return texts.get(url)

    return asked, mock.patch.object(cvm, "_fetch_pdf", side_effect=fetch), \
        mock.patch.object(cvm, "_pdf_text", side_effect=lambda data: data.decode())


@pytest.mark.unit
def test_material_facts_and_notices_carry_their_text_in_order(cache):
    asked, fetch, extract = _with_texts({"http://example/10/1": b"Projecao revisada de margem.",
                                         "http://example/16/1": b"JCP de R$ 0,50 por acao.",
                                         "http://example/11/1": b"Resultado do trimestre."})
    with fetch, extract:
        out = cvm.get_news("ITUB4.SA", "2026-06-01", "2026-08-10")
    text = out.split("### Text of the filings")[1]
    # Material fact first, then the shareholder notice, then the market notice.
    assert text.index("Projecao revisada") < text.index("JCP de R$ 0,50") < text.index("Resultado do trimestre")
    assert "#### 2026-08-04 · Fato Relevante: Projeções 2026" in text
    # Insider holdings reports are listed, never fetched.
    assert sorted(asked) == ["http://example/10/1", "http://example/11/1", "http://example/16/1"]


@pytest.mark.unit
def test_the_text_served_is_the_version_listed_and_is_cached(cache):
    asked, fetch, extract = _with_texts({"http://example/11/2": b"Resultado corrigido."})
    with fetch, extract:
        first = cvm.get_news("ITUB4.SA", "2026-08-15", "2026-08-25")
        again = cvm.get_news("ITUB4.SA", "2026-08-15", "2026-08-25")
    assert "Resultado corrigido." in first and first == again
    assert asked == ["http://example/11/2"]   # the second call read the cache
    assert (cache / "cvm" / "docs" / "11_v2.txt").read_text(encoding="utf-8") == "Resultado corrigido."


@pytest.mark.unit
def test_the_budget_cuts_one_document_and_leaves_the_rest_out(cache):
    long_fact = ("x" * 2_500).encode()
    asked, fetch, extract = _with_texts({"http://example/10/1": long_fact,
                                         "http://example/16/1": b"JCP.", "http://example/11/1": b"Resultado."},
                                        budget=2_100)
    with fetch, extract:
        out = cvm.get_news("ITUB4.SA", "2026-06-01", "2026-08-10")
    assert "x" * 2_100 + "\n_[... cut here: 400 more characters]_" in out
    assert "text of 2 more filing(s) left out: length limit reached" in out
    assert asked == ["http://example/10/1"]   # nothing fetched that would not be shown


@pytest.mark.unit
def test_a_missing_or_scanned_document_is_said_so(cache):
    asked, fetch, extract = _with_texts({"http://example/10/1": b""})
    with fetch, extract:
        out = cvm.get_news("ITUB4.SA", "2026-08-01", "2026-08-10")
    assert "Projeções 2026\n_(no extractable text: a scanned or image-only document)_" in out
    assert "Resultado 2T26\n_(text unavailable: CVM's document server did not return the PDF)_" in out
    assert not (cache / "cvm" / "docs" / "11_v1.txt").exists()   # a failed fetch is retried next time


@pytest.mark.unit
def test_earlier_filings_shown_for_an_empty_window_carry_no_text(cache):
    asked, fetch, extract = _with_texts({})
    with fetch, extract:
        out = cvm.get_news("ITUB4.SA", "2026-07-01", "2026-07-20")
    assert "Pagamento de JCP" in out and "### Text of the filings" not in out and not asked


@pytest.mark.unit
def test_a_dropped_connection_is_retried(cache):
    class Response:
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def raise_for_status(self):
            pass
        def iter_content(self, size):
            yield self.body

    replies = [cvm.requests.ConnectionError("reset"), Response(b"%PDF-1.7 body")]

    def get(url, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    with mock.patch.object(cvm.requests, "get", side_effect=get), mock.patch.object(cvm.time, "sleep"):
        assert cvm._fetch_pdf("http://example/doc") == b"%PDF-1.7 body"
    replies = [Response(b"<html>error</html>")]
    with mock.patch.object(cvm.requests, "get", side_effect=get):
        assert cvm._fetch_pdf("http://example/doc") is None   # not a PDF


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
def test_a_period_restated_after_the_run_date_is_left_out_without_saying_so(statements):
    before = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-08-10")
    # Version 1 was public on 10 Aug but its figures are gone; the restated ones
    # were not public, and telling the agents a restatement came is news from later.
    assert "12,324.0" not in before and "2026-06-30" not in before
    assert "restat" not in before.lower() and "withheld" not in before.lower()
    after = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-08-30")
    assert "2026-04-01..2026-06-30" in after and "12,324.0" in after and "24,199.0" not in after


@pytest.mark.unit
def test_a_period_not_yet_filed_is_not_served(statements):
    out = cvm.get_income_statement("ITUB4.SA", "quarterly", "2026-05-01")
    assert "2026-03-31" not in out and "2025-01-01..2025-12-31" in out


@pytest.mark.unit
def test_a_flow_column_keeps_accounts_reported_only_over_the_longer_span():
    row = ("2026-06-30", "1", "REAL", "MIL")
    rows = [(*row, "2026-01-01", "2026-06-30", "3.01", "Receita", "100"),
            (*row, "2026-04-01", "2026-06-30", "3.99.01.01", "LPA ON", "1.2")]
    assert cvm._span(rows) == ("2026-01-01", "2026-06-30")


@pytest.mark.unit
def test_a_flow_column_takes_the_quarter_when_it_is_complete():
    row = ("2026-06-30", "1", "REAL", "MIL")
    rows = [(*row, ini, "2026-06-30", code, "x", "1") for ini in ("2026-01-01", "2026-04-01")
            for code in ("3.01", "3.11")]
    assert cvm._span(rows) == ("2026-04-01", "2026-06-30")


@pytest.mark.unit
def test_a_public_fca_form_does_not_vouch_for_a_later_form_of_the_same_issuer(cache):
    # The issuer's 2026-01-01 form was public on 9 April; a second form (a later
    # reference date, delivered in June) lists a new code. It must not count in May.
    _fca(cache / "cvm" / "fca_cia_aberta_2026.zip", 2026, delivered="2026-04-09",
         extra_index=[f"{CNPJ};2026-06-01;1;ITAU;19348;FCA;2;2026-06-20;x"],
         extra_rows=[f"{CNPJ};2026-06-01;1;ITAU UNIBANCO HOLDING S.A.;ITUB9"])
    with mock.patch.object(cvm, "_cached_zip", side_effect=_cached_only(cache)):
        assert cvm.issuer_for("ITUB9", "2026-05-15") is None
        assert cvm.issuer_for("ITUB9", "2026-06-20")[0] == CNPJ
        assert cvm.issuer_for("ITUB4", "2026-05-15")[0] == CNPJ
