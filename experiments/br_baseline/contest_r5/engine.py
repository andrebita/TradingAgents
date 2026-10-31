"""The common executor of the protocol (§3): every arm through the same orders, events, taxes and units.

Monthly: plan at the month's last close, execute at the next session's close. Whole shares; at most three
sales and three purchases a month after the first build (ten purchases); sales first. Stock sales planned up
to R$ 19,000 a month, tax checked against R$ 20,000 at the executed prices. Out-of-selection positions are sold
largest first; overweight positions are trimmed only in decision months (an interpretation: the protocol does
not say how often). Costs 0.15% of each order. Idle cash earns nothing.

Events: share factors on their dates; proventos accrued at the ex-date (receivable, in the wealth) and paid 30
days later, with 17.5% withheld on the share treated as JCP. A held code that stops trading for ten sessions is
converted at value into a code of the same company and class that started trading within ten sessions of its
last trade; otherwise it is sold at its last close (a taxable sale).

Taxes, each arm a separate taxpayer: stocks pay 15% of the month's net gain after carried losses when the
month's sales exceed R$ 20,000, and losses carry forward in any month; ETFs pay 15% of the gain on sale, with no
exemption. The final value liquidates everything on the last day, with its costs and taxes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EXEMPT, PLAN_CAP, TAX, JCP_TAX = 20_000.0, 19_000.0, 0.15, 0.175


@dataclass
class Params:
    cost: float = 0.0015
    jcp_share: float = 1.0
    max_buys: int = 3
    max_sells: int = 3
    first_buys: int = 10
    holdings: int = 10


@dataclass
class Result:
    nav: pd.Series                     # daily wealth before liquidation
    units: pd.Series                   # daily unit value (time-weighted)
    flows: list                        # (date, amount)
    final: float                       # after liquidation, costs and taxes
    final_before: float                # marked to market on the last day
    tax_paid: float
    tax_terminal: float
    costs: float
    sales_by_year: dict
    orders: int
    months: int
    detected_share: float              # share of position-days in codes without proventos data
    unexplained: list = field(default_factory=list)
    idle_cash_share: float = 0.0       # average share of wealth in idle cash
    carry_end: float = 0.0             # stock losses still carried after liquidation
    realized: float = 0.0              # net realized stock gains before liquidation


class Prices:
    """Sessions, closes (as traded), share factors and proventos for every tradable code."""

    def __init__(self, sessions: pd.DatetimeIndex, codes: dict, etf: pd.DataFrame, company: dict):
        self.sessions = sessions
        self.pos = {d: i for i, d in enumerate(sessions)}
        closes = {c: k.close for c, k in codes.items()}
        for c in etf.columns:
            closes[c] = etf[c].dropna()
        self.raw = pd.DataFrame(closes).reindex(sessions)
        self.last = self.raw.ffill()
        self.traded = self.raw.notna()
        self.col = {c: j for j, c in enumerate(self.raw.columns)}
        self.last_np = self.last.to_numpy()
        self.raw_np = self.raw.to_numpy()
        self.traded_np = self.traded.to_numpy()
        self.splits = {c: dict(k.splits) for c, k in codes.items()}
        self.prov = {}
        for c, k in codes.items():
            if len(k.proventos):
                idx = sessions[np.searchsorted(sessions, k.proventos.index).clip(max=len(sessions) - 1)]
                self.prov[c] = pd.Series(k.proventos.values, index=idx).groupby(level=0).sum().to_dict()
        self.detected = {c for c, k in codes.items() if k.source.startswith("detected")}
        self.company = company
        last_trade = self.raw.apply(lambda s: s.last_valid_index())
        first_trade = self.raw.apply(lambda s: s.first_valid_index())
        self.first_trade, self.last_trade = first_trade.to_dict(), last_trade.to_dict()


def month_ends(sessions: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    s = sessions.to_series()
    ends = list(s.groupby(s.dt.to_period("M")).max())
    return [d for d in ends if start <= d <= end]


def run(px: Prices, targets, start: pd.Timestamp, end: pd.Timestamp, initial: float, monthly: float,
        params: Params | None = None, decision_months=(6, 12), etf: bool = False) -> Result:
    """``targets(t)``: the selection at decision date ``t`` (list of codes)."""
    params = params or Params()
    sessions = px.sessions
    ends = month_ends(sessions, start, end)
    end_i = px.pos[sessions[sessions <= end][-1]]
    cash, qty, cost_basis = 0.0, {}, {}
    receivable: list[tuple[pd.Timestamp, float]] = []
    carry, tax_paid, costs, orders, realized = 0.0, 0.0, 0.0, 0, 0.0
    sales_by_year: dict[int, float] = {}
    flows, nav_s, unit_s = [], {}, {}
    units, unit_value = 0.0, 1.0
    target: list[str] = []
    detected_days, held_days, idle_sum = 0, 0, 0.0
    unexplained = []
    month_sales, month_gain, month_key = 0.0, 0.0, None
    plan = None                                   # (execution index, decision date, is_decision, flow)

    def price(c, i):
        v = px.last_np[i, px.col[c]]
        return float(v) if v == v else None

    def traded(c, i):
        return bool(px.traded_np[i, px.col[c]])

    def settle_month(key):
        nonlocal month_sales, month_gain, carry, tax_paid, cash
        if key is None:
            return
        if etf:
            return
        if month_sales > EXEMPT:
            base = month_gain - carry
            tax = TAX * max(base, 0.0)
            carry = max(-base, 0.0)
            cash -= tax
            tax_paid += tax
        else:
            carry += max(-month_gain, 0.0)
        month_sales, month_gain = 0.0, 0.0

    def sell(c, q, i):
        nonlocal cash, costs, orders, month_sales, month_gain
        p = price(c, i)
        if p is None or q <= 0:
            return
        gross = q * p
        fee = gross * params.cost
        basis = cost_basis[c] * q / qty[c]
        cash += gross - fee
        costs += fee
        orders += 1
        if etf:
            gain = gross - fee - basis
            tax = TAX * max(gain, 0.0)
            cash -= tax
            nonlocal tax_paid
            tax_paid += tax
        else:
            month_sales += gross
            month_gain += gross - fee - basis
            nonlocal realized
            realized += gross - fee - basis
        sales_by_year[sessions[i].year] = sales_by_year.get(sessions[i].year, 0.0) + gross
        cost_basis[c] -= basis
        qty[c] -= q
        if qty[c] <= 1e-9:
            qty.pop(c)
            cost_basis.pop(c)

    def buy(c, amount, i):
        nonlocal cash, costs, orders
        p = price(c, i)
        if p is None or amount <= 0:
            return
        q = math.floor(min(amount, cash) / (p * (1 + params.cost)))
        if q <= 0:
            return
        spend = q * p
        fee = spend * params.cost
        cash -= spend + fee
        costs += fee
        orders += 1
        qty[c] = qty.get(c, 0.0) + q
        cost_basis[c] = cost_basis.get(c, 0.0) + spend + fee

    start_i = px.pos[ends[0]]
    end_index = {px.pos[d]: d for d in ends}
    for i in range(start_i, end_i + 1):
        day = sessions[i]
        # Calendar month of tax: settle the previous month when a new one starts.
        key = (day.year, day.month)
        if key != month_key:
            settle_month(month_key)
            month_key = key
        # Events on held codes.
        for c in list(qty):
            f = px.splits.get(c, {}).get(day)
            if f:
                qty[c] *= f
            d = px.prov.get(c, {}).get(day)
            if d:
                net = qty[c] * d * (1 - params.jcp_share * JCP_TAX)
                receivable.append((day + pd.Timedelta(days=30), net))
        if receivable:
            due = [a for when, a in receivable if when <= day]
            if due:
                cash += sum(due)
                receivable = [(w, a) for w, a in receivable if w > day]
        # Codes that stopped trading: a successor of the same company and class, or a sale at the last close.
        for c in list(qty):
            last = px.last_trade.get(c)
            if last is not None and last < day and px.pos[last] <= i - 10:
                succ = [o for o in px.raw.columns if o != c and px.company.get(o) == px.company.get(c)
                        and o[4:] == c[4:] and px.first_trade.get(o) is not None
                        and abs(px.pos.get(px.first_trade[o], -99) - px.pos[last]) <= 10 and traded(o, i)]
                if succ:
                    o = succ[0]
                    value = qty[c] * float(px.last.at[last, c])
                    new_q = value / float(px.raw.at[px.first_trade[o], o])
                    qty[o] = qty.get(o, 0.0) + new_q
                    cost_basis[o] = cost_basis.get(o, 0.0) + cost_basis.pop(c)
                    qty.pop(c)
                    target[:] = [o if x == c else x for x in target]
                else:
                    sell(c, qty[c], px.pos[last])
        # Execution of last month's plan.
        if plan is not None and plan[0] == i:
            _, t, is_decision, first = plan
            ti = px.pos[t]
            nav_t = cash + sum(r for _, r in receivable) + sum(q * (price(c, ti) or 0.0) for c, q in qty.items())
            each = nav_t / (1 if etf else params.holdings)
            # Sales: out of the selection, largest first; then overweights in decision months.
            wanted = []
            for c in sorted(qty, key=lambda c: -qty[c] * (price(c, ti) or 0.0)):
                if c not in target:
                    wanted.append((c, qty[c]))
            if is_decision and not etf:
                for c in target:
                    if c in qty and price(c, ti):
                        excess = qty[c] * price(c, ti) - each
                        if excess > 0:
                            wanted.append((c, math.floor(excess / price(c, ti))))
            planned = 0.0
            for c, q in wanted[:params.max_sells]:
                p = price(c, ti) or 0.0
                if not etf and planned + q * p > PLAN_CAP:
                    q = math.floor(max(PLAN_CAP - planned, 0.0) / p) if p else 0
                if q > 0 and traded(c, i):
                    sell(c, q, i)
                    planned += q * p
            # Purchases: the largest shortfalls against the target, within the cash.
            nav_e = cash + sum(r for _, r in receivable) + sum(q * (price(c, i) or 0.0) for c, q in qty.items())
            each = nav_e / (1 if etf else params.holdings)
            gaps = sorted(((each - qty.get(c, 0.0) * (price(c, i) or 0.0), c) for c in target
                           if c in px.col and traded(c, i)), reverse=True)
            for gap, c in gaps[:params.first_buys if first else params.max_buys]:
                if gap > 0:
                    buy(c, gap, i)
            plan = None
        # Wealth, units and the month's flow and plan.
        nav = cash + sum(r for _, r in receivable) + sum(q * (price(c, i) or 0.0) for c, q in qty.items())
        if units:
            unit_value = nav / units
        if i in end_index:
            t = end_index[i]
            first = not flows
            flow = initial if first else monthly
            flows.append((t, flow))
            cash += flow
            units += flow / unit_value
            nav += flow
            is_decision = first or t.month in decision_months
            if is_decision:
                target[:] = [c for c in targets(t) if c in px.col]
            if i < end_i:
                plan = (i + 1, t, is_decision, first)
        nav_s[day], unit_s[day] = nav, unit_value
        idle_sum += cash / nav if nav > 0 else 0.0
        for c in qty:
            held_days += 1
            detected_days += c in px.detected
            j = px.col[c]
            if i > 0 and px.traded_np[i, j] and px.traded_np[i - 1, j]:
                r = px.raw_np[i, j] / px.raw_np[i - 1, j]
                if (r > 1.6 or r < 0.4) and not px.splits.get(c, {}).get(day):
                    unexplained.append((day.date().isoformat(), c, round(float(r), 3)))
    before = nav
    # Liquidation on the last day: everything sold, taxed together with the month's other stock sales.
    pre_tax = tax_paid
    for c in list(qty):
        sell(c, qty[c], end_i)
    cash += sum(r for _, r in receivable)
    settle_month(month_key)
    return Result(pd.Series(nav_s), pd.Series(unit_s), flows, cash, before, tax_paid, tax_paid - pre_tax, costs,
                  sales_by_year, orders, len(ends), detected_days / held_days if held_days else 0.0,
                  sorted(set(unexplained)), idle_sum / max(len(nav_s), 1), carry, realized)
