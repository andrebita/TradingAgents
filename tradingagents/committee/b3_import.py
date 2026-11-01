"""The B3 Área do Investidor exports, read into trades and the average cost of each position.

The trades export ("Negociação", Excel) lists every purchase and sale on the exchange, in every broker, since
November 2019. From it this module computes the average cost the way the tax return does: purchases raise the
total cost, a sale takes out its share at the average and leaves the average unchanged. Brokerage fees are not
in the export, so the average here is before fees (the tax return may add them).

Trades alone do not explain every position. Renames (TRPL4 to ISAE4), splits, bonus shares, subscriptions,
transfers between brokers and lent shares are in the "Movimentação" export; a position bought before the
export's first date has no trades at all. So ``reconcile`` compares the computed quantity with the one in the
investor's wealth file and only trusts an average whose quantity matches; the rest are listed with the likely
reason, to be settled with the other exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

COLUMNS = {"Data do Negócio": "date", "Tipo de Movimentação": "side", "Mercado": "market", "Instituição": "broker",
           "Código de Negociação": "code", "Quantidade": "quantity", "Preço": "price", "Valor": "value"}


def read_trades(path: str | Path) -> pd.DataFrame:
    """The "Negociação" export as a frame: date, side (compra/venda), market, broker, code, quantity, price,
    value. The fractional-market suffix F is dropped from the code (PETR4F is PETR4)."""
    raw = pd.read_excel(path)
    missing = set(COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"not a B3 trades export: missing columns {sorted(missing)}")
    df = raw.rename(columns=COLUMNS)[list(COLUMNS.values())].copy()
    df["date"] = pd.to_datetime(df["date"], dayfirst=True)
    df["side"] = df["side"].str.strip().str.lower()
    df["code"] = df["code"].astype(str).str.strip().str.upper().str.replace(r"(?<=\d)F$", "", regex=True)
    df["broker"] = df["broker"].astype(str).str.strip().str.rstrip(".").str.replace(r"\s+", " ", regex=True)
    df["quantity"] = df["quantity"].astype(float)
    df["value"] = df["value"].astype(float)
    if not set(df["side"]) <= {"compra", "venda"}:
        raise ValueError(f"unknown trade sides: {sorted(set(df['side']) - {'compra', 'venda'})}")
    # Same-day purchases before sales, so a day's sale finds the day's purchase.
    order = df["side"].map({"compra": 0, "venda": 1})
    return df.assign(_o=order).sort_values(["date", "_o"], kind="stable").drop(columns="_o").reset_index(drop=True)


@dataclass
class Cost:
    code: str
    quantity: float
    total_cost: float
    first_trade: pd.Timestamp
    last_trade: pd.Timestamp
    oversold: bool = False          # a sale beyond the quantity bought: history starts later, or events are missing

    @property
    def average_price(self) -> float | None:
        return self.total_cost / self.quantity if self.quantity > 0 and not self.oversold else None


def average_costs(trades: pd.DataFrame, renames: dict[str, str] | None = None,
                  splits: dict[str, list[tuple[str, float]]] | None = None) -> dict[str, Cost]:
    """Quantity and average cost per code, in date order.

    ``renames``: old code to new (applied to every trade). ``splits``: code to [(date, factor)], a factor of 10 for
    a 1:10 split, applied to the quantity held on that date (the total cost does not change).
    """
    renames = renames or {}
    pending = {code: sorted((pd.Timestamp(d), f) for d, f in v) for code, v in (splits or {}).items()}
    out: dict[str, Cost] = {}
    for t in trades.itertuples(index=False):
        code = renames.get(t.code, t.code)
        c = out.setdefault(code, Cost(code, 0.0, 0.0, t.date, t.date))
        while pending.get(code) and pending[code][0][0] <= t.date:
            c.quantity *= pending[code].pop(0)[1]
        c.last_trade = t.date
        if t.side == "compra":
            c.total_cost += t.value
            c.quantity += t.quantity
        else:
            if t.quantity > c.quantity + 1e-9:
                c.oversold = True
            average = c.total_cost / c.quantity if c.quantity > 0 else 0.0
            c.total_cost = max(c.total_cost - average * min(t.quantity, c.quantity), 0.0)
            c.quantity -= t.quantity
    for code, left in pending.items():           # splits after the last trade
        if code in out:
            for _, factor in left:
                out[code].quantity *= factor
    return out


def reconcile(costs: dict[str, Cost], held: dict[str, float]) -> dict[str, list[dict]]:
    """Compare the trades with the wealth file: ``held`` is code to quantity.

    Returns three lists: ``matched`` (quantities equal, the average is usable), ``different`` (the code is in
    both but the quantities differ, or the history sells more than it bought), ``only_in_trades`` (a position
    the trades leave open that the wealth file does not have) and ``only_in_wealth`` (held, with no trades).
    """
    out = {"matched": [], "different": [], "only_in_trades": [], "only_in_wealth": []}
    for code, q in sorted(held.items()):
        c = costs.get(code)
        if c is None:
            out["only_in_wealth"].append({"code": code, "held": q})
        elif abs(c.quantity - q) < 1e-6 and not c.oversold:
            out["matched"].append({"code": code, "held": q, "average_price": round(c.average_price, 4),
                                   "total_cost": round(c.total_cost, 2), "first_trade": c.first_trade.date(),
                                   "last_trade": c.last_trade.date()})
        else:
            out["different"].append({"code": code, "held": q, "from_trades": c.quantity, "oversold": c.oversold})
    for code, c in sorted(costs.items()):
        if code not in held and abs(c.quantity) > 1e-6:
            out["only_in_trades"].append({"code": code, "from_trades": c.quantity, "oversold": c.oversold,
                                          "last_trade": c.last_trade.date()})
    return out
