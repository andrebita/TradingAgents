"""The shadow portfolio: every target the routine set, replayed against BOVA11 and DIVO11.

It records only what the routine decided and when (``{"date", "codes"}``); the
performance is replayed from prices each time, so it needs no bookkeeping and
cannot drift. Each target is bought in equal parts at the close of the session
after its date and held, dividends reinvested, until the next target; no costs
or taxes. This is the forward test the backtests could not give: decisions
recorded before their outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


class Shadow:
    def __init__(self, path: Path):
        self.path = path
        self.events: list[dict] = json.loads(path.read_text(encoding="utf-8"))["events"] if path.exists() else []

    def record(self, day: str, codes: list[str]) -> bool:
        """Add a target dated ``day``; a repeat of the last target, or a past date, is not recorded."""
        codes = sorted(codes)
        if self.events and (self.events[-1]["codes"] == codes or day <= self.events[-1]["date"]):
            return False
        self.events.append({"date": day, "codes": codes})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"events": self.events}, indent=1), encoding="utf-8")
        return True

    def curve(self, px: dict[str, pd.DataFrame], sessions: pd.DatetimeIndex) -> pd.Series:
        """Portfolio value (1 at the first entry) on each session since the first target."""
        segments, level = [], 1.0
        for i, event in enumerate(self.events):
            after = sessions[sessions > pd.Timestamp(event["date"])]
            if not len(after):
                break
            entry = after[0]
            if i + 1 < len(self.events):
                later = sessions[sessions > pd.Timestamp(self.events[i + 1]["date"])]
                exit_ = later[0] if len(later) else sessions[-1]
            else:
                exit_ = sessions[-1]
            days = sessions[(sessions >= entry) & (sessions <= exit_)]
            if not len(days):
                break
            curves = []
            for code in event["codes"]:
                if code in px:
                    adj = px[code]["Adj Close"].reindex(days).ffill()
                    if pd.notna(adj.iloc[0]):
                        curves.append(adj / adj.iloc[0])
            value = pd.concat(curves, axis=1).mean(axis=1) if curves else pd.Series(1.0, index=days)
            segment = value * level
            level = float(segment.iloc[-1])
            segments.append(segment if not segments else segment.iloc[1:])
        return pd.concat(segments) if segments else pd.Series(dtype=float)

    def performance(self, px: dict[str, pd.DataFrame], benchmarks: dict[str, pd.Series]) -> dict | None:
        """Return since the first entry, the portfolio's and each benchmark's over the same sessions."""
        sessions = next(iter(benchmarks.values())).index
        curve = self.curve(px, sessions)
        if len(curve) < 2:
            return None
        out = {"since": curve.index[0].date().isoformat(), "to": curve.index[-1].date().isoformat(),
               "portfolio": float(curve.iloc[-1] / curve.iloc[0] - 1)}
        for name, series in benchmarks.items():
            s = series.reindex(curve.index).ffill()
            out[name] = float(s.iloc[-1] / s.iloc[0] - 1) if pd.notna(s.iloc[0]) else None
        return out
