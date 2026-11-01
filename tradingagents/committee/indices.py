"""The month's plan for the portfolio by index funds (review 5): sleeves, migration, orders, crisis status.

Everything here is code: the director only picks the Brazil ETF from the policy's menu and may hold back up to
two migration sales (``director.check_index``). The plan:

1. **Reserve first.** As in the earlier versions: cash and daily post-fixed money only; the contribution tops it
   up before anything else.
2. **Migration out of what the plan does not hold**, at a set pace:
   - single stocks, sold within the monthly exemption (``stock_sales_cap``), largest first;
   - listed funds, sold down to zero over ``listed_funds_exit_months``, largest first;
   - Tesouro IPCA+ and fixed-rate bonds maturing after ``horizon_end``, redeemed into Tesouro Selic: their
     duration does not fit the horizon (review 4). Those maturing earlier, and bank deposits and debentures
     with liquidity only at maturity, are held to maturity.
3. **Proceeds of single stocks buy the Brazil ETF first** (a swap within the sleeve, so the migration does not
   move money between Brazil and abroad after whatever the market did); **new money and the other proceeds go
   to the sleeves furthest below target**, in reais: the Brazil ETF, the global ETF, Tesouro Selic. A shortfall in equities beyond the band also draws a monthly transfer from Tesouro Selic
   beyond the reserve, over ``transition_months``, never while the reserve is short.
4. **Rebalancing sales** only in June and December, and only for a sleeve more than ``band`` above target.
5. **Crisis status** from each equity ETF's fall from its 12-month high: the written crisis plan applies from
   ``crisis_alert``; from ``crisis_plan`` the letter calls an extraordinary meeting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from tradingagents.committee.policy import InvestmentPolicy
from tradingagents.committee.wealth import Wealth
from tradingagents.dividend_portfolio.report import brl

ETF_TAX, FUND_TAX = 0.15, 0.20
REGRESSIVE = ((180, 0.225), (360, 0.20), (720, 0.175))
SLEEVES = ("brasil", "internacional", "renda_fixa", "fundos_listados")

CRISIS_PLAN = [
    "Não vender ações por causa da queda: o plano foi escolhido sabendo que quedas de 30% ou mais acontecem.",
    "Manter os aportes. O aporte vai para a classe mais abaixo do alvo, que numa queda tende a ser ações.",
    "Se as ações ficarem mais de 5 pontos abaixo do alvo, transferir da Selic acima da reserva para voltar ao "
    "alvo, no ritmo da política: é comprar barato por regra, não por previsão.",
    "Não tocar na reserva de emergência.",
    "A partir de uma queda de 30%, reunião extraordinária com o diretor para revisar o plano com você.",
]


@dataclass
class Order:
    side: str                  # compra, venda, aplicar, resgatar
    asset: str
    value: float
    reason: str
    quantity: float | None = None
    price: float | None = None
    tax: float | None = None   # estimated income tax on a sale; None when it cannot be estimated
    sleeve: str = ""


@dataclass
class Plan:
    day: date
    investable: float
    reserve_target: float
    reserve_eligible: float
    reserve_deficit: float
    sleeves: dict = field(default_factory=dict)          # name: {today, target, after}
    orders: list[Order] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    crisis: dict = field(default_factory=dict)
    brazil_vehicle: str = ""
    selic_bond: str = ""
    selic_rate: float | None = None
    transfer: float = 0.0
    unvalued: list[str] = field(default_factory=list)

    def migration_sales(self) -> list[Order]:
        return [o for o in self.orders if o.side in ("venda", "resgatar") and o.reason.startswith("migração")]

    def facts(self) -> dict:
        return {"day": self.day.isoformat(), "investable": round(self.investable, 2),
                "reserve": {"target": self.reserve_target, "eligible": round(self.reserve_eligible, 2),
                            "deficit": round(self.reserve_deficit, 2)},
                "sleeves": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in self.sleeves.items()},
                "orders": [{"side": o.side, "asset": o.asset, "value": round(o.value, 2), "quantity": o.quantity,
                            "reason": o.reason, "tax": None if o.tax is None else round(o.tax, 2)}
                           for o in self.orders],
                "held_to_maturity": self.holds, "pending": self.pending, "crisis": self.crisis,
                "brazil_vehicle": self.brazil_vehicle, "selic_bond": self.selic_bond,
                "monthly_transfer_to_equities": round(self.transfer, 2), "unvalued": self.unvalued}


@dataclass
class Holding:
    """One code across lots and brokers: quantity summed, average price weighted (None if any lot lacks it)."""

    code: str
    kind: str
    counts_as: str | None
    quantity: float
    average_price: float | None
    value: float | None


def consolidate(wealth: Wealth) -> dict[str, Holding]:
    out: dict[str, Holding] = {}
    cost: dict[str, float | None] = {}
    for x in wealth.listed:
        code = x.code.strip().upper()
        h = out.setdefault(code, Holding(code, x.kind, x.counts_as, 0.0, None, None))
        h.quantity += x.quantity
        if x.value is not None:
            h.value = (h.value or 0.0) + x.value
        known = cost.get(code, 0.0)
        cost[code] = None if known is None or x.average_price is None else known + x.average_price * x.quantity
    for code, h in out.items():
        h.average_price = cost[code] / h.quantity if cost[code] is not None and h.quantity else None
    return out


def regressive_rate(applied_on: date | None, today: date) -> float:
    if applied_on is None:
        return REGRESSIVE[0][1]
    days = (today - applied_on).days
    return next((rate for limit, rate in REGRESSIVE if days <= limit), 0.15)


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def crisis_status(history: dict[str, pd.Series], today: date, alert: float, plan: float) -> dict:
    """Each equity ETF's fall from its high of the last 12 months, and the status it sets."""
    falls = {}
    for code, s in history.items():
        s = s.loc[:pd.Timestamp(today)]
        window = s.loc[pd.Timestamp(today) - pd.DateOffset(months=12):]
        if len(window):
            falls[code] = round(float(window.iloc[-1] / window.max() - 1), 4)
    worst = min(falls.values()) if falls else 0.0
    status = "crise" if -worst >= plan else "atenção" if -worst >= alert else "normal"
    return {"status": status, "falls_from_12m_high": falls, "plan": CRISIS_PLAN if status != "normal" else []}


