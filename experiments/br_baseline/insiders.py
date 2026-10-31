"""Insider pilot: do B3 stocks whose officers and directors bought shares beat the Ibovespa?

No LLM. CVM's VLMO dataset holds the structured monthly reports in which the
officers, board members and controlling shareholders of every listed company
report their trades (CVM Resolution 44, art. 11), from December 2018. FCA gives
each company's trading codes and Yahoo the prices.

Point in time. A monthly form counts from the delivery of the version CVM
keeps, so a re-delivered form counts only from its re-delivery. At each
month-end ``t`` the signal reads the forms delivered by ``t`` for the three
reference months before ``t``'s month; the position is entered at the close of
the next session and held to the close of the session after the next month-end.
The design is pre-registered in the README ("Piloto de insiders").

Usage:
    python experiments/br_baseline/insiders.py [--out experiments/br_baseline/results/insiders]
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import cvm
from tradingagents.default_config import DEFAULT_CONFIG

HERE = Path(__file__).resolve().parent
VLMO_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_{year}.zip"
YEARS = range(2018, 2027)
CODE = re.compile(r"^[A-Z]{4}(3|4|5|6|11)$")
CLASS_SUFFIX = {"ON": "3", "PN": "4", "PNA": "5", "PNB": "6"}
ROLES = {"officers_board": ("Diretor", "Conselho de Administração"),
         "controllers": ("Controlador",),
         "all_insiders": ("Diretor", "Conselho de Administração", "Controlador")}
FIRST, LAST = "2019-03-01", "2026-08-31"   # rebalance month-ends (pre-registered)
MIN_TRADED = 2_000_000                     # R$ per day, median over 63 sessions
COST = 0.0015                              # per side, on turnover
MIN_NAMES = 5


# --- Data ---------------------------------------------------------------------------------

def vlmo_trades(cache: Path) -> pd.DataFrame:
    """Open-market trades in the company's own shares and units, with the delivery of their form."""
    frames = []
    for year in YEARS:
        path = cache / "vlmo" / f"vlmo_cia_aberta_{year}.zip"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            response = requests.get(VLMO_URL.format(year=year), timeout=120)
            response.raise_for_status()
            path.write_bytes(response.content)
        with zipfile.ZipFile(path) as archive:
            forms = pd.read_csv(archive.open(f"vlmo_cia_aberta_{year}.csv"), sep=";", encoding="latin-1", dtype=str)
            lines = pd.read_csv(archive.open(f"vlmo_cia_aberta_con_{year}.csv"), sep=";", encoding="latin-1",
                                dtype=str)
        lines = lines[lines["Tipo_Movimentacao"].isin(["Compra à vista", "Venda à vista"])
                      & (lines["Tipo_Empresa"] == "Companhia") & lines["Tipo_Ativo"].isin(["Ações", "Units"])]
        delivered = forms.groupby(["CNPJ_Companhia", "Data_Referencia", "Versao"])["Data_Entrega"].max()
        frames.append(lines.join(delivered, on=["CNPJ_Companhia", "Data_Referencia", "Versao"]))
    df = pd.concat(frames, ignore_index=True)
    return pd.DataFrame({
        "cnpj": df["CNPJ_Companhia"], "role": df["Tipo_Cargo"].fillna(""),
        "sign": np.where(df["Tipo_Movimentacao"] == "Compra à vista", 1, -1),
        "units": df["Tipo_Ativo"] == "Units",
        "cls": df["Caracteristica_Valor_Mobiliario"].fillna("").str.upper().str.strip(),
        "qty": pd.to_numeric(df["Quantidade"], errors="coerce"),
        "reported": pd.to_numeric(df["Volume"], errors="coerce"),
        "ref": pd.to_datetime(df["Data_Referencia"], errors="coerce"),
        "delivered": pd.to_datetime(df["Data_Entrega"].str[:10], errors="coerce"),
        "traded": pd.to_datetime(df["Data_Movimentacao"], errors="coerce"),
    }).dropna(subset=["ref", "delivered", "qty"])


def company_codes(cache: Path) -> dict[str, set[str]]:
    """``{cnpj: {trading codes of its shares and units on B3}}`` from every FCA year (a mapping only)."""
    codes: dict[str, set[str]] = {}
    config = {**copy.deepcopy(DEFAULT_CONFIG), "data_cache_dir": str(cache)}
    with run_config(config):
        for year in YEARS:
            path = cvm._cached_zip(cvm._FCA_URL.format(year=year), f"fca_cia_aberta_{year}.zip", year)
            if path is None:
                continue
            for row in cvm._rows(path, f"valor_mobiliario_{year}.csv"):
                code = (row.get("Codigo_Negociacao") or "").strip().upper()
                if row.get("Mercado") == "Bolsa" and CODE.match(code):
                    codes.setdefault(row["CNPJ_Companhia"], set()).add(code)
    return codes


