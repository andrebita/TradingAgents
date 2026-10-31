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
  which avoids taxable sales.

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


def plan_orders(book: PortfolioContext, target: list[str], prices: dict[str, float], *,
                contribution: float = 0.0, sold_earlier_this_month: float = 0.0,
                gain_earlier_this_month: float = 0.0, loss_carryforward: float = 0.0,
                band: float = 0.2, stay_exempt: bool = True, sell: bool = True) -> Plan:
    """The orders toward ``target`` (codes, equal weights) from ``book`` at ``prices``.

    ``band``: a target stock is topped up only when it sits more than this fraction
    below its equal share. ``stay_exempt``: sell only as much as keeps the month's
    sales within the exemption, deferring the rest. ``sell=False`` (between
    rebalances) only places new money, in the target stocks already held.
    """
    plan = Plan()
    held: dict[str, tuple[float, float | None]] = {}
    for p in book.positions:
        code = code_of(p.ticker)
        qty, avg = held.get(code, (0.0, None))
        held[code] = (qty + p.quantity, p.average_price if avg is None else avg)
    missing = [c for c in set(held) | set(target) if c not in prices]
    if missing:
        plan.notes.append(f"Sem preço para {', '.join(sorted(missing))}: fora do plano.")

    # Sales: what left the target, smallest first, within the exempt room if asked.
    room = EXEMPT_MONTHLY_SALES - sold_earlier_this_month
    exits = sorted((c for c in held if sell and c not in target and c in prices and held[c][0] > 0),
                   key=lambda c: held[c][0] * prices[c])
    sold = sold_earlier_this_month
    for code in exits:
        qty, avg = held[code]
        price = prices[code]
        now = int(qty)
        if stay_exempt and sold + now * price > EXEMPT_MONTHLY_SALES:
            now = max(0, int(math.floor(max(room, 0) / price)))
        for quantity, bucket in ((now, plan.orders), (int(qty) - now, plan.deferred)):
            if quantity > 0:
                gain = (price - avg) * quantity if avg is not None else None
                bucket.append(Order(code, "venda", quantity, price, "saiu da seleção", gain))
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
                          "R$ 20 mil (isenção de IR).")

    # Buys: equal shares of everything that will be held, funded by cash, contribution and sales.
    cash = (book.cash or 0.0) + contribution + sum(o.value for o in plan.orders if o.side == "venda")
    remaining = {c: held[c][0] for c in held}
    for o in plan.orders:
        remaining[o.code] -= o.quantity
    invested = sum(q * prices[c] for c, q in remaining.items() if c in prices and q > 0)
    priced = [c for c in target if c in prices and (sell or c in held)]
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
