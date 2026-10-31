"""Dividend-trap filter: can an LLM reading anonymized statements tell which high-yield stocks will cut?

At each semiannual rebalance (June and December, 2012-2026) the candidates are
the 15 highest 24-month dividend yields among the 50 most traded companies that
paid in each of the last three years (``dividend_tilt.py``). For each candidate
the LLM reads its CVM statements as filed by the rebalance date, anonymized: no
name, ticker or dates, every line as a share of revenue or of total assets, so
size gives nothing away. It returns the probability that dividends per share
over the next 12 months fall more than 30% below the last 12 months.

Compared against a rule with no LLM (a loss, net income down more than 30%, or
dividends paid above net income in the latest year) and against no filter:
how well each ranks the cuts that followed, and what each does to the top-10
portfolio. The design is pre-registered in the README ("Filtro de armadilhas").

Usage:
    python experiments/br_baseline/dividend_filter.py --show 2   # print two packages, no LLM
    python experiments/br_baseline/dividend_filter.py --run-id filter_v1 --budget-usd 10 --workers 4
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import random
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import dividend_tilt as tilt
import numpy as np
import pandas as pd
from llm_validation import OverBudget, SpendGuard
from pydantic import BaseModel, Field, field_validator

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import cvm
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_tier_client

HERE = Path(__file__).resolve().parent
FIRST, LAST = "2012-06-01", "2026-06-30"
CANDIDATES, HOLD = 15, 10
CUT = 0.7              # next 12 months' dividends below 70% of the last 12 months' is a cut
FLAG = 50              # the LLM filter drops a candidate at a cut probability of 50% or more
DIVIDENDS_PAID = re.compile(r"dividend|juros sobre (o )?capital", re.I)

INCOME = ("3.01", "3.02", "3.03", "3.04", "3.05", "3.06", "3.07", "3.08", "3.09", "3.11")
BALANCE = ("1", "1.01", "1.02", "2.01", "2.02", "2.03")
CASH = ("6.01", "6.02", "6.03")


# --- Statements, anonymized ------------------------------------------------------------

# cvm._statement_rows reads a whole statement file for one issuer; across hundreds of
# candidate cells that rereads each file hundreds of times. Here each file is read
# once into {cnpj: rows}, with the same filter, and the vendor is pointed at it.
_MEMBERS: dict[tuple[str, str], dict[str, list]] = {}
_PAID: dict[tuple[str, str], dict[tuple[str, str, str], float]] = {}


def _member_rows(path: str, member: str) -> dict[str, list]:
    if (path, member) not in _MEMBERS:
        by_cnpj: dict[str, list] = {}
        with zipfile.ZipFile(path) as archive:
            if member in archive.namelist():
                with archive.open(member) as raw:
                    reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
                    head = {n: i for i, n in enumerate(next(reader))}
                    for row in reader:
                        code = row[head["CD_CONTA"]]
                        if not row[head["ORDEM_EXERC"]].startswith("Ú") or not cvm._depth_ok(code):
                            continue
                        by_cnpj.setdefault(row[head["CNPJ_CIA"]], []).append((
                            row[head["DT_REFER"]], row[head["VERSAO"]], row[head["MOEDA"]], row[head["ESCALA_MOEDA"]],
                            row[head["DT_INI_EXERC"]] if "DT_INI_EXERC" in head else "", row[head["DT_FIM_EXERC"]],
                            code, row[head["DS_CONTA"]], row[head["VL_CONTA"]]))
        _MEMBERS[(path, member)] = by_cnpj
    return _MEMBERS[(path, member)]


def _statement_rows(path: str, mtime: float, member: str, cnpj: str) -> tuple:
    return tuple(_member_rows(path, member).get(cnpj, ()))


cvm._statement_rows = _statement_rows


def _paid_rows(path: str, member: str) -> dict[tuple[str, str, str], float]:
    """``{(cnpj, refer, version): dividends and interest on capital paid, R$}`` of one cash-flow file."""
    if (path, member) not in _PAID:
        paid: dict[tuple[str, str, str], float] = {}
        with zipfile.ZipFile(path) as archive:
            if member in archive.namelist():
                with archive.open(member) as raw:
                    reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
                    head = {n: i for i, n in enumerate(next(reader))}
                    for row in reader:
                        if (not row[head["ORDEM_EXERC"]].startswith("Ú") or not row[head["CD_CONTA"]].startswith("6.03.")
                                or not DIVIDENDS_PAID.search(row[head["DS_CONTA"]])):
                            continue
                        try:
                            amount = abs(float(row[head["VL_CONTA"]])) * cvm._SCALES.get(
                                row[head["ESCALA_MOEDA"]].upper(), 1)
                        except ValueError:
                            continue
                        key = (row[head["CNPJ_CIA"]], row[head["DT_REFER"]], row[head["VERSAO"]])
                        paid[key] = paid.get(key, 0.0) + amount
        _PAID[(path, member)] = paid
    return _PAID[(path, member)]


def _dividends_paid(doc: dict, cnpj: str) -> float | None:
    """Dividends and interest on capital paid in the period (cash flow, R$, positive), if itemized."""
    for method in ("DFC_MI", "DFC_MD"):
        member = f"{doc['kind']}_cia_aberta_{method}_con_{doc['year']}.csv"
        paid = _paid_rows(str(doc["path"]), member).get((cnpj, doc["refer"], doc["version"]))
        if paid is not None:
            return paid
    return None


_VENDOR_SPAN = cvm._span


def _year_to_date(rows: list[tuple]) -> tuple[str, str]:
    """The longest span a statement reports: the year to date in an interim filing."""
    return min({(r[4], r[5]) for r in rows})


def statements(cnpj: str, as_of: date) -> list[dict]:
    """Up to three annual statements and a newer interim one, public by ``as_of``, newest first."""
    annual = cvm._documents(cnpj, as_of, "annual")
    docs = list(annual)
    interim = [d for d in cvm._documents(cnpj, as_of, "quarterly") if d["kind"] == "itr"]
    if interim and (not annual or interim[0]["refer"] > annual[0]["refer"]):
        docs.insert(0, interim[0])
    out = []
    for doc in docs:
        values = {}
        # An interim income statement comes for the quarter and for the year to date; the
        # cash flow only for the year to date. Take the year to date for both.
        cvm._span = _year_to_date if doc["kind"] == "itr" else _VENDOR_SPAN
        try:
            for kind in ("income_statement", "balance_sheet", "cashflow"):
                v, _ = cvm._values(doc, cnpj, cvm._STATEMENTS[kind][1])
                values.update({c: (name, amount) for c, (name, amount, eps) in v.items() if not eps})
        finally:
            cvm._span = _VENDOR_SPAN
        if values:
            months = int(doc["refer"][5:7])
            out.append({"kind": doc["kind"], "refer": doc["refer"], "months": months if doc["kind"] == "itr" else 12,
                        "values": values, "dividends_paid": _dividends_paid(doc, cnpj)})
    return out


def _pct(a, b):
    return None if a is None or not b else 100 * a / b


def _fmt(x):
    return "" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:,.1f}"


def package(sector: str, docs: list[dict], dps_by_year: list[float], dy12: float, dy24: float) -> str:
    """The anonymized evidence: common-size statements, growth, payout and the dividend history."""
    cols = []
    annual_seen = 0
    for d in docs:
        if d["kind"] == "itr":
            cols.append((f"Latest interim, {d['months']} months year-to-date", d))
        else:
            cols.append((f"FY{-annual_seen if annual_seen else 0} (fiscal year{' , most recent' if not annual_seen else ''})"
                         .replace(" , ", ", "), d))
            annual_seen += 1
    head = "| Line | " + " | ".join(c for c, _ in cols) + " |\n|---|" + "---|" * len(cols)

    def table(codes, base_code, title):
        rows = []
        for code in codes:
            names = [d["values"][code][0] for _, d in cols if code in d["values"]]
            if not names:
                continue
            cells = [_fmt(_pct(d["values"].get(code, (None, None))[1], d["values"].get(base_code, (None, None))[1]))
                     for _, d in cols]
            rows.append(f"| {code} {names[0]} | " + " | ".join(cells) + " |")
        return f"### {title}\n{head}\n" + "\n".join(rows)

    annual = [d for _, d in cols if d["kind"] == "dfp"]
    growth = []
    for code, label in (("3.01", "Revenue"), ("3.11", "Net income"), ("2.03", "Equity"), ("1", "Total assets")):
        cells = []
        for i, d in enumerate(annual):
            prev = annual[i + 1]["values"].get(code, (None, None))[1] if i + 1 < len(annual) else None
            cur = d["values"].get(code, (None, None))[1]
            cells.append(_fmt(100 * (cur / prev - 1)) if cur is not None and prev and prev > 0 else "")
        growth.append(f"| {label} | " + " | ".join(cells) + " |")
    payout = []
    for d in annual:
        ni = d["values"].get("3.11", (None, None))[1]
        payout.append(_fmt(_pct(d["dividends_paid"], ni)) if d["dividends_paid"] is not None and ni and ni > 0 else
                      ("n/a (loss)" if ni is not None and ni <= 0 else ""))
    annual_labels = [c for c, d in cols if d["kind"] == "dfp"]
    dps = ", ".join(f"{v:.0f}" for v in dps_by_year)
    return (f"Sector: {sector or 'not stated'}. Brazilian listed company, figures as filed with the securities "
            "regulator, consolidated. Every line is a percentage: income statement and cash flow lines of revenue "
            "(line 3.01), balance sheet lines of total assets (line 1). Account names are in Portuguese. The interim "
            "column covers the year to date, so its lines are comparable as shares of revenue only.\n\n"
            + table(INCOME, "3.01", "Income statement (% of revenue)") + "\n\n"
            + table(BALANCE, "1", "Balance sheet (% of total assets)") + "\n\n"
            + table(CASH, "3.01", "Cash flow (% of revenue)") + "\n\n"
            + "### Year-on-year growth of the fiscal years (%)\n| Line | " + " | ".join(annual_labels) + " |\n|---|"
            + "---|" * len(annual_labels) + "\n" + "\n".join(growth) + "\n\n"
            + "### Dividends and interest on capital paid (cash flow) as % of net income\n| | "
            + " | ".join(annual_labels) + " |\n|---|" + "---|" * len(annual_labels) + "\n| Payout | "
            + " | ".join(payout) + " |\n\n"
            + f"### Dividends per share by calendar year, oldest to latest 12 months (latest = 100)\n{dps}\n\n"
            + f"Dividend yield over the last 12 months: {100 * dy12:.1f}%; average over the last 24 months: "
              f"{100 * dy24:.1f}% a year.\n")


def rule_flags(docs: list[dict]) -> int:
    """Warning signs with no LLM: a loss, net income down over 30%, dividends paid above net income."""
    annual = [d for d in docs if d["kind"] == "dfp"]
    if not annual:
        return 0
    ni = annual[0]["values"].get("3.11", (None, None))[1]
    prev = annual[1]["values"].get("3.11", (None, None))[1] if len(annual) > 1 else None
    flags = int(ni is not None and ni <= 0)
    flags += int(ni is not None and prev is not None and prev > 0 and ni < 0.7 * prev)
    flags += int(ni is not None and ni > 0 and (annual[0]["dividends_paid"] or 0) > ni)
    return flags


# --- The LLM's answer ---------------------------------------------------------------------

class TrapAssessment(BaseModel):
    """The dividend-sustainability read of one anonymized company."""

    key_points: list[str] = Field(description="Three to five facts from the statements that drive the estimate.")
    probability_cut: float = Field(
        description="Probability in percent (0-100) that dividends per share over the next 12 months will be at "
                    "least 30% lower than over the last 12 months.")

    @field_validator("probability_cut")
    @classmethod
    def _in_range(cls, v):
        if not 0 <= v <= 100:
            raise ValueError("probability must be between 0 and 100 percent")
        return v


class Identity(BaseModel):
    guess: str = Field(description="The company you think this is, or 'unknown'.")
    confidence: float = Field(description="Confidence in percent, 0-100.")


PROMPT = """You are a credit and dividend analyst. The company below is anonymized: no name, no dates, figures as percentages. Judge only from what is shown; do not try to identify the company.

