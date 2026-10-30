"""Probe what each data tool returns for B3 tickers on a historical date.

No LLM is involved: every vendor function the analysts would call is invoked
directly through the router with the same config and date clamping the graph
uses, and the size/first lines of each answer are recorded.

Usage: python experiments/br_baseline/probe_data_coverage.py OUT_DIR DATE [TICKERS...]
"""

import json
import sys
from pathlib import Path

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.default_config import DEFAULT_CONFIG


def classify(text: str) -> str:
    head = text.lstrip()[:200].upper()
    for tag in ("NO_DATA_AVAILABLE", "DATA_UNAVAILABLE", "NOT AVAILABLE", "WITHHELD", "NO NEWS", "NO ARTICLES"):
        if tag in head:
            return tag
    return "OK" if text.strip() else "EMPTY"


def probe(ticker: str, date: str) -> dict:
    from datetime import datetime, timedelta
    d = datetime.strptime(date, "%Y-%m-%d")
    week = (d - timedelta(days=7)).strftime("%Y-%m-%d")
    month = (d - timedelta(days=30)).strftime("%Y-%m-%d")
    calls = {
        "get_stock_data": (ticker, month, date),
        "get_indicators[rsi]": None,
        "get_fundamentals": (ticker, date),
        "get_balance_sheet": (ticker, "quarterly", date),
        "get_income_statement": (ticker, "quarterly", date),
        "get_cashflow": (ticker, "quarterly", date),
        "get_news": (ticker, week, date),
        "get_global_news": (date, 7, 10),
        "get_insider_transactions": (ticker, date),
        "get_macro_indicators[selic]": None,
    }
    out = {}
    for name, args in calls.items():
        try:
            if name == "get_indicators[rsi]":
                text = route_to_vendor("get_indicators", ticker, "rsi", date, 30)
            elif name == "get_macro_indicators[selic]":
                text = route_to_vendor("get_macro_indicators", "interest_rate", date, 90)
            else:
                text = route_to_vendor(name, *args)
            text = str(text)
            out[name] = {"status": classify(text), "chars": len(text),
                         "head": text.strip()[:400]}
        except Exception as exc:  # recorded, not fatal: the probe maps coverage
            out[name] = {"status": f"ERROR {type(exc).__name__}", "chars": 0, "head": str(exc)[:400]}
    return out


def main():
    out_dir, date, *tickers = sys.argv[1:]
    tickers = tickers or ["ITUB4.SA", "PSSA3.SA", "SBSP3.SA", "BBDC4.SA", "B3SA3.SA"]
    results = {}
    with run_config(DEFAULT_CONFIG):
        for t in tickers:
            results[t] = probe(t, date)
            print(t, {k: v["status"] for k, v in results[t].items()}, flush=True)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"data_coverage_{date}.json").write_text(
        json.dumps({"date": date, "results": results}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