def prices(codes: set[str], cache: Path) -> dict[str, pd.DataFrame]:
    """Daily Close, Adj Close and Volume per code from Yahoo, cached; codes Yahoo lacks are left out."""
    folder = cache / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    missing = sorted(c for c in codes if not (folder / f"{c}.csv").exists())
    for i in range(0, len(missing), 40):
        batch = missing[i:i + 40]
        for attempt in range(4):
            try:
                data = yf.download([f"{c}.SA" for c in batch], start="2018-06-01", end="2026-10-31",
                                   auto_adjust=False, group_by="ticker", threads=True, progress=False)
                break
            except Exception:
                time.sleep(5 * 2 ** attempt)
        else:
            continue
        for code in batch:
            try:
                frame = data[f"{code}.SA"][["Close", "Adj Close", "Volume"]].dropna(subset=["Adj Close"])
            except KeyError:
                frame = pd.DataFrame(columns=["Close", "Adj Close", "Volume"])
            frame.to_csv(folder / f"{code}.csv")
        time.sleep(2)
    out = {}
    for code in codes:
        frame = pd.read_csv(folder / f"{code}.csv", index_col=0, parse_dates=True)
        if len(frame):
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            out[code] = frame.sort_index()
    return out


def benchmark(cache: Path) -> pd.Series:
    path = cache / "prices" / "BVSP.csv"
    if not path.exists():
        frame = yf.Ticker("^BVSP").history(start="2018-06-01", end="2026-10-31", auto_adjust=False)
        frame.index = frame.index.tz_localize(None).normalize()
        frame[["Close"]].to_csv(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)["Close"].sort_index()


# --- Signal and portfolio -----------------------------------------------------------------

def trade_values(trades: pd.DataFrame, codes: dict[str, set[str]], px: dict[str, pd.DataFrame]) -> pd.Series:
    """Each trade's value in R$: as reported, or quantity times the class's close on the trade day."""
    values = trades["reported"].where(trades["reported"] > 0)
    for i in values[values.isna()].index:
        row = trades.loc[i]
        suffix = "11" if row["units"] else CLASS_SUFFIX.get(row["cls"])
        candidates = [c for c in codes.get(row["cnpj"], ()) if c in px]
        exact = [c for c in candidates if suffix and c.endswith(suffix) and len(c) == 4 + len(suffix)]
        day = row["traded"] if pd.notna(row["traded"]) else row["ref"]
        for code in exact or candidates:
            close = px[code]["Close"].asof(day)
            if pd.notna(close):
                values.at[i] = row["qty"] * close
                break
    return values


def representative(cnpj_codes: set[str], px: dict[str, pd.DataFrame], t: pd.Timestamp) -> tuple[str, float] | None:
    """The company's most traded code over the 63 sessions to ``t`` and its median traded value."""
    best = None
    for code in cnpj_codes:
        frame = px.get(code)
        if frame is None:
            continue
        window = frame.loc[:t].tail(63)
        if len(window) < 40:
            continue
        traded = float((window["Close"] * window["Volume"]).median())
        if best is None or traded > best[1]:
            best = (code, traded)
    return best


def holding_return(frame: pd.DataFrame, entry: pd.Timestamp, exit_: pd.Timestamp) -> float | None:
    adj = frame["Adj Close"]
    if entry not in adj.index:
        return None
    last = adj.loc[:exit_]
    return float(last.iloc[-1] / adj.loc[entry] - 1)   # a delisted stock keeps its return to its last price


def periods(codes, px, bvsp) -> list[dict]:
    """Each rebalance month-end with its holding window and liquid universe: ``{cnpj: (code, traded, return)}``."""
    sessions = bvsp.index
    month_ends = sessions.to_series().groupby(sessions.to_period("M")).max()
    out = []
    for t in (t for t in month_ends if pd.Timestamp(FIRST) <= t <= pd.Timestamp(LAST)):
        nxt = month_ends[month_ends > t].iloc[0]
        entry = sessions[sessions > t][0]
        after = sessions[sessions > nxt]
        if not len(after):
            break
        exit_ = after[0]
        universe = {}
        for cnpj, cs in codes.items():
            rep = representative(cs, px, t)
            if rep and rep[1] >= MIN_TRADED:
                r = holding_return(px[rep[0]], entry, exit_)
                if r is not None:
                    universe[cnpj] = (rep[0], rep[1], r)
        out.append({"t": t, "entry": entry, "exit": exit_, "universe": universe})
    return out


