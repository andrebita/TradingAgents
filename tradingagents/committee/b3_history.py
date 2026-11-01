"""The investor's real portfolio month by month, from the B3 exports: what was held, what it was worth, and the
money that came in and went out. The backtest (``committee.backtest``) puts the committee's plan through the
same money.

**Scope: listed assets and Tesouro Direto**, the two the exports record in full. Bank deposits (CDB, LCI, LCA,
LIG) and debentures are left out: the B3 records bank deposits only from 2025 and misses maturities, so their
money would appear and vanish. What is left out of the real portfolio is left out of the simulated one too.

* **Quantities** come from the trades export ("Negociação"), plus the events the movements export records as
  changes of quantity (bonus shares, splits, fractions sold at auction), plus the conversions the investor lists:
  a code renamed or merged into another, or an offering's receipt turned into the quota. The movements export
  shows a conversion only as an "Atualização" credit of the new code, the same name it gives the lending
  updates, so it cannot be read from the export alone.
* **Ignored on purpose:** transfers between brokers (both legs), settlements ("Transferência - Liquidação": the
  trades export has the trades, and these rows also carry the lending legs), lending (lent shares stay the
  investor's), subscription rights not exercised, the "- Transferido" pairs of income moved between brokers.
* **External flows**, the money that came from the investor's pocket (positive) or went to it (negative):
  purchases, sales, Tesouro purchases and sales, custody fees, and every cash income of the scope (dividends,
  interest on equity, fund income, coupons, amortizations, lending fees, auctions of fractions, capital returns).
  A dividend reinvested shows up as income out and a purchase in: the net is what the pocket saw.
* **Taxes** are not in the exports: the Tesouro's income tax is withheld from the sale, and on listed funds and
  on stock sales above the monthly exemption the investor pays it in a DARF. ``realized_taxes`` estimates them, so
  the money the investor received is the sale less its tax, as in the simulation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

MOVEMENT_COLUMNS = {"Entrada/Saída": "io", "Data": "date", "Movimentação": "kind", "Produto": "product",
                    "Instituição": "broker", "Quantidade": "quantity", "Preço unitário": "price",
                    "Valor da Operação": "value"}
LISTED = re.compile(r"^[A-Z]{4}\d{1,2}$")
INCOME_KINDS = {"Dividendo", "Juros Sobre Capital Próprio", "Rendimento", "Juros", "Amortização", "Reembolso",
                "Restituição de Capital", "Leilão de Fração", "Resgate", "Empréstimo"}
SHARE_KINDS = {"Bonificação em Ativos", "Desdobro", "Fração em Ativos", "Grupamento"}
TESOURO_TRADES = {"Compra": 1.0, "Venda": -1.0, "VENCIMENTO": -1.0}
FEE_KINDS = {"Cobrança de Taxa Semestral"}
REGRESSIVE = ((180, 0.225), (360, 0.20), (720, 0.175))
STOCK_EXEMPTION, STOCK_TAX, FUND_TAX = 20_000.0, 0.15, 0.20


def read_movements(path: str | Path) -> pd.DataFrame:
    """The "Movimentação" export: date, sign (+1 credit, -1 debit), kind, code, product, broker, quantity, price,
    value. Prices and values the export leaves as "-" are NaN."""
    raw = pd.read_excel(path)
    missing = set(MOVEMENT_COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"not a B3 movements export: missing columns {sorted(missing)}")
    df = raw.rename(columns=MOVEMENT_COLUMNS)[list(MOVEMENT_COLUMNS.values())].copy()
    df["date"] = pd.to_datetime(df["date"], dayfirst=True)
    df["sign"] = np.where(df["io"].astype(str).str.strip().str.lower().str.startswith("cred"), 1.0, -1.0)
    df["kind"] = df["kind"].astype(str).str.strip()
    df["product"] = df["product"].astype(str).str.strip()
    df["code"] = df["product"].str.split(" - ").str[0].str.strip()
    for col in ("quantity", "price", "value"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["quantity"] = df["quantity"].fillna(0.0)
    return df.drop(columns="io").sort_values("date", kind="stable").reset_index(drop=True)


def scope_of(code: str) -> str | None:
    """"listed", "tesouro" or None (bank deposits, debentures: outside the backtest)."""
    if code.startswith("Tesouro "):
        return "tesouro"
    return "listed" if LISTED.match(code) else None


@dataclass
class Conversion:
    """A code that became another on ``day``: renamed, merged, or a receipt turned into the quota. ``to`` None:
    the holding was cancelled (fractions after a reverse split, paid at auction). ``quantity``: how many of the new
    code, when not the same number."""

    day: date
    source: str
    to: str | None = None
    quantity: float | None = None


@dataclass
class ExtraTrade:
    """A purchase or sale outside the trades export: shares bought in an offering, for instance."""

    day: date
    code: str
    quantity: float
    value: float


@dataclass
class Adjustment:
    """A change of quantity the exports do not explain, stated by the investor so the ledger meets the position."""

    day: date
    code: str
    delta: float


def listed_events(trades: pd.DataFrame, movements: pd.DataFrame, conversions=(), extra_trades=(),
                  adjustments=()) -> pd.DataFrame:
    """Every dated change of quantity of a listed code: date, code, delta, source."""
    rows = [(r.date, r.code, r.quantity if r.side == "compra" else -r.quantity, "negociação")
            for r in trades.itertuples()]
    rows += [(pd.Timestamp(t.day), t.code, t.quantity, "oferta") for t in extra_trades]
    rows += [(pd.Timestamp(a.day), a.code, a.delta, "ajuste") for a in adjustments]
    shares = movements[movements["kind"].isin(SHARE_KINDS) & movements["code"].map(lambda c: scope_of(c) == "listed")]
    rows += [(r.date, r.code, r.sign * r.quantity, r.kind) for r in shares.itertuples()]
    events = pd.DataFrame(rows, columns=["date", "code", "delta", "source"])
    for c in sorted(conversions, key=lambda c: c.day):
        day = pd.Timestamp(c.day)
        held = events.loc[(events["code"] == c.source) & (events["date"] <= day), "delta"].sum()
        new = [(day, c.source, -held, "conversão")]
        if c.to:
            new.append((day, c.to, held if c.quantity is None else c.quantity, "conversão"))
        events = pd.concat([events, pd.DataFrame(new, columns=events.columns)], ignore_index=True)
    # By date only, stable: within a day trades come first and a conversion's two legs stay in order.
    return events.sort_values("date", kind="stable").reset_index(drop=True)


def tesouro_events(movements: pd.DataFrame) -> pd.DataFrame:
    """Tesouro purchases, sales and maturities: date, bond (the product's name), delta, value."""
    t = movements[movements["kind"].isin(TESOURO_TRADES) & movements["code"].str.startswith("Tesouro ")]
    return pd.DataFrame({"date": t["date"], "bond": t["product"], "delta": t["kind"].map(TESOURO_TRADES) * t["quantity"],
                         "value": t["value"].fillna(0.0)}).reset_index(drop=True)


def holdings(events: pd.DataFrame, day, key: str = "code") -> dict[str, float]:
    """Quantity of each code (or bond) held at the end of ``day``; zeros left out."""
    q = events[events["date"] <= pd.Timestamp(day)].groupby(key)["delta"].sum()
    return {k: float(v) for k, v in q.items() if abs(v) > 1e-6}


def external_flows(trades: pd.DataFrame, movements: pd.DataFrame, extra_trades=()) -> pd.DataFrame:
    """The money that came from the investor's pocket into the scope (positive) or went back (negative): date,
    amount, source, code. Before taxes; see ``realized_taxes``."""
    rows = [(r.date, r.value if r.side == "compra" else -r.value, "negociação", r.code) for r in trades.itertuples()]
    rows += [(pd.Timestamp(t.day), t.value, "oferta", t.code) for t in extra_trades]
    m = movements[movements["code"].map(scope_of).notna() & movements["value"].notna()]
    for r in m.itertuples():
        if r.kind in TESOURO_TRADES and r.code.startswith("Tesouro "):
            rows.append((r.date, r.sign * r.value, r.kind, r.product))
        elif r.kind == "Empréstimo":
            # A lending fee received (credit) goes to the investor; one paid as borrower (debit) comes in.
            rows.append((r.date, -r.sign * r.value, r.kind, r.code))
        elif r.kind in INCOME_KINDS:
            # Cash paid to the investor, whichever leg the export shows (an amortization is a debit of the bond).
            rows.append((r.date, -r.value, r.kind, r.code))
        elif r.kind in FEE_KINDS:
            rows.append((r.date, r.value, r.kind, r.code))
    out = pd.DataFrame(rows, columns=["date", "amount", "source", "code"])
    return out[out["amount"].abs() > 0].sort_values("date", kind="stable").reset_index(drop=True)


def regressive_rate(days: int) -> float:
    return next((rate for limit, rate in REGRESSIVE if days <= limit), 0.15)


def tesouro_lots_taxes(tesouro: pd.DataFrame) -> pd.DataFrame:
    """Income tax withheld on each Tesouro sale, first-in first-out over the purchases: date, bond, gain, tax."""
    lots: dict[str, list[list]] = {}
    rows = []
    for r in tesouro.sort_values("date", kind="stable").itertuples():
        if r.delta > 0:
            lots.setdefault(r.bond, []).append([r.date, r.delta, r.value / r.delta if r.delta else 0.0])
            continue
        q, price = -r.delta, (r.value / -r.delta if r.delta else 0.0)
        gain = tax = 0.0
        for lot in lots.get(r.bond, []):
            if q <= 1e-9:
                break
            take = min(q, lot[1])
            g = (price - lot[2]) * take
            gain += g
            tax += regressive_rate((r.date - lot[0]).days) * max(g, 0.0)
            lot[1] -= take
            q -= take
        lots[r.bond] = [lot for lot in lots.get(r.bond, []) if lot[1] > 1e-9]
        rows.append((r.date, r.bond, gain, tax))
    return pd.DataFrame(rows, columns=["date", "bond", "gain", "tax"])


def listed_taxes(trades: pd.DataFrame, events: pd.DataFrame, fund_codes: set[str], extra_trades=()) -> pd.DataFrame:
    """Income tax on listed sales, by month: listed funds 20% of the gain; stocks 15% of the month's net gain when
    the month's stock sales pass R$ 20,000, with losses carried forward per class. Average cost as in the tax
    return: purchases add, sales take out at the average. Conversions carry the cost to the new code."""
    qty: dict[str, float] = {}
    cost: dict[str, float] = {}
    month_rows: dict[tuple, dict] = {}
    trade_rows = [(r.date, r.code, r.side, r.quantity, r.value) for r in trades.itertuples()]
    trade_rows += [(pd.Timestamp(t.day), t.code, "compra", t.quantity, t.value) for t in extra_trades]
    other = events[~events["source"].isin(("negociação", "oferta"))]
    seq = sorted([(d, 0, c, s, q, v) for d, c, s, q, v in trade_rows] +
                 [(r.date, 1, r.code, r.source, r.delta, 0.0) for r in other.itertuples()], key=lambda x: (x[0], x[1]))
    pending_conversion: dict[pd.Timestamp, float] = {}
    for day, _, code, side, q, value in seq:
        if side == "compra":
            qty[code] = qty.get(code, 0.0) + q
            cost[code] = cost.get(code, 0.0) + value
        elif side == "venda":
            held = qty.get(code, 0.0)
            avg = cost.get(code, 0.0) / held if held > 1e-9 else None
            gain = value - avg * q if avg is not None else 0.0
            if held > 1e-9:
                cost[code] -= avg * min(q, held)
            qty[code] = held - q
            cls = "fundos" if code in fund_codes else "acoes"
            m = month_rows.setdefault((day.to_period("M"), cls), {"sales": 0.0, "gain": 0.0})
            m["sales"] += value
            m["gain"] += gain
        elif side == "conversão":
            if q < 0:
                pending_conversion[day] = cost.pop(code, 0.0)
                qty[code] = 0.0
            else:
                qty[code] = qty.get(code, 0.0) + q
                cost[code] = cost.get(code, 0.0) + pending_conversion.pop(day, 0.0)
        else:  # bonus shares, splits, fractions, adjustments: quantity changes, cost stays
            qty[code] = qty.get(code, 0.0) + q
    rows, carried = [], {"acoes": 0.0, "fundos": 0.0}
    for (month, cls), m in sorted(month_rows.items()):
        tax = 0.0
        if cls == "fundos" or m["sales"] > STOCK_EXEMPTION:
            net = m["gain"] - carried[cls]
            carried[cls] = max(-net, 0.0)
            tax = (FUND_TAX if cls == "fundos" else STOCK_TAX) * max(net, 0.0)
        rows.append((month.to_timestamp(how="end").normalize(), cls, m["sales"], m["gain"], tax))
    return pd.DataFrame(rows, columns=["date", "class", "sales", "gain", "tax"])


def realized_taxes(trades: pd.DataFrame, events: pd.DataFrame, tesouro: pd.DataFrame,
                   fund_codes: set[str], extra_trades=()) -> pd.DataFrame:
    """Estimated income tax on what was sold, dated: Tesouro withheld at the sale; listed in the month's DARF."""
    t = tesouro_lots_taxes(tesouro)
    lst = listed_taxes(trades, events, fund_codes, extra_trades)
    out = pd.concat([pd.DataFrame({"date": t["date"], "tax": t["tax"], "source": "Tesouro"}),
                     pd.DataFrame({"date": lst["date"], "tax": lst["tax"], "source": lst["class"]})])
    return out[out["tax"] > 0.005].sort_values("date").reset_index(drop=True)


# -- Tesouro prices ------------------------------------------------------------------------------------------------

TESOURO_KINDS = ("Tesouro IPCA+ com Juros Semestrais", "Tesouro Prefixado com Juros Semestrais",
                 "Tesouro IGPM+ com Juros Semestrais", "Tesouro Renda+ Aposentadoria Extra", "Tesouro Educa+",
                 "Tesouro Selic", "Tesouro IPCA+", "Tesouro Prefixado")
PAYMENT_YEARS = {"Tesouro Educa+": 4, "Tesouro Renda+ Aposentadoria Extra": 19}   # named by the first payment


def bond_key(name: str) -> tuple[str, int]:
    """("Tesouro Selic", 2031) for "Tesouro Selic 2031"; Educa+ and Renda+ by the year of their last payment, as
    the Treasury's price file dates them."""
    for kind in TESOURO_KINDS:
        if name.startswith(kind + " "):
            year = int(re.search(r"(\d{4})", name[len(kind):]).group(1))
            return kind, year + PAYMENT_YEARS.get(kind, 0)
    raise ValueError(f"unknown Tesouro bond: {name}")


def read_tesouro_prices(path: str | Path) -> pd.DataFrame:
    """The Treasury's price file (PrecoTaxaTesouroDireto.csv): kind, maturity, date, rate, buy, sell, base."""
    f = pd.read_csv(path, sep=";", decimal=",")
    return pd.DataFrame({
        "kind": f["Tipo Titulo"], "maturity": pd.to_datetime(f["Data Vencimento"], dayfirst=True),
        "date": pd.to_datetime(f["Data Base"], dayfirst=True), "rate": f["Taxa Compra Manha"],
        "buy": f["PU Compra Manha"], "sell": f["PU Venda Manha"], "base": f["PU Base Manha"],
    }).sort_values("date", kind="stable").reset_index(drop=True)


class TesouroPrices:
    """Unit prices by bond and day, the last quote on or before the day."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self._series: dict[tuple, pd.DataFrame] = {}
        frame = frame.assign(year=frame["maturity"].dt.year)
        for (kind, year), g in frame.groupby(["kind", "year"]):
            self._series[(kind, year)] = g.set_index("date")[["buy", "sell", "base", "rate", "maturity"]]

    def quote(self, key: tuple[str, int], day, column: str = "base") -> float | None:
        s = self._series.get(key)
        if s is None:
            return None
        s = s.loc[:pd.Timestamp(day)]
        return float(s[column].iloc[-1]) if len(s) else None

    def maturity(self, key: tuple[str, int]) -> date | None:
        s = self._series.get(key)
        return s["maturity"].iloc[-1].date() if s is not None else None

    def offers(self, day):
        """The bonds on offer on ``day`` (the last quote day on or before it), as ``tesouro_direto.Offer``."""
        from tradingagents.dataflows.vendors.tesouro_direto import Offer

        f = self.frame[self.frame["date"] <= pd.Timestamp(day)]
        if f.empty:
            return []
        last = f["date"].max()
        d = f[(f["date"] == last) & (f["maturity"] > last) & (f["buy"] > 0)]
        return [Offer(r.kind, r.maturity.date(), float(r.rate), float(r.buy), last.date()) for r in d.itertuples()]


# -- Valuation and returns -----------------------------------------------------------------------------------------

def last_close(series: pd.Series | None, day) -> float | None:
    if series is None:
        return None
    s = series.loc[:pd.Timestamp(day)].dropna()
    return float(s.iloc[-1]) if len(s) else None


def value_on(day, listed: pd.DataFrame, tesouro: pd.DataFrame, closes: dict[str, pd.Series],
             prices: TesouroPrices, fallback: dict[str, float] | None = None) -> dict:
    """What the real portfolio was worth at the end of ``day``: listed at the last close (or ``fallback``, the
    price paid, for a code that never traded, such as an offering's receipt), Tesouro at the Treasury's base
    price. Codes with neither are listed in ``missing``."""
    fallback = fallback or {}
    out = {"listed": 0.0, "tesouro": 0.0, "missing": []}
    for code, q in holdings(listed, day).items():
        p = last_close(closes.get(code), day) or fallback.get(code)
        if p is None:
            out["missing"].append(code)
            continue
        out["listed"] += q * p
    for bond, q in holdings(tesouro, day, key="bond").items():
        p = prices.quote(bond_key(bond), day)
        if p is None:
            out["missing"].append(bond)
            continue
        out["tesouro"] += q * p
    out["total"] = out["listed"] + out["tesouro"]
    return out


def modified_dietz(values: pd.Series, flows: pd.DataFrame) -> pd.Series:
    """Monthly returns from period-end values and dated flows, each flow weighted by the share of the period it was
    invested (Modified Dietz). A period starting from nothing returns 0."""
    out = {}
    days = list(values.index)
    for prev, day in zip(days[:-1], days[1:], strict=False):
        f = flows[(flows["date"] > prev) & (flows["date"] <= day)]
        span = (day - prev).days or 1
        net = float(f["amount"].sum())
        weighted = float(((day - f["date"]).dt.days / span * f["amount"]).sum())
        base = values[prev] + weighted
        out[day] = (values[day] - values[prev] - net) / base if base > 1.0 else 0.0
    return pd.Series(out)


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annual money-weighted return of dated amounts from the investor's side (money in negative, out positive)."""
    if not flows or not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)
    years = [((d - t0).days / 365.25, a) for d, a in flows]

    def npv(rate):
        return sum(a / (1 + rate) ** t for t, a in years)

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def drawdown(index: pd.Series) -> float:
    """Largest fall from a running peak (a negative fraction)."""
    return float((index / index.cummax() - 1).min()) if len(index) else 0.0


def annualized(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    growth = float((1 + returns).prod())
    return growth ** (12 / len(returns)) - 1 if growth > 0 else -1.0


def month_ends(calendar: pd.DatetimeIndex, start, end) -> list[pd.Timestamp]:
    """The last trading day of each month between ``start`` and ``end``, from a calendar of trading days."""
    c = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    s = pd.Series(c, index=c)
    return list(s.groupby(s.index.to_period("M")).max())


def is_zero(x: float) -> bool:
    return math.isclose(x, 0.0, abs_tol=1e-6)
