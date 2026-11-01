"""The committee's index plan, run month by month through past markets with the investor's own money.

The investor's real portfolio comes from the B3 exports (``b3_history``): what it was worth each month and the
money that came from and went to the investor's pocket. The simulation puts that same money through the plan's
code (``indices.build``), with no model calls: the director's discretion is reduced to the equity share, run at
fixed values (the plan's mix, the policy's floor) to show the range it could have taken.

How a month goes:

* **Between meetings** the money that arrives waits in cash earning the CDI (the daily-liquidity post-fixed
  account); a withdrawal comes out of it, and the account may go below zero until the meeting settles it.
* **At the meeting** (the month's last trading day, at the closes): a negative balance is a withdrawal, paid from
  Tesouro Selic first (the oldest bonds, income tax withheld on the gain) and then from the ETFs in proportion
  (15% of the gain, as the DARF would take). A positive balance is the month's contribution. ``indices.build``
  plans the month and the orders are executed at the closes and the Treasury's prices of that day: ETFs in whole
  quotas, Tesouro Selic in fractions.
* **Costs:** the B3 custody fee on Tesouro (``custody`` a year, above ``custody_free``), taxes as above, and the
  ETFs' fees, which are in their prices. No brokerage.

What it cannot show: a director's choices month by month (only its bounds), news the committee would have read,
and how the investor would have kept to the plan in 2022–2026's markets. The plan itself was designed in 2026,
after the selection contest: its mix is not free of hindsight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradingagents.committee import b3_history as h, indices
from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import FixedIncome, Listed, Wealth


@dataclass
class Lot:
    """Tesouro Selic bought on one day."""

    name: str              # "Tesouro Selic 2027"
    quantity: float
    applied_on: date
    applied_value: float

    @property
    def key(self) -> tuple[str, int]:
        return h.bond_key(self.name)


@dataclass
class Market:
    """Closes of the listed codes, the Treasury's prices and the daily CDI (% a day, by date)."""

    closes: dict[str, pd.Series]
    tesouro: h.TesouroPrices
    cdi: pd.Series

    def close(self, code: str, day) -> float | None:
        return h.last_close(self.closes.get(code), day)

    def growth(self, start, end) -> float:
        """What R$ 1 in the daily post-fixed account on ``start`` is worth on ``end``: the CDI of the days after
        ``start`` up to and including ``end``."""
        if pd.Timestamp(end) <= pd.Timestamp(start):
            return 1.0
        r = self.cdi.loc[(self.cdi.index > pd.Timestamp(start)) & (self.cdi.index <= pd.Timestamp(end))]
        return float((1 + r / 100).prod())


@dataclass
class Book:
    cash: float = 0.0
    lots: list[Lot] = field(default_factory=list)
    etfs: dict[str, list[float]] = field(default_factory=dict)     # code: [quantity, total cost]
    taxes: float = 0.0
    fees: float = 0.0

    def lot_value(self, lot: Lot, market: Market, day, column: str = "base") -> float:
        p = market.tesouro.quote(lot.key, day, column)
        if p is None:
            raise ValueError(f"no Treasury price for {lot.name} on {day}")
        return lot.quantity * p

    def selic_value(self, market: Market, day) -> float:
        return sum(self.lot_value(lot, market, day) for lot in self.lots)

    def etf_value(self, market: Market, day) -> dict[str, float]:
        return {c: q * (market.close(c, day) or 0.0) for c, (q, _) in self.etfs.items() if q > 0}

    def value(self, market: Market, day) -> float:
        return self.cash + self.selic_value(market, day) + sum(self.etf_value(market, day).values())


def _redeem(book: Book, market: Market, day, net_needed: float | None = None, gross: float | None = None,
            lots: list[Lot] | None = None) -> float:
    """Sell Tesouro Selic, oldest first, until ``net_needed`` reaches the cash after the tax withheld (or until
    ``gross`` is sold). Returns the cash raised, after tax."""
    raised = 0.0
    for lot in sorted(lots if lots is not None else list(book.lots), key=lambda x: x.applied_on):
        if (net_needed is not None and raised >= net_needed - 0.005) or (gross is not None and gross <= 0.005):
            break
        value = book.lot_value(lot, market, day, "sell")
        if value <= 0:
            continue
        rate = indices.regressive_rate(lot.applied_on, pd.Timestamp(day).date())
        gain_share = max(value - lot.applied_value, 0.0) / value
        net_per_gross = 1 - rate * gain_share
        take = (min(value, (net_needed - raised) / net_per_gross) if net_needed is not None else min(value, gross))
        share = take / value
        tax = rate * gain_share * take
        raised += take - tax
        book.taxes += tax
        if gross is not None:
            gross -= take
        lot.quantity *= 1 - share
        lot.applied_value *= 1 - share
    book.lots = [lot for lot in book.lots if lot.quantity > 1e-9]
    return raised


