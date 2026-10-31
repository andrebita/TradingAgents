"""Brazilian press vendor and the combined B3 news vendor: point-in-time filtering
(publication and last edit), ranking, roundup excerpts, budget, caching, and
how failing sources are reported.

The publishers' WordPress API is mocked, so these run offline.
"""
import copy
from datetime import date
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import b3_news, br_press


def _post(title, content, published, edited=None, link="http://example/post"):
    return {"title": {"rendered": title}, "content": {"rendered": content}, "link": link,
            "date_gmt": published, "modified_gmt": published if edited is None else edited}


class _Response:
    def __init__(self, posts, status=200):
        self.posts, self.status_code = posts, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise br_press.requests.HTTPError(response=self)

    def json(self):
        return self.posts


def _serve(by_site, failing=()):
    """A ``requests.get`` that answers each site with its posts; record the calls."""
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["params"]["page"]))
        name = next(n for n, base in br_press.PORTALS.items() if url.startswith(base))
        if name in failing:
            raise br_press.requests.ConnectionError("reset")
        return _Response(by_site.get(name, []))

    return calls, mock.patch.object(br_press.requests, "get", side_effect=get)


@pytest.fixture
def press(tmp_path):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_cache_dir": str(tmp_path)})
    yield tmp_path
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.mark.unit
def test_only_posts_published_in_the_window_and_not_edited_after_it_are_served(press):
    _, served = _serve({"InfoMoney": [
        _post("Petrobras (PETR4) lucra", "<p>Lucro.</p>", "2026-08-06T22:41:46", "2026-08-07T12:19:26"),
        _post("Petrobras (PETR4) revisada", "<p>Texto com dado posterior.</p>", "2026-08-06T10:00:00",
              "2026-08-09T12:00:00"),
        _post("Petrobras (PETR4) depois", "<p>Amanhã.</p>", "2026-08-08T00:30:00"),
        _post("Petrobras (PETR4) no limite", "<p>Fim do dia.</p>", "2026-08-07T23:59:00"),
        _post("Petrobras (PETR4) agendada", "<p>Pronta antes.</p>", "2026-08-05T09:00:00", "2026-08-04T18:00:00"),
        _post("Petrobras (PETR4) sem data de edição", "<p>?</p>", "2026-08-05T09:00:00", ""),
        _post("Ibovespa fecha em alta", "<p>Sem o código.</p>", "2026-08-05T20:00:00"),
    ]})
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    for kept in ("lucra", "no limite", "agendada"):
        assert f"Petrobras (PETR4) {kept}" in out
    # Edited after the window (its text may carry later facts), published after it,
    # with no edit time, or not naming the code: left out.
    for dropped in ("revisada", "depois", "sem data de edição", "Ibovespa fecha"):
        assert dropped not in out
    assert "An article whose text was edited after 2026-08-07 is left out" in out


@pytest.mark.unit
def test_articles_about_the_company_come_first_and_roundups_are_cut_to_its_paragraphs(press):
    roundup = ("<p>Destaques: Petrobras (PETR4), Vale (VALE3) e Itaú (ITUB4).</p>"
               "<h2>Vale (VALE3) sobe</h2><p>Minério em alta.</p>"
               "<h2>Petrobras (PETR4) lucra R$ 52 bi</h2><p>Lucro acima do consenso.</p><p>Dividendos de R$ 17 bi.</p>"
               "<h2>Itaú (ITUB4) paga JCP</h2><p>JCP de R$ 7 bi.</p>")
    focused = "<p>A Petrobras (PETR4) aprovou dividendos, mais que Vale (VALE3) e Banco do Brasil (BBAS3).</p>"
    _, served = _serve({
        "Money Times": [_post("Petrobras (PETR4), Vale (VALE3) e outros destaques", roundup, "2026-08-07T12:53:00")],
        "InfoMoney": [_post("Petrobras (PETR4) aprova dividendos", focused, "2026-08-06T22:21:00")],
        "BP Money": [_post("Ibovespa fecha em alta", "<p>Puxado por PETR4.</p>", "2026-08-07T20:00:00")],
    })
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    listing = out.split("### Text of the articles")[0]
    assert (listing.index("aprova dividendos") < listing.index("outros destaques")
            < listing.index("Ibovespa fecha em alta"))
    text = out.split("### Text of the articles")[1]
    assert "mais que Vale (VALE3) e Banco do Brasil (BBAS3)" in text   # headlined on the company: whole
    assert "outros destaques (the paragraphs about the company)" in text
    assert "Lucro acima do consenso." in text and "Dividendos de R$ 17 bi." in text
    assert "Minério em alta" not in text and "JCP de R$ 7 bi" not in text


@pytest.mark.unit
def test_the_same_story_from_two_sites_is_listed_once(press):
    story = _post("Petrobras (PETR4) lucra R$ 52 bi", "<p>PETR4.</p>", "2026-08-06T22:00:00")
    _, served = _serve({"InfoMoney": [story], "Seu Dinheiro": [story]})
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert out.split("### Text")[0].count("lucra R$ 52 bi") == 1


