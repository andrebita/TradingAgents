"""The Supabase store, against a fake REST server: the requests it makes and how it reads the answers."""
import json

import pytest

from tradingagents.committee import store
from tradingagents.committee.wealth import Listed, Wealth


class FakeResponse:
    def __init__(self, method, url, status=200, body=None):
        self.status_code, self.text = status, json.dumps(body) if body is not None else ""
        self.url = url
        self.request = type("R", (), {"method": method})()

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, answers):
        self.headers, self.calls, self.answers = {}, [], list(answers)

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        status, body = self.answers.pop(0) if self.answers else (200, [])
        return FakeResponse(method, url, status, body)

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def patch(self, url, **kw):
        return self._do("PATCH", url, **kw)


@pytest.mark.unit
def test_the_store_needs_both_variables(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_SUPABASE_URL", raising=False)
    monkeypatch.setenv("TRADINGAGENTS_SUPABASE_KEY", "sb_secret_x")
    with pytest.raises(store.StoreError):
        store.Store.from_env()


@pytest.mark.unit
def test_the_latest_policy_and_wealth_are_read_newest_first():
    s = FakeSession([(200, [{"body": {"monthly_expenses": 1000}}]),
                     (200, [{"body": {"as_of": "2026-10-30", "listed": [{"code": "AAAA3", "quantity": 5}]}}])])
    st = store.Store("https://x.supabase.co", "sb_secret_x", s)
    assert st.latest_policy().monthly_expenses == 1000
    assert st.latest_wealth().listed[0].code == "AAAA3"
    method, url, kw = s.calls[0]
    assert url.endswith("/rest/v1/policies") and kw["params"]["order"] == "valid_from.desc,id.desc"
    assert s.headers["apikey"] == "sb_secret_x" and s.headers["Authorization"] == "Bearer sb_secret_x"


@pytest.mark.unit
def test_a_meeting_is_saved_with_its_orders_as_proposals():
    s = FakeSession([(201, [{"id": 7}]), (201, [])])
    st = store.Store("https://x.supabase.co", "k", s)
    record = {"date": "2026-11-01", "mode": "indices", "cost_usd": 0.5,
              "plan": {"orders": [{"side": "compra", "asset": "BOVA11", "value": 100.0, "quantity": 1}]}}
    assert st.save_meeting(record, letter_url="https://docs") == 7
    orders = json.loads(s.calls[1][2]["data"])
    assert s.calls[1][1].endswith("/orders") and orders == [
        {"meeting_id": 7, "side": "compra", "asset": "BOVA11", "quantity": 1, "value": 100.0, "tax": None,
         "reason": None}]


@pytest.mark.unit
def test_pending_items_keep_their_row_and_costs_replace_theirs():
    s = FakeSession([(201, []), (201, []), (200, [])])
    st = store.Store("https://x.supabase.co", "k", s)
    st.upsert_pending([{"key": "a", "owner": "investidor", "text": "t"}])
    st.save_average_costs([{"code": "AAAA3", "quantity": 1, "average_price": 2, "total_cost": 2, "method": "m"}])
    st.resolve_pending("a", "feito")
    assert s.calls[0][2]["params"] == {"on_conflict": "key"} and "ignore-duplicates" in s.calls[0][2]["headers"]["Prefer"]
    assert s.calls[1][2]["params"] == {"on_conflict": "code"} and "merge-duplicates" in s.calls[1][2]["headers"]["Prefer"]
    assert s.calls[2][0] == "PATCH" and s.calls[2][2]["params"] == {"key": "eq.a"}


@pytest.mark.unit
def test_an_http_error_is_raised_with_its_reason():
    s = FakeSession([(401, {"message": "Invalid API key"})])
    st = store.Store("https://x.supabase.co", "k", s)
    with pytest.raises(store.StoreError, match="HTTP 401"):
        st.save_wealth(Wealth(listed=[Listed(code="AAAA3", quantity=1)]), "teste")
