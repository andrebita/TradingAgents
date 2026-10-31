"""The decision calendar and the 50 most traded companies at each decision (protocol §2)."""

from __future__ import annotations

import pandas as pd

FIRST_DECISION, END = pd.Timestamp("2014-06-30"), pd.Timestamp("2026-10-30")


class Market:
    """Daily close (as traded), traded value and quantity per code; the company and class of each code."""

    def __init__(self, stocks: pd.DataFrame, company: dict[str, str]):
        self.close = stocks.pivot_table(index="date", columns="code", values="close", aggfunc="last")
        self.value = stocks.pivot_table(index="date", columns="code", values="volume", aggfunc="sum")
        self.quantity = stocks.pivot_table(index="date", columns="code", values="quantity", aggfunc="sum")
        self.sessions = self.close.index
        self.company = company

    def month_ends(self) -> list[pd.Timestamp]:
        s = self.sessions.to_series()
        return list(s.groupby(s.dt.to_period("M")).max())

    def decisions(self) -> list[pd.Timestamp]:
        return [d for d in self.month_ends() if FIRST_DECISION <= d <= END and d.month in (6, 12)]

    def universe(self, t: pd.Timestamp, size: int = 50) -> pd.DataFrame:
        """``code, company, traded`` of the ``size`` most traded companies at ``t``, by their most traded class."""
        window = self.value.loc[:t].tail(63)
        med = window.median()
        ok = (window.count() >= 42) & (self.close.loc[:t].ffill().iloc[-1].notna())
        last = self.close.loc[:t].apply(lambda c: c.last_valid_index())
        ok &= last.reindex(med.index).fillna(pd.Timestamp("1900-01-01")) >= t - pd.Timedelta(days=10)
        med = med[ok & med.notna()]
        frame = pd.DataFrame({"code": med.index, "traded": med.values})
        frame["company"] = frame["code"].map(self.company)
        frame = frame.sort_values(["company", "traded", "code"], ascending=[True, False, True])
        frame = frame.drop_duplicates("company")
        return frame.sort_values(["traded", "company"], ascending=[False, True]).head(size).reset_index(drop=True)