How likely is it that its dividends per share over the next 12 months will be at least 30% lower than over the last 12 months? Weigh earnings trend and quality, payout against earnings and cash generation, leverage and liquidity, and any one-off that inflated the recent payout. Most companies that paid steadily keep paying; reserve high probabilities for clear warning signs.

{package}"""

PROBE = """The company below is anonymized. Can you tell which Brazilian listed company it is? Answer 'unknown' unless you are fairly sure.

{package}"""


# --- Data assembly ------------------------------------------------------------------------

def dividends_between(frame: pd.DataFrame, start, end) -> float:
    div = frame["Dividends"]
    return float(div[(div.index > start) & (div.index <= end)].sum())


def candidates(codes, sectors, px, sessions) -> list[dict]:
    ends = sessions.to_series().groupby(sessions.to_period("M")).max()
    dates = [t for t in ends if pd.Timestamp(FIRST) <= t <= pd.Timestamp(LAST) and t.month in (6, 12)]
    out = []
    for i, t in enumerate(dates):
        later = ends[ends > t]
        nxt = dates[i + 1] if i + 1 < len(dates) else later.iloc[min(6, len(later)) - 1]
        entry, after = sessions[sessions > t][0], sessions[sessions > nxt]
        if not len(after):
            break
        snap = tilt.snapshot(codes, px, t, 50)
        pool = snap[snap["constant"]].nlargest(CANDIDATES, "dy24")
        for _, row in pool.iterrows():
            frame = px[row["code"]]
            adj = frame["Adj Close"].reindex(sessions[(sessions >= entry) & (sessions <= after[0])]).ffill()
            last12 = dividends_between(frame, t - pd.DateOffset(months=12), t)
            next12 = dividends_between(frame, t, t + pd.DateOffset(months=12))
            years = [dividends_between(frame, t - pd.DateOffset(months=12 * (k + 1)), t - pd.DateOffset(months=12 * k))
                     for k in range(4)][::-1]
            out.append({"t": t.date().isoformat(), "cnpj": row["cnpj"], "code": row["code"], "dy24": row["dy24"],
                        "dy12": row["dy12"], "dps_index": [100 * y / years[-1] if years[-1] else 0 for y in years],
                        "ret": float(adj.iloc[-1] / adj.iloc[0] - 1) if pd.notna(adj.iloc[0]) else None,
                        "cut": (next12 < CUT * last12) if t + pd.DateOffset(months=12) <= sessions[-1] else None,
                        "sector": tilt.sector_at(sectors.get(row["cnpj"], {}), t.year)})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "dividend_filter"))
    p.add_argument("--run-id", default="filter_v1")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--effort", default="low")
    p.add_argument("--budget-usd", type=float, default=10.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=0, help="only the first N candidates (a smoke test)")
    p.add_argument("--show", type=int, default=0, help="print N packages and stop (no LLM)")
    p.add_argument("--probe", type=int, default=30, help="packages asked to name the company (leakage check)")
    args = p.parse_args()
    out = Path(args.out)
    cache = HERE / "results" / "dividends" / "cache"
    out.mkdir(parents=True, exist_ok=True)

    config = {**copy.deepcopy(DEFAULT_CONFIG), "data_cache_dir": str(cache), "llm_provider": "anthropic",
              "deep_think_llm": args.model, "anthropic_effort": args.effort}
    codes, sectors = tilt.fca(cache)
    px = tilt.prices(set().union(*codes.values()), cache)
    sessions = tilt.series("^BVSP", cache).loc["2008-01-01":].index
    cells_path = out / "candidates.json"
    if cells_path.exists():
        cells = json.loads(cells_path.read_text(encoding="utf-8"))
    else:
        cells = candidates(codes, sectors, px, sessions)
        with run_config(config):
            for c in cells:
                docs = statements(c["cnpj"], date.fromisoformat(c["t"]))
                c["rule_flags"] = rule_flags(docs)
                c["package"] = package(c["sector"], docs, c["dps_index"], c["dy12"], c["dy24"]) if docs else None
        cells_path.write_text(json.dumps(cells, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(cells)} candidate cells, {sum(c['package'] is not None for c in cells)} with statements", flush=True)
    if args.show:
        for c in random.Random(1).sample([c for c in cells if c["package"]], args.show):
            print("\n" + "=" * 80 + "\n" + c["package"])
        return

    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("TRADINGAGENTS_CLAUDE_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["TRADINGAGENTS_CLAUDE_KEY"]
    guard = SpendGuard(args.budget_usd, out / f"{args.run_id}_calls.jsonl", fallback_model=args.model)
    llm = create_tier_client(config, "deep", callbacks=[guard]).get_llm()
    assess, identify = llm.with_structured_output(TrapAssessment), llm.with_structured_output(Identity)
    answers_path, lock = out / f"{args.run_id}_answers.jsonl", threading.Lock()
    done = set()
    if answers_path.exists():
        done = {(r["t"], r["cnpj"], r["task"]) for r in map(json.loads, answers_path.read_text().splitlines())
                if r.get("status") == "ok"}
    todo = [c for c in cells if c["package"]][:args.limit or None]
    probes = {(c["t"], c["cnpj"]) for c in random.Random(7).sample(todo, min(args.probe, len(todo)))}

    def ask(c):
        for task in ("assess", "identify") if (c["t"], c["cnpj"]) in probes else ("assess",):
            if (c["t"], c["cnpj"], task) in done:
                continue
            row = {"t": c["t"], "cnpj": c["cnpj"], "code": c["code"], "task": task}
            try:
                if task == "assess":
                    answer = assess.invoke(PROMPT.format(package=c["package"]),
                                           config={"metadata": {"langgraph_node": "trap"}})
                else:
                    answer = identify.invoke(PROBE.format(package=c["package"]),
                                             config={"metadata": {"langgraph_node": "probe"}})
                row.update(status="ok", **answer.model_dump())
            except OverBudget:
                raise
            except Exception as exc:
                row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            with lock, answers_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    stopped = None
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, future in enumerate([pool.submit(ask, c) for c in todo]):
                future.result()
                if i % 25 == 0:
                    print(f"[{i}/{len(todo)}] US$ {guard.total():.3f}", flush=True)
    except OverBudget as exc:
        stopped = str(exc)
    print(json.dumps({"cost_usd": round(guard.total(), 4), "calls_by_node": guard.by_node, "stopped": stopped},
                     indent=2))


if __name__ == "__main__":
    main()
