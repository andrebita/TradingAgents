"""Banco Central macro vendor: release-lag cut of SGS series, the Focus
publication cutoff, alias resolution, and fall-through to FRED.

All API access is mocked, so these run offline.
"""
import copy
from datetime import date
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import bcb


@pytest.mark.unit
def test_a_daily_value_is_known_on_its_own_date():
    assert bcb.released(date(2026, 9, 14), None, date(2026, 9, 14))
    assert not bcb.released(date(2026, 9, 15), None, date(2026, 9, 14))


@pytest.mark.unit
def test_a_monthly_value_waits_for_its_release_lag():
    # August IPCA, released around 10 September: not yet public on the 11th
    # under a 12-day lag, public from the 12th.
    assert not bcb.released(date(2026, 8, 1), 12, date(2026, 9, 11))
    assert bcb.released(date(2026, 8, 1), 12, date(2026, 9, 12))


@pytest.mark.unit
@pytest.mark.parametrize("as_of, cutoff", [
    (date(2026, 9, 16), date(2026, 9, 11)),   # Wednesday: Monday's report, Friday's survey
    (date(2026, 9, 14), date(2026, 9, 11)),   # Monday: that morning's report
    (date(2026, 9, 13), date(2026, 9, 4)),    # Sunday: the previous Monday's report
])
def test_focus_reads_the_survey_of_the_last_published_report(as_of, cutoff):
    assert bcb.focus_cutoff(as_of) == cutoff


@pytest.mark.unit
def test_sgs_drops_values_not_yet_public():
    rows = [{"data": "01/07/2026", "valor": "4.44"}, {"data": "01/08/2026", "valor": "4.30"}]
    with mock.patch.object(bcb, "_get", return_value=rows):
        out = bcb.get_macro_data("ipca_12m", "2026-09-11", 120)
    assert "**Latest:** 4.44 (2026-07-01)" in out and "4.3" not in out


@pytest.mark.unit
def test_sgs_drops_the_selic_target_filled_past_the_run_date():
    rows = [{"data": "14/09/2026", "valor": "14.00"}, {"data": "02/11/2026", "valor": "13.75"}]
    with mock.patch.object(bcb, "_get", return_value=rows):
        out = bcb.get_macro_data("selic", "2026-09-14", 30)
    assert "**Latest:** 14 (2026-09-14)" in out and "13.75" not in out


@pytest.mark.unit
def test_focus_asks_only_for_published_statistics():
    calls = {}

    def fake(url, params):
        calls.update(params)
        return {"value": [
            {"Data": "2026-09-11", "DataReferencia": "2026", "Mediana": 4.9, "Media": 4.91,
             "DesvioPadrao": 0.22, "numeroRespondentes": 147},
        ]}

    with mock.patch.object(bcb, "_get", side_effect=fake):
        out = bcb.get_macro_data("focus_ipca", "2026-09-16", 30)
    assert "Data le '2026-09-11'" in calls["$filter"]
    assert "| 2026 | 4.9 |" in out


@pytest.mark.unit
def test_synonyms_resolve_and_unknown_indicators_are_passed_on():
    assert bcb._key("PTAX") == "usd_brl" and bcb._key("Brazil inflation") == "ipca_12m"
    with pytest.raises(NoMarketDataError):
        bcb.get_macro_data("cpi", "2026-09-14")


@pytest.mark.unit
def test_the_default_macro_chain_passes_us_indicators_to_fred():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    with mock.patch.dict(router.VENDOR_METHODS["get_macro_indicators"], {"fred": lambda *a, **k: "FRED"}):
        assert router.route_to_vendor("get_macro_indicators", "cpi", "2026-09-14", 365) == "FRED"
