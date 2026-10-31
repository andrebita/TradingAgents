"""Load everything the contest needs, once."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from contest_r5 import data, events
from contest_r5.signals import Signals
from contest_r5.universe import Market

SPOT = Path("/tmp/claude-0/-home-user-TradingAgents/540bb046-2800-529d-a5e8-e913432480d2/scratchpad/cotahist/spot.csv.gz")


def load(spot_path: Path = SPOT):
    spot = data.load_spot(spot_path)
    stocks = data.stocks_of(spot)
    company, sectors = data.companies(stocks)
    market = Market(stocks, company)
    u = json.loads((data.OUT / "universe_codes.json").read_text())
    acts = data.yahoo_actions(sorted(set(u["codes"]) | {p for ps in u["peers"].values() for p in ps}),
                              data.OUT / "cache")
    codes = events.build(stocks, u["codes"], u["peers"], acts)
    dfp = pd.read_csv(data.OUT / "dfp_table.csv.gz", parse_dates=["refer", "received"])
    etf = spot[spot["code"].isin(["BOVA11", "DIVO11"])].pivot_table(index="date", columns="code", values="close",
                                                                      aggfunc="last")
    return market, codes, Signals(market, codes, sectors, dfp), etf, stocks
