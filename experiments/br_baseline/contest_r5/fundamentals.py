"""Annual CVM statements (DFP 2010-2025) as one table, for the quality and value signals (protocol §2).

One row per company, fiscal year and version: the delivery date, consolidated net income, consolidated equity
(non-controlling interests included, so the same perimeter as the income), total assets and basic earnings per
share by class. Consolidated figures are used when filed; otherwise the individual ones, flagged. The CVM file
keeps only the latest version of each period: a period whose latest version was delivered after a decision is
left out of that decision (§2).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

import pandas as pd

NET_INCOME = re.compile(r"lucro.*(per[ií]odo|exerc[ií]cio)", re.I)
EQUITY = re.compile(r"patrim[oô]nio l[ií]quido", re.I)
SCALES = {"MIL": 1000.0, "UNIDADE": 1.0}


def _rows(archive: zipfile.ZipFile, member: str):
    if member not in archive.namelist():
        return
    with archive.open(member) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
        head = {n: i for i, n in enumerate(next(reader))}
        for row in reader:
            if row[head["ORDEM_EXERC"]].startswith("Ú"):
                yield {k: row[i] for k, i in head.items()}


def _amount(row) -> float | None:
    try:
        value = float(row["VL_CONTA"])
    except ValueError:
        return None
    return value if row["CD_CONTA"].startswith("3.99") else value * SCALES.get(row["ESCALA_MOEDA"].upper(), 1.0)


def extract(cache: Path, years=range(2010, 2026)) -> pd.DataFrame:
    out: dict[tuple, dict] = {}
    for year in years:
        path = cache / f"dfp_cia_aberta_{year}.zip"
        if not path.exists():
            continue
        with zipfile.ZipFile(path) as archive:
            received = {}
            with archive.open(f"dfp_cia_aberta_{year}.csv") as raw:
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";"):
                    received[(row["CNPJ_CIA"], row["DT_REFER"], row["VERSAO"])] = row["DT_RECEB"]
            for basis in ("ind", "con"):           # consolidated overwrites individual
                for row in _rows(archive, f"dfp_cia_aberta_DRE_{basis}_{year}.csv"):
                    key = (row["CNPJ_CIA"], row["DT_REFER"], row["VERSAO"])
                    rec = out.setdefault(key, {"basis": basis})
                    code, name = row["CD_CONTA"], row["DS_CONTA"]
                    amount = _amount(row)
                    if amount is None:
                        continue
                    if code.count(".") == 1 and NET_INCOME.search(name) and "atribu" not in name.lower():
                        rec[f"ni_{basis}"] = amount           # the last such line is the bottom line
                    elif code == "3.99.01.01":
                        rec[f"eps_on_{basis}"] = amount
                    elif code == "3.99.01.02":
                        rec[f"eps_pn_{basis}"] = amount
                for member in ("BPA", "BPP"):
                    for row in _rows(archive, f"dfp_cia_aberta_{member}_{basis}_{year}.csv"):
                        key = (row["CNPJ_CIA"], row["DT_REFER"], row["VERSAO"])
                        rec = out.setdefault(key, {"basis": basis})
                        code, name = row["CD_CONTA"], row["DS_CONTA"]
                        amount = _amount(row)
                        if amount is None:
                            continue
                        if code == "1":
                            rec[f"assets_{basis}"] = amount
                        elif code.count(".") == 1 and code.startswith("2.") and EQUITY.search(name):
                            rec[f"equity_{basis}"] = amount
            for key, rec in out.items():
                if key in received:
                    rec["received"] = received[key]
    rows = []
    for (cnpj, refer, version), rec in out.items():
        basis = "con" if "ni_con" in rec and "equity_con" in rec else "ind"
        rows.append({"cnpj": cnpj, "refer": refer, "version": int(version), "received": rec.get("received"),
                     "basis": basis, "net_income": rec.get(f"ni_{basis}"), "equity": rec.get(f"equity_{basis}"),
                     "assets": rec.get(f"assets_{basis}"), "eps_on": rec.get(f"eps_on_{basis}"),
                     "eps_pn": rec.get(f"eps_pn_{basis}")})
    frame = pd.DataFrame(rows)
    frame["refer"] = pd.to_datetime(frame["refer"])
    frame["received"] = pd.to_datetime(frame["received"])
    return frame.sort_values(["cnpj", "refer", "version"])