def build(policy: InvestmentPolicy, wealth: Wealth, prices: dict[str, float], today: date, contribution: float, *,
          offers=None, history: dict[str, pd.Series] | None = None, brazil_vehicle: str | None = None,
          held_back: set[str] | None = None) -> Plan:
    """The month's plan. ``prices``: last close per listed code; ``offers``: Tesouro Direto bonds on offer."""
    ip = policy.index_plan
    held_back = held_back or set()
    vehicle = brazil_vehicle or ip.brazil_default
    menu = set(ip.brazil_menu)

    # -- Where the wealth stands, by sleeve ------------------------------------------------------------
    unvalued = []
    values: dict[str, float] = {}
    for x in wealth.listed:
        code = x.code.strip().upper()
        if code in prices:
            values[code] = values.get(code, 0.0) + x.quantity * prices[code]
        elif x.value is not None:
            values[code] = values.get(code, 0.0) + x.value
        else:
            unvalued.append(code)
    kinds = consolidate(wealth)

    def sleeve_of(code: str) -> str:
        x = kinds[code]
        if code in menu or x.kind == "acao" or x.counts_as == "acoes":
            return "brasil"
        if code == ip.international_vehicle or x.counts_as == "internacional":
            return "internacional"
        if x.counts_as == "renda_fixa":
            return "renda_fixa"
        return "fundos_listados"

    today_by = dict.fromkeys(SLEEVES, 0.0)
    for code, v in values.items():
        today_by[sleeve_of(code)] += v
    fixed = sum(x.value for x in wealth.fixed_income) + wealth.cash
    today_by["renda_fixa"] += fixed
    total = sum(today_by.values())
    reserve = policy.reserve
    eligible = wealth.reserve_eligible()
    deficit = max(reserve - eligible, 0.0)
    investable = max(total - reserve, 0.0)
    base = investable + contribution
    target = {"brasil": ip.brazil * base, "internacional": ip.international * base,
              "renda_fixa": ip.fixed_income * base + reserve, "fundos_listados": 0.0}
    plan = Plan(today, investable, reserve, eligible, deficit, brazil_vehicle=vehicle, unvalued=unvalued)
    if unvalued:
        plan.pending.append("Sem preço nem valor para " + ", ".join(unvalued) + ": o plano não move dinheiro "
                            "entre classes até completar o patrimônio.")
    if offers:
        from tradingagents.dataflows.vendors.tesouro_direto import selic_to_buy

        bond = selic_to_buy(offers, today)
        if bond:
            plan.selic_bond, plan.selic_rate = bond.name, bond.rate
    selic_name = plan.selic_bond or "Tesouro Selic"

    # -- Migration sales ------------------------------------------------------------------------------
    sold_this_month, _, _ = wealth.tax.for_month(today)
    room = max(ip.stock_sales_cap - sold_this_month, 0.0)
    equity_cash, fixed_cash, brazil_cash = 0.0, 0.0, 0.0
    no_cost, no_cost_funds, infra, later = [], [], [], []
    singles = sorted((c for c, x in kinds.items() if x.kind == "acao" and c not in menu and c in prices),
                     key=lambda c: -values.get(c, 0.0))
    for code in singles:
        if code in held_back:
            continue
        x, p = kinds[code], prices[code]
        q = min(x.quantity, math.floor(room / p)) if p > 0 else 0
        if q <= 0:
            later.append(code)
            continue
        plan.orders.append(Order("venda", code, q * p, "migração: ações individuais saem da carteira; isento até "
                                 "R$ 20 mil de vendas no mês", q, p, 0.0, "brasil"))
        room -= q * p
        brazil_cash += q * p
        if x.average_price is None:
            no_cost.append(code)

    if later:
        plan.pending.append("Vendas que ficam para o mês seguinte, pelo limite de isenção: " + ", ".join(later) + ".")
    funds = sorted((c for c, x in kinds.items() if sleeve_of(c) == "fundos_listados" and c in values),
                   key=lambda c: -values[c])
    if funds:
        start = ip.migration_start or today
        left = max(ip.listed_funds_exit_months - months_between(start, today), 1)
        amount = sum(values[c] for c in funds) / left
        for code in funds:
            if amount <= 0 or code in held_back:
                continue
            x = kinds[code]
            p = prices.get(code) or (x.value / x.quantity if x.value and x.quantity else None)
            if not p:
                continue
            q = min(x.quantity, math.ceil(amount / p)) if left > 1 else x.quantity
            gain = (p - x.average_price) * q if x.average_price is not None else None
            reason = ("migração: fundos listados saem da carteira, do maior para o menor, em "
                      f"{ip.listed_funds_exit_months} meses")
            if x.kind == "fi_infra":     # exempt whatever the gain: the average price is not needed for the tax
                tax = 0.0
                reason += "; FI-Infra da Lei 12.431 é isento na venda, se o regulamento confirmar o enquadramento"
                infra.append(code)
            else:
                tax = FUND_TAX * max(gain, 0.0) if gain is not None else None
                if gain is None:
                    no_cost_funds.append(code)
            plan.orders.append(Order("venda", code, q * p, reason, q, p, tax, "fundos_listados"))
            equity_cash += q * p - (tax or 0.0)
            amount -= q * p

    if no_cost:
        plan.pending.append("Informe o preço médio de " + ", ".join(no_cost) + ": as vendas estão dentro da "
                            "isenção, mas o preço médio é preciso para declarar o ganho isento e registrar prejuízos.")
    if no_cost_funds:
        plan.pending.append("Informe o preço médio de " + ", ".join(no_cost_funds) + ": sem ele, o IR na venda "
                            "(20% sobre o ganho, pago por você em DARF) não pode ser calculado.")
    if infra:
        plan.pending.append("Confirme no regulamento de " + ", ".join(infra) + " o enquadramento na Lei 12.431: "
                            "sem ele, a venda pode pagar IR sobre o ganho.")
    horizon = ip.horizon_end
    for b in wealth.fixed_income:
        if b.reserve_eligible:
            continue
        matured = b.maturity is not None and b.maturity <= today
        long_bond = (b.sovereign and b.indexer in ("IPCA", "PRE") and b.liquidity_days is not None
                     and horizon is not None and b.maturity is not None and b.maturity > horizon)
        if matured:
            plan.orders.append(Order("aplicar", selic_name, b.value, f"vencimento de {b.name}", sleeve="renda_fixa"))
            continue
        if long_bond and b.name not in held_back:
            gain = b.value - b.applied_value if b.applied_value is not None else None
            tax = regressive_rate(b.applied_on, today) * max(gain, 0.0) if gain is not None else None
            note = "" if gain is None else (f"; ganho de {brl(gain)}, IR de {brl(tax)}" if gain >= 0
                                            else f"; perda de {brl(-gain)}, sem IR")
            plan.orders.append(Order("resgatar", b.name, b.value, f"migração: vence em {b.maturity.year}, depois do "
                                     f"seu horizonte ({horizon.isoformat()}); o dinheiro vai para a Selic" + note,
                                     tax=tax, sleeve="renda_fixa"))
            fixed_cash += b.value - (tax or 0.0)
            if gain is None:
                plan.pending.append(f"Informe a data e o valor aplicado em {b.name}, para estimar o IR do resgate.")
        else:
            why = "vence dentro do seu horizonte" if b.maturity else "liquidez só no vencimento"
            plan.holds.append(f"{b.name}: manter até {b.maturity or 'o vencimento'} ({why})")

    # -- New money, proceeds and transfer, to the sleeves furthest below target ----------------------------
    after = dict(today_by)
    for o in plan.orders:
        if o.side in ("venda", "resgatar"):
            after[o.sleeve] -= o.value
    after["renda_fixa"] += fixed_cash                       # redemptions stay in fixed income (Selic)
    to_reserve = min(deficit, contribution)
    money = contribution - to_reserve
    after["renda_fixa"] += to_reserve
    equity_gap = (sum(max(target[s] - after[s], 0.0) for s in ("brasil", "internacional"))
                  - equity_cash - brazil_cash - money)
    if equity_gap > policy.band * investable and not deficit and not unvalued:
        beyond = max(eligible + fixed_cash - reserve, 0.0)
        plan.transfer = min(equity_gap / policy.transition_months, beyond)
    cash = equity_cash + money + plan.transfer
    after["renda_fixa"] -= plan.transfer
    # Single stocks swap into the Brazil ETF first: same sleeve, so the swap does not move money between Brazil
    # and abroad at whatever the market did that month; any excess over the Brazil target joins the pool.
    buys: dict[str, float] = {"brasil": min(brazil_cash, max(target["brasil"] - after["brasil"], 0.0))}
    cash += brazil_cash - buys["brasil"]
    while cash > 1.0:
        gaps = {s: target[s] - after[s] - buys.get(s, 0.0) for s in ("brasil", "internacional", "renda_fixa")}
        s = max(gaps, key=gaps.get)
        if gaps[s] <= 1.0:
            s = "renda_fixa"
        amount = cash if gaps[s] <= 1.0 else min(cash, gaps[s])
        buys[s] = buys.get(s, 0.0) + amount
        cash -= amount
    leftover = 0.0
    for s, amount in buys.items():
        if s == "renda_fixa":
            continue
        codes = [ip.international_vehicle] if s == "internacional" else (
            list(ip.brazil_menu) if vehicle == "metade" else [vehicle])
        for code in codes:
            part = amount / len(codes)
            p = prices.get(code)
            if not p:
                plan.pending.append(f"Sem cotação de {code}: a compra fica para a próxima revisão.")
                leftover += part
                continue
            q = math.floor(part / p)
            if q > 0:
                plan.orders.append(Order("compra", code, q * p, "dinheiro novo e da migração para a classe mais "
                                         "abaixo do alvo", q, p, sleeve=s))
                after[s] += q * p
            leftover += part - q * p
    selic_amount = buys.get("renda_fixa", 0.0) + leftover + fixed_cash + to_reserve
    if selic_amount > 1.0:
        rate = f" (Selic + {plan.selic_rate:.2f}% ao ano)" if plan.selic_rate is not None else ""
        plan.orders.append(Order("aplicar", selic_name + rate, selic_amount, "reserva, resgates e o que sobra do "
                                 "mês vão para o pós-fixado", sleeve="renda_fixa"))
        after["renda_fixa"] += selic_amount - fixed_cash - to_reserve
    if plan.transfer:
        plan.orders.append(Order("resgatar", "Tesouro Selic e pós-fixados acima da reserva", plan.transfer,
                                 f"transição para ações, 1/{policy.transition_months} da diferença por mês",
                                 sleeve="renda_fixa"))

    # -- Rebalancing sales in June and December -------------------------------------------------------
    if today.month in (6, 12) and not unvalued:
        for s, code in (("brasil", vehicle if vehicle != "metade" else ip.brazil_default),
                        ("internacional", ip.international_vehicle)):
            excess = after[s] - (target[s] + policy.band * base)
            p = prices.get(code)
            if excess > 0 and p:
                q = math.floor(excess / p)
                if q > 0:
                    x = kinds.get(code)
                    gain = (p - x.average_price) * q if x and x.average_price is not None else None
                    plan.orders.append(Order("venda", code, q * p, "rebalanceamento: classe acima do alvo mais a "
                                             "faixa", q, p, ETF_TAX * max(gain, 0.0) if gain is not None else None, s))

    plan.sleeves = {s: {"today": today_by[s], "target": target[s], "after": after[s]} for s in SLEEVES}
    plan.crisis = crisis_status(history or {}, today, ip.crisis_alert, ip.crisis_plan)
    return plan
