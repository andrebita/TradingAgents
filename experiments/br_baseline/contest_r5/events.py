"""Per-code corporate events and total-return index for the contest."""

from __future__ import annotations

import numpy as np
import pandas as pd
from contest_r5 import data


class Code:
    """One code: COTAHIST closes, share factors by date, raw proventos per share by ex-date, and their source."""

    def __init__(self, code: str, close: pd.Series, quantity: pd.Series, yahoo: pd.DataFrame | None, source: str):
        self.code = code
        self.close = close.dropna()
        self._quantity = quantity
        self.source = source                      # "yahoo", "yahoo:<peer>" or "detected"
        self.check = None
        if yahoo is not None and len(yahoo) and self._from_yahoo(yahoo):
            return
        self.source = "detected" if source == "detected" else f"detected ({source} unusable)"
        self.splits, self.proventos = data.detect_splits(close, quantity), pd.Series(dtype=float)

    def _from_yahoo(self, yahoo: pd.DataFrame) -> bool:
        """Events and raw proventos from the ratio of COTAHIST's close to Yahoo's split-adjusted close.

        The ratio is the factor Yahoo applied for later events, so it steps on each event Yahoo knows, listed in
        its split record or not. A step counts as an event only if the traded price jumps the same way that day;
        a step without a jump is a Yahoo error and is ignored. Proventos are Yahoo's amounts times the ratio.
        """
        y = yahoo["Close"].reindex(self.close.index)
        ratio = (self.close / y).dropna()
        if len(ratio) < 60:
            return False
        jumps = self.close / self.close.shift(1)
        # The ratio is flat between events: smooth out day-to-day noise and find where its level moves.
        smooth = ratio.rolling(5, center=True, min_periods=1).median()
        move = np.log(smooth / smooth.shift(1)).abs()
        days = list(move[move > np.log(1.01)].index)
        splits, i = {}, 0
        while i < len(days):
            j = i
            while j + 1 < len(days) and ratio.index.get_loc(days[j + 1]) - ratio.index.get_loc(days[j]) <= 2:
                j += 1
            first, last = ratio.index.get_loc(days[i]), ratio.index.get_loc(days[j])
            before = ratio.iloc[max(first - 6, 0):max(first - 1, 1)].median()
            after = ratio.iloc[last + 1:last + 6].median() if last + 1 < len(ratio) else ratio.iloc[-1]
            shares = before / after
            # The event day: the day the traded price jumped the most within the smeared step.
            span = ratio.index[max(first - 2, 0):last + 2]
            day = (np.log(jumps.reindex(span)).abs()).idxmax() if len(span) else days[i]
            big = abs(np.log(shares)) > np.log(1.08)
            if abs(np.log(shares)) > np.log(1.01) and (not big or abs(np.log(jumps.get(day, 1.0) * shares)) < np.log(1.15)):
                splits[day] = float(shares)
            i = j + 1
        self.splits = splits
        div = yahoo["Dividends"][yahoo["Dividends"] > 0]
        level = ratio.reindex(ratio.index.union(div.index)).ffill().bfill()
        self.proventos = (div * level.reindex(div.index)).dropna().sort_index()
        # How well the accepted events explain the ratio: the share of days within 2% of the implied level.
        implied = pd.Series(1.0, index=ratio.index)
        for day, f in splits.items():
            implied[implied.index < day] *= f
        implied *= ratio.iloc[-20:].median() / implied.iloc[-20:].median()
        self.check = float(((ratio / implied) - 1).abs().lt(0.02).mean())
        if self.check < 0.9:
            # Yahoo's adjustments do not add up for this code (a misadjusted spin-off, say): its events come from
            # the traded prices instead. The proventos keep Yahoo's amounts at that day's ratio.
            self.splits = data.detect_splits(self.close, self._quantity)
            self.source += " (events detected: Yahoo inconsistent)"
        return True

    def total_return(self, sessions: pd.DatetimeIndex) -> pd.Series:
        """Index of price plus reinvested proventos, on ``sessions`` (flat on days without a trade)."""
        c = self.close
        split = pd.Series(self.splits, dtype=float).reindex(c.index).fillna(1.0)
        div = self.proventos.groupby(self.proventos.index).sum()
        # A provento whose ex-date fell on a day without a trade counts on the next traded day.
        div = div.groupby(c.index[np.searchsorted(c.index, div.index).clip(max=len(c) - 1)]).sum() if len(div) else div
        div = div.reindex(c.index).fillna(0.0)
        r = (c * split + div) / c.shift(1) - 1
        tr = (1 + r.fillna(0.0)).cumprod()
        return tr.reindex(sessions).ffill()


def build(stocks: pd.DataFrame, codes: list[str], peers: dict[str, list[str]], actions: dict) -> dict[str, Code]:
    close = stocks.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    qty = stocks.pivot_table(index="date", columns="code", values="quantity", aggfunc="sum")
    out = {}
    for code in codes:
        yahoo, source = actions.get(code), "yahoo"
        if yahoo is None:
            peer = next((p for p in peers.get(code, []) if p in actions), None)
            yahoo, source = (actions[peer], f"yahoo:{peer}") if peer else (None, "detected")
        out[code] = Code(code, close[code], qty[code], yahoo, source)
    return out
