"""The committee's state in Supabase (review 6): the tables of ``supabase/migrations``, through the REST API.

The investor's own project holds the data; nothing personal is in the repository. The code reaches it with two
environment variables, kept outside the repository:

- ``TRADINGAGENTS_SUPABASE_URL``: the project's URL (``https://<ref>.supabase.co``);
- ``TRADINGAGENTS_SUPABASE_KEY``: a secret key (``sb_secret_...``). Every table has row level security on and no
  policy, so a publishable key reads nothing; the secret key bypasses it and must never reach a browser.
"""

from __future__ import annotations

import json
import os
from datetime import date

import requests

from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import Wealth

TIMEOUT = 30


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, url: str, key: str, session: requests.Session | None = None):
        if not url or not key:
            raise StoreError("Supabase URL and key are required")
        self.base = url.rstrip("/") + "/rest/v1"
        self.http = session or requests.Session()
        self.http.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"})

    @classmethod
    def from_env(cls, session: requests.Session | None = None) -> Store:
        url, key = os.environ.get("TRADINGAGENTS_SUPABASE_URL"), os.environ.get("TRADINGAGENTS_SUPABASE_KEY")
        if not url or not key:
            raise StoreError("set TRADINGAGENTS_SUPABASE_URL and TRADINGAGENTS_SUPABASE_KEY to use the store")
        return cls(url, key, session)

    # -- REST primitives --------------------------------------------------------------------------------
    def _check(self, r: requests.Response):
        if r.status_code >= 400:
            raise StoreError(f"{r.request.method} {r.url.split('?')[0]}: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.text else []

    def select(self, table: str, **params) -> list[dict]:
        return self._check(self.http.get(f"{self.base}/{table}", params=params, timeout=TIMEOUT))

    def insert(self, table: str, rows: list[dict] | dict, *, on_conflict: str | None = None,
               ignore_duplicates: bool = False) -> list[dict]:
        prefer = ["return=representation"]
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
            prefer.append("resolution=ignore-duplicates" if ignore_duplicates else "resolution=merge-duplicates")
        r = self.http.post(f"{self.base}/{table}", params=params, data=json.dumps(rows, default=str),
                           headers={"Prefer": ",".join(prefer)}, timeout=TIMEOUT)
        return self._check(r)

    def update(self, table: str, values: dict, **match) -> list[dict]:
        params = {k: f"eq.{v}" for k, v in match.items()}
        r = self.http.patch(f"{self.base}/{table}", params=params, data=json.dumps(values, default=str),
                            headers={"Prefer": "return=representation"}, timeout=TIMEOUT)
        return self._check(r)

    # -- The committee's state --------------------------------------------------------------------------
    def latest_policy(self) -> InvestmentPolicy:
        rows = self.select("policies", select="body", order="valid_from.desc,id.desc", limit=1)
        if not rows:
            raise StoreError("no policy in the store")
        return InvestmentPolicy.model_validate(rows[0]["body"])

    def latest_wealth(self) -> Wealth:
        rows = self.select("wealth_snapshots", select="body", order="as_of.desc,id.desc", limit=1)
        if not rows:
            raise StoreError("no wealth snapshot in the store")
        return Wealth.model_validate(rows[0]["body"])

    def save_wealth(self, wealth: Wealth, source: str, note: str = "") -> int:
        row = {"as_of": wealth.as_of or date.today(), "source": source, "note": note or None,
               "body": json.loads(wealth.model_dump_json(exclude_none=True))}
        return self.insert("wealth_snapshots", row)[0]["id"]

    def save_average_costs(self, rows: list[dict]) -> None:
        """Rows: code, quantity, average_price, total_cost, method, assumptions. Replaces each code's row."""
        if rows:
            self.insert("average_costs", rows, on_conflict="code")

    def save_meeting(self, record: dict, *, letter_md: str | None = None, letter_url: str | None = None) -> int:
        """The meeting's log line, and its orders as 'proposta'."""
        meeting = self.insert("meetings", {"meeting_date": record["date"], "mode": record.get("mode", "indices"),
                                           "record": record, "letter_md": letter_md, "letter_url": letter_url,
                                           "cost_usd": record.get("cost_usd")})[0]
        orders = [{"meeting_id": meeting["id"], "side": o["side"], "asset": o["asset"], "quantity": o.get("quantity"),
                   "value": o["value"], "tax": o.get("tax"), "reason": o.get("reason")}
                  for o in (record.get("plan") or {}).get("orders", [])]
        if orders:
            self.insert("orders", orders)
        return meeting["id"]

    def open_pending(self) -> list[dict]:
        return self.select("pending_items", select="key,owner,text,due_on,opened_on", status="eq.aberta",
                           order="opened_on.asc,id.asc")

    def upsert_pending(self, items: list[dict]) -> None:
        """Items: key, owner, text, and optionally due_on, meeting_id, opened_on. A known key keeps its row."""
        rows = [{"opened_on": date.today(), **i} for i in items]
        if rows:
            self.insert("pending_items", rows, on_conflict="key", ignore_duplicates=True)

    def resolve_pending(self, key: str, resolution: str, status: str = "resolvida") -> None:
        self.update("pending_items", {"status": status, "resolution": resolution, "resolved_on": date.today(),
                                      "updated_at": "now()"}, key=key)