def _sell_etf(book: Book, code: str, quantity: float, price: float) -> float:
    q, cost = book.etfs[code]
    quantity = min(quantity, q)
    avg = cost / q if q else 0.0
    tax = indices.ETF_TAX * max((price - avg) * quantity, 0.0)
    book.etfs[code] = [q - quantity, cost - avg * quantity]
    book.taxes += tax
    return quantity * price - tax


def _withdraw(book: Book, market: Market, day, amount: float) -> None:
    """Pay ``amount`` from Tesouro Selic, then from the ETFs in proportion to their value."""
    book.cash += _redeem(book, market, day, net_needed=amount)
    short = -book.cash
    if short <= 0.005:
        return
    values = book.etf_value(market, day)
    total = sum(values.values())
    for code, v in values.items():
        p = market.close(code, day)
        q, cost = book.etfs[code]
        gain_share = max(1 - cost / v, 0.0) if v else 0.0
        gross = short * v / total / (1 - indices.ETF_TAX * gain_share)
        book.cash += _sell_etf(book, code, min(math.ceil(gross / p), q), p)


def _mature(book: Book, market: Market, day) -> None:
    for lot in list(book.lots):
        maturity = market.tesouro.maturity(lot.key)
        if maturity is not None and maturity <= pd.Timestamp(day).date():
            book.cash += _redeem(book, market, maturity - pd.Timedelta(days=1), gross=math.inf, lots=[lot])


def wealth_of(book: Book, market: Market, day, vehicles: dict[str, str]) -> Wealth:
    fixed = [FixedIncome(name=lot.name, kind="tesouro_selic", issuer="Tesouro", indexer="SELIC",
                         maturity=market.tesouro.maturity(lot.key), value=book.lot_value(lot, market, day),
                         liquidity_days=1, applied_on=lot.applied_on, applied_value=lot.applied_value)
             for lot in book.lots]
    listed = [Listed(code=c, quantity=q, kind="etf", average_price=cost / q, asset_class=vehicles[c])
              for c, (q, cost) in book.etfs.items() if q > 0]
    return Wealth(as_of=pd.Timestamp(day).date(), fixed_income=fixed, listed=listed)


def _execute(book: Book, plan: indices.Plan, market: Market, day) -> None:
    """The plan's orders at the day's closes and Treasury prices: sales and redemptions first."""
    today = pd.Timestamp(day).date()
    for o in plan.orders:
        if o.side == "venda" and o.asset in book.etfs:
            book.cash += _sell_etf(book, o.asset, o.quantity, o.price)
        elif o.side == "resgatar":
            book.cash += _redeem(book, market, day, gross=o.value)
    for o in plan.orders:
        if o.side == "compra":
            q, cost = book.etfs.get(o.asset, [0.0, 0.0])
            book.etfs[o.asset] = [q + o.quantity, cost + o.quantity * o.price]
            book.cash -= o.quantity * o.price
        elif o.side == "aplicar":
            name = plan.selic_bond
            pu = market.tesouro.quote(h.bond_key(name), day, "buy")
            book.lots.append(Lot(name, o.value / pu, today, o.value))
            book.cash -= o.value


@dataclass
class Result:
    label: str
    months: pd.DataFrame          # date, value, cash, selic, brasil, internacional, contribution, withdrawal, ...
    book: Book
    orders: list[dict]


