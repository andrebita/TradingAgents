"""A company's CVM statements as an anonymized, common-size package for the dividend-cut read.

No name, ticker or dates: every income-statement and cash-flow line as a share of
revenue, every balance-sheet line as a share of total assets, so size gives the
company away no more than its name would. The same package the backtest scored
(``experiments/br_baseline/dividend_filter.py``): in 30 identification probes the
model named one company.

Statements are those public on the date asked (``cvm._documents``): up to three
annual filings and a newer interim one, read over the full year or the year to
date.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date

import numpy as np

from tradingagents.dataflows.vendors import cvm

INCOME = ("3.01", "3.02", "3.03", "3.04", "3.05", "3.06", "3.07", "3.08", "3.09", "3.11")
BALANCE = ("1", "1.01", "1.02", "2.01", "2.02", "2.03")
CASH = ("6.01", "6.02", "6.03")
DIVIDENDS_PAID = re.compile(r"dividend|juros sobre (o )?capital", re.I)

# Each statement file is read once into {cnpj: rows}; the vendor's own reader serves
# one issuer per pass, which a portfolio's ten issuers would repeat ten times.
_ROWS: dict[tuple[str, str], dict[str, list]] = {}
_PAID: dict[tuple[str, str], dict[tuple[str, str, str], float]] = {}


def _member(path: str, member: str) -> dict[str, list]:
    if (path, member) not in _ROWS:
        by_cnpj: dict[str, list] = {}
        with zipfile.ZipFile(path) as archive:
            if member in archive.namelist():
                with archive.open(member) as raw:
                    reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
                    head = {n: i for i, n in enumerate(next(reader))}
                    for row in reader:
                        code = row[head["CD_CONTA"]]
                        if row[head["ORDEM_EXERC"]].startswith("Ú") and cvm._depth_ok(code):
                            by_cnpj.setdefault(row[head["CNPJ_CIA"]], []).append((
                                row[head["DT_REFER"]], row[head["VERSAO"]], row[head["MOEDA"]],
                                row[head["ESCALA_MOEDA"]], row[head["DT_INI_EXERC"]] if "DT_INI_EXERC" in head else "",
                                row[head["DT_FIM_EXERC"]], code, row[head["DS_CONTA"]], row[head["VL_CONTA"]]))
        _ROWS[(path, member)] = by_cnpj
    return _ROWS[(path, member)]


def _values(doc: dict, cnpj: str, members: tuple[str, ...]) -> dict[str, tuple[str, float]]:
    """``{code: (name, R$)}`` over the widest span: the full year, or the year to date."""
    for basis in ("con", "ind"):
        values = {}
        for member in members:
            rows = [r for r in _member(str(doc["path"]), f"{doc['kind']}_cia_aberta_{member}_{basis}_{doc['year']}.csv")
                    .get(cnpj, ()) if r[0] == doc["refer"] and r[1] == doc["version"]]
            if not rows:
                continue
            span = min((r[4], r[5]) for r in rows)
            for _, _, currency, scale, ini, end, code, name, raw in rows:
                if (ini, end) != span or code.startswith("3.99") or currency.upper() != "REAL" \
                        or scale.upper() not in cvm._SCALES:
                    continue
                try:
                    values[code] = (name, float(raw) * cvm._SCALES[scale.upper()])
                except ValueError:
                    continue
            if members[0].startswith("DFC") and values:
                break
        if values:
            return values
    return {}


def _dividends_paid(doc: dict, cnpj: str) -> float | None:
    """Dividends and interest on capital paid in the period (cash flow, R$), if itemized."""
    for method in ("DFC_MI", "DFC_MD"):
        member = f"{doc['kind']}_cia_aberta_{method}_con_{doc['year']}.csv"
        key = (str(doc["path"]), member)
        if key not in _PAID:
            paid: dict[tuple[str, str, str], float] = {}
            with zipfile.ZipFile(doc["path"]) as archive:
                if member in archive.namelist():
                    with archive.open(member) as raw:
                        reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
                        head = {n: i for i, n in enumerate(next(reader))}
                        for row in reader:
                            if (row[head["ORDEM_EXERC"]].startswith("Ú") and row[head["CD_CONTA"]].startswith("6.03.")
                                    and DIVIDENDS_PAID.search(row[head["DS_CONTA"]])):
                                try:
                                    amount = abs(float(row[head["VL_CONTA"]])) * cvm._SCALES.get(
                                        row[head["ESCALA_MOEDA"]].upper(), 1)
                                except ValueError:
                                    continue
                                k = (row[head["CNPJ_CIA"]], row[head["DT_REFER"]], row[head["VERSAO"]])
                                paid[k] = paid.get(k, 0.0) + amount
            _PAID[key] = paid
        found = _PAID[key].get((cnpj, doc["refer"], doc["version"]))
        if found is not None:
            return found
    return None


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
        for kind in ("income_statement", "balance_sheet", "cashflow"):
            values.update(_values(doc, cnpj, cvm._STATEMENTS[kind][1]))
        if values:
            out.append({"kind": doc["kind"], "refer": doc["refer"],
                        "months": int(doc["refer"][5:7]) if doc["kind"] == "itr" else 12,
                        "values": values, "dividends_paid": _dividends_paid(doc, cnpj)})
    return out


def _pct(a, b):
    return None if a is None or not b else 100 * a / b


def _fmt(x) -> str:
    return "" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:,.1f}"


def package(sector: str, docs: list[dict], dps_index: list[float], dy12: float, dy24: float) -> str:
    """The anonymized evidence: common-size statements, growth, payout and the dividend history."""
    cols, annual_seen = [], 0
    for d in docs:
        if d["kind"] == "itr":
            cols.append((f"Latest interim, {d['months']} months year-to-date", d))
        else:
            cols.append((f"FY{-annual_seen} (fiscal year, most recent)" if not annual_seen
                         else f"FY{-annual_seen} (fiscal year)", d))
            annual_seen += 1
    head = "| Line | " + " | ".join(c for c, _ in cols) + " |\n|---|" + "---|" * len(cols)

    def table(codes, base, title):
        rows = []
        for code in codes:
            names = [d["values"][code][0] for _, d in cols if code in d["values"]]
            if names:
                cells = [_fmt(_pct(d["values"].get(code, (None, None))[1], d["values"].get(base, (None, None))[1]))
                         for _, d in cols]
                rows.append(f"| {code} {names[0]} | " + " | ".join(cells) + " |")
        return f"### {title}\n{head}\n" + "\n".join(rows)

    annual = [d for _, d in cols if d["kind"] == "dfp"]
    labels = [c for c, d in cols if d["kind"] == "dfp"]
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
        payout.append(_fmt(_pct(d["dividends_paid"], ni)) if d["dividends_paid"] is not None and ni and ni > 0
                      else ("n/a (loss)" if ni is not None and ni <= 0 else ""))
    return (f"Sector: {sector or 'not stated'}. Brazilian listed company, figures as filed with the securities "
            "regulator, consolidated. Every line is a percentage: income statement and cash flow lines of revenue "
            "(line 3.01), balance sheet lines of total assets (line 1). Account names are in Portuguese. The interim "
            "column covers the year to date, so its lines are comparable as shares of revenue only.\n\n"
            + table(INCOME, "3.01", "Income statement (% of revenue)") + "\n\n"
            + table(BALANCE, "1", "Balance sheet (% of total assets)") + "\n\n"
            + table(CASH, "3.01", "Cash flow (% of revenue)") + "\n\n"
            + "### Year-on-year growth of the fiscal years (%)\n| Line | " + " | ".join(labels) + " |\n|---|"
            + "---|" * len(labels) + "\n" + "\n".join(growth) + "\n\n"
            + "### Dividends and interest on capital paid (cash flow) as % of net income\n| | "
            + " | ".join(labels) + " |\n|---|" + "---|" * len(labels) + "\n| Payout | " + " | ".join(payout) + " |\n\n"
            + "### Dividends per share by calendar year, oldest to latest 12 months (latest = 100)\n"
            + ", ".join(f"{v:.0f}" for v in dps_index) + "\n\n"
            + f"Dividend yield over the last 12 months: {100 * dy12:.1f}%; average over the last 24 months: "
              f"{100 * dy24:.1f}% a year.\n")