def run(trades, schedule, bvsp, roles, window, select="positive"):
    mine = trades[trades["role"].str.startswith(roles)]
    months, prev_w, prev_ew = [], {}, {}
    for period in schedule:
        t, entry, exit_, universe = period["t"], period["entry"], period["exit"], period["universe"]
        start = (t.to_period("M") - window).to_timestamp()
        seen = mine[(mine["delivered"] <= t) & (mine["ref"] >= start) & (mine["ref"] < t.to_period("M").to_timestamp())]
        net = seen.groupby("cnpj")["signed_value"].sum()
        if select == "negative":
            chosen = [c for c in universe if net.get(c, 0) < 0]
        else:
            chosen = [c for c in universe if net.get(c, 0) > 0]
            if select == "top_tercile" and chosen:
                intensity = pd.Series({c: net[c] / universe[c][1] for c in chosen})
                chosen = list(intensity[intensity >= intensity.quantile(2 / 3)].index)
        fallback = len(chosen) < MIN_NAMES
        if fallback:
            chosen = list(universe)
        w = {c: 1 / len(chosen) for c in chosen}
        ew = {c: 1 / len(universe) for c in universe}
        turnover = sum(abs(w.get(c, 0) - prev_w.get(c, 0)) for c in set(w) | set(prev_w))
        turnover_ew = sum(abs(ew.get(c, 0) - prev_ew.get(c, 0)) for c in set(ew) | set(prev_ew))
        months.append({"t": t.date().isoformat(), "names": len(chosen), "universe": len(universe),
                       "fallback": fallback,
                       "portfolio": sum(w[c] * universe[c][2] for c in w) - COST * turnover,
                       "equal_weight": sum(ew[c] * universe[c][2] for c in ew) - COST * turnover_ew,
                       "ibovespa": float(bvsp.loc[:exit_].iloc[-1] / bvsp.loc[entry] - 1),
                       "turnover": turnover})
        prev_w, prev_ew = w, ew
    return pd.DataFrame(months)


def summary(m: pd.DataFrame) -> dict:
    def cagr(r):
        return float((1 + r).prod() ** (12 / len(r)) - 1)

    def drawdown(r):
        eq = (1 + r).cumprod()
        return float((eq / eq.cummax() - 1).min())

    out = {"months": len(m), "fallback_months": int(m["fallback"].sum()), "mean_names": float(m["names"].mean()),
           "mean_universe": float(m["universe"].mean()), "mean_turnover": float(m["turnover"].mean())}
    for col in ("portfolio", "equal_weight", "ibovespa"):
        out[f"cagr_{col}"] = cagr(m[col])
        out[f"max_drawdown_{col}"] = drawdown(m[col])
    year = pd.to_datetime(m["t"]).dt.year
    compounded = (1 + m[["portfolio", "equal_weight", "ibovespa"]]).groupby(year).prod()
    for ref in ("equal_weight", "ibovespa"):
        d = m["portfolio"] - m[ref]
        years = compounded["portfolio"] - compounded[ref]
        out[f"vs_{ref}"] = {"mean_monthly": float(d.mean()), "t": float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))),
                            "months_positive": float((d > 0).mean()),
                            "years_positive": int((years > 0).sum()), "years": len(years),
                            "by_year": {int(y): round(v, 4) for y, v in years.items()}}
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(HERE / "results" / "insiders"))
    args = p.parse_args()
    out = Path(args.out)
    cache = out / "cache"

    trades = vlmo_trades(cache)
    codes = company_codes(cache)
    px = prices(set().union(*codes.values()), cache)
    bvsp = benchmark(cache)
    trades["value"] = trade_values(trades, codes, px)
    trades["signed_value"] = trades["sign"] * trades["value"]

    in_vlmo = set(trades["cnpj"])
    data = {"trades": len(trades), "trades_reported_value": int((trades["reported"] > 0).sum()),
            "trades_valued_by_price": int(((trades["reported"].fillna(0) <= 0) & trades["value"].notna()).sum()),
            "trades_unvalued": int(trades["value"].isna().sum()),
            "companies_with_trades": len(in_vlmo),
            "companies_with_codes": len(in_vlmo & set(codes)),
            "companies_with_yahoo_prices": len({c for c in in_vlmo if any(k in px for k in codes.get(c, ()))}),
            "codes": len(set().union(*codes.values())), "codes_with_yahoo_prices": len(px)}
    print(json.dumps(data, indent=2), flush=True)

    variants = {"primary": ("officers_board", 3, "positive"),
                "a_controllers": ("controllers", 3, "positive"),
                "b_all_insiders": ("all_insiders", 3, "positive"),
                "c_top_tercile": ("officers_board", 3, "top_tercile"),
                "d_six_months": ("officers_board", 6, "positive"),
                "e_net_sellers": ("officers_board", 3, "negative")}
    report = {"data": data, "design": {"first": FIRST, "last": LAST, "min_traded": MIN_TRADED, "cost": COST,
                                       "min_names": MIN_NAMES}, "variants": {}}
    schedule = periods(codes, px, bvsp)
    for name, (roles, window, select) in variants.items():
        m = run(trades, schedule, bvsp, ROLES[roles], window, select)
        m.to_csv(out / f"monthly_{name}.csv", index=False)
        report["variants"][name] = summary(m)
        s = report["variants"][name]
        print(f"{name:15} CAGR {s['cagr_portfolio']:.1%} vs EW {s['cagr_equal_weight']:.1%} vs Ibov "
              f"{s['cagr_ibovespa']:.1%} | excess vs EW {s['vs_equal_weight']['mean_monthly']:+.2%}/mo "
              f"t={s['vs_equal_weight']['t']:.2f} | years + {s['vs_equal_weight']['years_positive']}/"
              f"{s['vs_equal_weight']['years']} | names {s['mean_names']:.0f}/{s['mean_universe']:.0f}", flush=True)
    (out / "insiders_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
