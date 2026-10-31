"""The orders that move a book toward the target, with the income tax they would cost.

Brazilian individual investors pay no income tax on swing-trade gains in stocks in
a month whose total stock sales stay within R$ 20,000; above it, 15% on the
month's net gain, after losses carried from earlier months. So the plan:

* sells what left the target, within the month's exempt room when asked to,
  and defers the rest to the following months;
* puts the cash, the month's contribution and the sale proceeds into the target
  stocks furthest below their equal share, in whole shares (the odd-lot market
  trades single shares);
* never trims a winner still in the target: new money evens the weights out,
  which avoids taxable sales;
* between rebalances sells nothing and places new money in the current target,
  so a book starting from cash is built up month by month.

The tax figures are estimates for planning, not tax advice: day trades, other
assets and the rules of each year are out of scope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from tradingagents.portfolio import PortfolioContext

EXEMPT_MONTHLY_SALES = 20_000.0
TAX_RATE = 0.15


@dataclass
class Order:
    code: str
    side: str            # "venda" or "compra"
    quantity: int
    price: float
    reason: str
    gain: float | None = None   # sells: (price - average price) x quantity, if the average is known

    @property
    def value(self) -> float:
        return self.quantity * self.price


@dataclass
class Plan:
    orders: list[Order] = field(default_factory=list)
    deferred: list[Order] = field(default_factory=list)   # sales left for the next months
    sales_this_month: float = 0.0
    taxable: bool = False
    net_gain: float = 0.0
    estimated_tax: float = 0.0
    loss_carryforward_after: float = 0.0
    cash_left: float = 0.0
    target_value: float = 0.0
    notes: list[str] = field(default_factory=list)


def code_of(ticker: str) -> str:
    return ticker.strip().upper().removesuffix(".SA")


def consolidate(book: PortfolioContext) -> dict[str, tuple[float, float | None]]:
    """``{code: (quantity, average price)}``, lots of one code added up across brokers.

    The average is weighted by quantity; when any lot lacks its price, the whole
    position's cost is unknown (a partial average would misstate the gain).
    """
    totals: dict[str, list] = {}
    for p in book.positions:
        entry = totals.setdefault(code_of(p.ticker), [0.0, 0.0, True])
        entry[0] += p.quantity
        if p.average_price is None:
            entry[2] = False
        else:
            entry[1] += p.quantity * p.average_price
    return {c: (q, cost / q if known and q else None) for c, (q, cost, known) in totals.items()}


def plan_orders(book: PortfolioContext, target: list[str], prices: dict[str, float], *,
                contribution: float = 0.0, sold_earlier_this_month: float = 0.0,
                gain_earlier_this_month: float = 0.0, loss_carryforward: float = 0.0,
                band: float = 0.2, stay_exempt: bool = True, sell: bool = True,
                pending: set[str] | None = None, keep: set[str] | None = None) -> Plan:
    """The orders toward ``target`` (codes, equal weights) from ``book`` at ``prices``.

    ``band``: a target stock is topped up only when it sits more than this fraction
    below its equal share. ``stay_exempt``: sell only as much as keeps the month's
    sales within the exemption, deferring the rest. ``sell=False`` (between
    rebalances) sells only the ``pending`` codes (left the target earlier, still
    held), and places new money in the current target. ``keep``: codes neither sold
    nor bought, outside the equal weights (a price under review). Only common
    stocks and units: other assets are taxed differently and do not belong in
    ``book``.
    """
    plan = Plan()
    held = consolidate(book)
    missing = [c for c in set(held) | set(target) if c not in prices]
    if missing:
        plan.notes.append(f"Sem preço para {', '.join(sorted(missing))}: fora do plano.")

    # Sales: what left the target (at a rebalance) and what was deferred before, smallest
    # first, within the exempt room if asked.
    keep = keep or set()
    pending = pending or set()
    to_sell = {c: int(held[c][0]) for c in held
               if (sell or c in pending) and c not in target and c not in keep and c in prices and held[c][0] > 0}
    room = EXEMPT_MONTHLY_SALES - sold_earlier_this_month
    sold = sold_earlier_this_month
    for code in sorted(to_sell, key=lambda c: to_sell[c] * prices[c]):
        qty, avg = to_sell[code], held[code][1]
        price = prices[code]
        now = qty
        if stay_exempt and sold + now * price > EXEMPT_MONTHLY_SALES:
            now = max(0, int(math.floor(max(room, 0) / price)))
        reason = "saiu da seleção" if code not in pending else "venda pendente"
        for quantity, bucket in ((now, plan.orders), (qty - now, plan.deferred)):
            if quantity > 0:
                gain = (price - avg) * quantity if avg is not None else None
                bucket.append(Order(code, "venda", quantity, price, reason, gain))
        sold += now * price
        room -= now * price
    plan.sales_this_month = sold
    gains = [o.gain for o in plan.orders if o.side == "venda"]
    if any(g is None for g in gains):
        plan.notes.append("Preço médio ausente em alguma venda: o ganho dela não entrou na estimativa de IR.")
    net = gain_earlier_this_month + sum(g for g in gains if g is not None)
    plan.net_gain = net
    plan.taxable = sold > EXEMPT_MONTHLY_SALES
    if plan.taxable:
        base = net - loss_carryforward
        plan.estimated_tax = TAX_RATE * max(0.0, base)
        plan.loss_carryforward_after = max(0.0, -base)
    else:
        plan.loss_carryforward_after = loss_carryforward + max(0.0, -net)
    if plan.deferred:
        plan.notes.append("Vendas adiadas para os próximos meses, para manter as vendas do mês em até "
                          "R$ 20 mil (isenção de IR); a rotina as retoma a cada mês.")

    # Buys: equal shares of everything that will be held, funded by cash, contribution and
    # sales, less the tax the sales will owe (kept aside, not spent).
    cash = (book.cash or 0.0) + contribution + sum(o.value for o in plan.orders if o.side == "venda")
    cash -= plan.estimated_tax
    remaining = {c: held[c][0] for c in held}
    for o in plan.orders:
        remaining[o.code] -= o.quantity
    invested = sum(q * prices[c] for c, q in remaining.items() if c in prices and q > 0 and c not in keep)
    priced = [c for c in target if c in prices and c not in keep]
    plan.target_value = (invested + cash) / len(priced) if priced else 0.0
    bought: dict[str, int] = {}

    def value(c: str) -> float:
        return (remaining.get(c, 0) + bought.get(c, 0)) * prices[c]

    # Cash goes to the stocks below their share (beyond the band, or not held at all),
    # in proportion to how far below they are; a second pass, without the band, places
    # what is left once it is worth an order.
    for floor_ in (1 - band, 1.0):
        if floor_ == 1.0 and cash < 0.05 * plan.target_value:
            break
        gaps = {c: plan.target_value - value(c) for c in priced
                if value(c) < plan.target_value * floor_ or (c not in held and not bought.get(c))}
        gaps = {c: g for c, g in gaps.items() if g > 0}
        total = sum(gaps.values())
        if not total:
            continue
        budget = min(cash, total)
        for c, gap in gaps.items():
            quantity = int(budget * gap / total // prices[c])
            if quantity > 0:
                bought[c] = bought.get(c, 0) + quantity
                cash -= quantity * prices[c]
        # Whole shares leave change: one share at a time to the stock furthest below.
        while True:
            open_ = [c for c in gaps if prices[c] <= cash and value(c) + prices[c] <= plan.target_value]
            if not open_:
                break
            c = min(open_, key=lambda c: value(c) / plan.target_value)
            bought[c] = bought.get(c, 0) + 1
            cash -= prices[c]
    for code, quantity in bought.items():
        reason = "entrou na seleção" if code not in held else "abaixo do peso-alvo"
        plan.orders.append(Order(code, "compra", quantity, prices[code], reason))
    plan.cash_left = cash
    return plan