@pytest.mark.unit
def test_the_budget_cuts_one_article_and_leaves_the_rest_out(press):
    set_config({"br_press_text_chars": 1_600})
    _, served = _serve({"InfoMoney": [
        _post("Petrobras (PETR4) um", "<p>PETR4 " + "a" * 2_000 + "</p>", "2026-08-06T10:00:00"),
        _post("Petrobras (PETR4) dois", "<p>PETR4 b</p>", "2026-08-05T10:00:00"),
    ]})
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert "_[... cut here: 406 more characters]_" in out
    assert "text of 1 more article(s) left out: length limit reached" in out


@pytest.mark.unit
def test_a_failing_site_is_named_and_none_answering_is_unavailable(press):
    _, served = _serve({"InfoMoney": [_post("Petrobras (PETR4)", "<p>x</p>", "2026-08-06T10:00:00")]},
                       failing={"NeoFeed"})
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert "Unavailable: NeoFeed (ConnectionError)." in out
    _, served = _serve({}, failing=set(br_press.PORTALS))
    with served, pytest.raises(VendorUnavailableError):
        br_press.get_news("PETR4.SA", "2026-07-01", "2026-07-07")


@pytest.mark.unit
def test_a_quiet_window_says_so(press):
    _, served = _serve({})
    with served:
        out = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert "None of these sites has an article naming PETR4 in the window." in out


@pytest.mark.unit
def test_a_settled_window_is_asked_once_and_a_full_page_asks_for_the_next(press):
    full_page = [_post(f"Petrobras (PETR4) {i}", "<p>x</p>", "2026-08-06T10:00:00") for i in range(50)]
    calls, served = _serve({"InfoMoney": full_page})
    with served:
        first = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
        assert ("https://www.infomoney.com.br/wp-json/wp/v2/posts", 2) in calls
        asked = len(calls)
        again = br_press.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert first == again and len(calls) == asked


@pytest.mark.unit
def test_a_live_window_is_asked_again_after_its_ttl(press):
    today = date.today().isoformat()
    calls, served = _serve({})
    with served:
        br_press.get_news("PETR4.SA", today, today)
        asked = len(calls)
        br_press.get_news("PETR4.SA", today, today)
        assert len(calls) == asked
        with mock.patch.object(br_press, "_LIVE_TTL", -1):
            br_press.get_news("PETR4.SA", today, today)
    assert len(calls) == 2 * asked


@pytest.mark.unit
def test_a_non_b3_ticker_is_not_served():
    with pytest.raises(NoMarketDataError):
        br_press.get_news("AAPL", "2026-08-01", "2026-08-07")


# --- The combined vendor ------------------------------------------------------------------

@pytest.mark.unit
def test_b3_news_joins_filings_and_press():
    with mock.patch.object(b3_news.cvm, "get_news", return_value="## Filings\n- fato\n"), \
            mock.patch.object(b3_news.br_press, "get_news", return_value="## Brazilian press\n- artigo\n"):
        out = b3_news.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert out == "## Filings\n- fato\n\n## Brazilian press\n- artigo\n"


@pytest.mark.unit
def test_b3_news_names_a_source_that_is_down_and_skips_one_with_nothing():
    down = VendorUnavailableError("no Brazilian press feed answered")
    with mock.patch.object(b3_news.cvm, "get_news", return_value="## Filings\n"), \
            mock.patch.object(b3_news.br_press, "get_news", side_effect=down):
        out = b3_news.get_news("PETR4.SA", "2026-08-01", "2026-08-07")
    assert "<Brazilian press unavailable: no Brazilian press feed answered>" in out
    unknown = NoMarketDataError("XPTO3.SA", detail="no CVM-registered issuer trades under this code")
    with mock.patch.object(b3_news.cvm, "get_news", side_effect=unknown), \
            mock.patch.object(b3_news.br_press, "get_news", return_value="## Brazilian press\n"):
        assert b3_news.get_news("XPTO3.SA", "2026-08-01", "2026-08-07") == "## Brazilian press\n"
    with mock.patch.object(b3_news.cvm, "get_news", side_effect=unknown), \
            mock.patch.object(b3_news.br_press, "get_news", side_effect=unknown), \
            pytest.raises(NoMarketDataError):
        b3_news.get_news("XPTO3.SA", "2026-08-01", "2026-08-07")


@pytest.mark.unit
def test_the_default_news_chain_serves_b3_tickers_from_both_sources():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    with mock.patch.dict(router.VENDOR_METHODS["get_news"], {"b3": lambda *a: "both sources"}):
        assert router.route_to_vendor("get_news", "PETR4.SA", "2026-08-01", "2026-08-07") == "both sources"