def simulate(policy: InvestmentPolicy, flows: pd.DataFrame, market: Market, meetings: list[pd.Timestamp], *,
             equity_share: float | None = None, label: str = "", custody: float = 0.002,
             custody_free: float = 10_000.0) -> Result:
    """The plan through ``meetings`` with the investor's ``flows`` (date, amount: positive from the pocket)."""
    ip = policy.index_plan
    vehicles = {ip.international_vehicle: "internacional", **dict.fromkeys(ip.brazil_menu, "acoes")}
    book, rows, log = Book(), [], []
    days = sorted(set(pd.to_datetime(flows["date"])) | set(meetings))
    by_day = flows.groupby(pd.to_datetime(flows["date"]))["amount"].sum()
    history = {c: market.closes[c] for c in vehicles if c in market.closes}
    episode = None
    prev = days[0]
    for day in days:
        book.cash *= market.growth(prev, day)
        prev = day
        _mature(book, market, day)
        book.cash += float(by_day.get(day, 0.0))
        if day not in meetings:
            continue
        fee = custody / 12 * max(book.selic_value(market, day) - custody_free, 0.0)
        book.cash -= fee
        book.fees += fee
        withdrawal = 0.0
        if book.cash < -0.005:
            withdrawal = -book.cash
            _withdraw(book, market, day, withdrawal)
        contribution = max(book.cash, 0.0)
        book.cash -= contribution
        prices = {c: p for c in vehicles if (p := market.close(c, day))}
        wealth = wealth_of(book, market, day, vehicles)
        plan = indices.build(policy, wealth, prices, day.date(), contribution,
                             offers=market.tesouro.offers(day), history=history, episode=episode,
                             equity_share=equity_share)
        episode = plan.crisis.get("episode")
        book.cash += contribution
        _execute(book, plan, market, day)
        etf = book.etf_value(market, day)
        selic = book.selic_value(market, day)
        brazil = sum(v for c, v in etf.items() if vehicles[c] == "acoes")
        rows.append({"date": day, "value": book.cash + selic + sum(etf.values()), "cash": book.cash, "selic": selic,
                     "brasil": brazil, "internacional": etf.get(ip.international_vehicle, 0.0),
                     "contribution": contribution, "withdrawal": withdrawal, "taxes": book.taxes, "fees": book.fees,
                     "crisis": plan.crisis.get("status", "normal"), "selic_bond": plan.selic_bond})
        log += [{"date": day.date().isoformat(), "side": o.side, "asset": o.asset, "value": round(o.value, 2),
                 "quantity": o.quantity, "reason": o.reason} for o in plan.orders]
    return Result(label, pd.DataFrame(rows), book, log)


def latent_tax(book: Book, market: Market, day) -> float:
    """Income tax if everything were sold on ``day``: 15% of the ETFs' gains, the regressive table on Tesouro."""
    tax = 0.0
    for code, (q, cost) in book.etfs.items():
        p = market.close(code, day) or 0.0
        tax += indices.ETF_TAX * max(q * p - cost, 0.0)
    for lot in book.lots:
        v = book.lot_value(lot, market, day, "sell")
        tax += indices.regressive_rate(lot.applied_on, pd.Timestamp(day).date()) * max(v - lot.applied_value, 0.0)
    return tax


def summary(values: pd.Series, flows: pd.DataFrame, final_tax: float = 0.0,
            taxes: pd.DataFrame | None = None) -> dict:
    """The investor's result and the investments' performance, from period-end ``values`` and the investor's dated
    ``flows`` (positive into the portfolio, net of the taxes the investor paid).

    * Result for the investor, after tax: the money-weighted return (XIRR) of the flows and the end value, before
      and after ``final_tax`` (the tax if everything were sold at the end).
    * Performance of the investments, before tax: time-weighted returns, largest fall and volatility, with the
      ``taxes`` paid along the way (date, amount) counted as money leaving the portfolio rather than as a loss, so
      a large sale's tax does not show up as a fall of whatever was left after it.
    """
    pre = flows if taxes is None or taxes.empty else pd.concat(
        [flows[["date", "amount"]], pd.DataFrame({"date": taxes["date"], "amount": -taxes["amount"]})])
    returns = h.modified_dietz(values, pre.sort_values("date"))
    index = (1 + returns).cumprod()
    end = values.index[-1]
    dated = [(d.date(), -a) for d, a in zip(flows["date"], flows["amount"], strict=True)]
    return {"end_value": float(values.iloc[-1]), "end_value_after_tax": float(values.iloc[-1]) - final_tax,
            "latent_tax": final_tax,
            "xirr": h.xirr(dated + [(end.date(), float(values.iloc[-1]))]),
            "xirr_after_tax": h.xirr(dated + [(end.date(), float(values.iloc[-1]) - final_tax)]),
            "twr_annual": h.annualized(returns), "twr_total": float(index.iloc[-1] - 1) if len(index) else 0.0,
            "max_drawdown": h.drawdown(index), "volatility": float(returns.std() * 12 ** 0.5) if len(returns) > 1
            else 0.0, "returns": returns}


def taxes_paid(result: Result) -> pd.DataFrame:
    """The taxes the simulation paid, by meeting: date, amount."""
    m = result.months
    return pd.DataFrame({"date": m["date"], "amount": m["taxes"].diff().fillna(m["taxes"])})
