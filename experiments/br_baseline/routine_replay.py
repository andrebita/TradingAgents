"""Replay of the real routine: the production selection and order planner, month by month, 2012-2026.

Review 3 (A2, B1) found that the backtest tested a frictionless, fixed-value
portfolio while the routine buys whole shares with monthly contributions, sells
only within the R$ 20,000 monthly exemption, defers the rest, pays income tax and
freezes the target between rebalances. This replays the routine's own code
(``selection``, ``routine.frozen_target``, ``routine.same_company_codes``,
``orders.plan_orders``) on the data of "Dividendos entre as grandes", with the
same cash flows into DIVO11, BOVA11 and the CDI. The design is pre-registered in
the README ("Replay da rotina real").

Usage:
    python experiments/br_baseline/routine_replay.py [--out experiments/br_baseline/results/routine_replay]
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from dividend_tilt import fca, sector_at, series

from tradingagents.dataflows.vendors import b3_market
from tradingagents.dividend_portfolio import routine, selection
from tradingagents.dividend_portfolio.orders import EXEMPT_MONTHLY_SALES, TAX_RATE, plan_orders
from tradingagents.portfolio import PortfolioContext, Position

HERE = Path(__file__).resolve().parent
CACHE = HERE / "results" / "dividends" / "cache"
FIRST, LAST = "2012-03-01", "2026-09-30"      # decision months (pre-registered)
COST = 0.0015                                 # per order, on its value
MONTHLY = 3000.0
SCALES = {"A": 0.0, "B": 100_000.0}           # initial money; R$ 3,000 every month on top
VARIANTS = {"semestral": (6, 12), "trimestral": (3, 6, 9, 12)}


def load_prices(codes: set[str]) -> dict[str, pd.DataFrame]:
    """The cached Yahoo series, cleaned by the production rule (``b3_market.clean``)."""
    out = {}
    for code in codes:
        path = CACHE / "prices" / f"{code}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(frame):
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
            out[code], _, _ = b3_market.clean(frame.sort_index())
    return out


def close_at(frame: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """The close on ``day``, or the last one within 10 days before it."""
    s = frame["Close"].loc[:day]
    if not len(s) or s.index[-1] < day - pd.Timedelta(days=10):
        return None
    return float(s.iloc[-1])


def xirr(flows: list[tuple[pd.Timestamp, float]]) -> float:
    """Annual rate that zeroes the flows (negative: money in; positive: money out), by bisection."""
    t0 = flows[0][0]

    def npv(rate):
        return sum(v / (1 + rate) ** ((t - t0).days / 365.25) for t, v in flows)

    lo, hi = -0.99, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def unit_stats(values: pd.Series, flows: pd.Series) -> dict:
    """Time-weighted return and drawdown from month-end values (before that month's flow) and the flows."""
    r = values / (values.shift(1) + flows.shift(1)) - 1
    r = r.iloc[1:].replace([np.inf, -np.inf], np.nan).dropna()
    r = r[(values.shift(1) + flows.shift(1)).reindex(r.index) > 0]
    unit = (1 + r).cumprod()
    years = (unit.index[-1] - unit.index[0]).days / 365.25
    return {"cagr_twr": float(unit.iloc[-1] ** (1 / years) - 1) if years > 0 else None,
            "max_drawdown_twr": float((unit / unit.cummax().clip(lower=1.0) - 1).min())}


def replay(companies, px, sessions, days, rules, initial) -> dict:
    cash, held, cost_basis = 0.0, {}, {}
    recorded, pending, carry = None, set(), 0.0
    values, flows, cash_seen = {}, {}, []
    tax_paid, taxable_months, deferred_months, orders, sold_total = 0.0, 0, 0, 0, 0.0
    last_exec = None
    snaps: dict = {}
    for i, t in enumerate(days):
        # Proventos with ex-dates since the last execution, on what was held since then.
        if last_exec is not None:
            for code, q in held.items():
                if code in px and q > 0:
                    div = px[code]["Dividends"]
                    cash += q * float(div[(div.index > last_exec) & (div.index <= t)].sum())
        value = cash + sum(q * (close_at(px[c], t) or 0.0) for c, q in held.items() if c in px)
        values[t] = value
        flow = MONTHLY + (initial if i == 0 else 0.0)
        flows[t] = flow
        cash += flow

        rebalance = t.month in rules.rebalance_months
        snap = snaps.get(t)
        if snap is None:
            snap = snaps[t] = selection.snapshot(companies, px, t, rules)
        if rebalance or recorded is None:
            chosen = selection.target(snap, rules)
            recorded = list(chosen["code"])
        else:
            chosen = routine.frozen_target(recorded, snap, companies, rules)
        priced = {c for c in px if close_at(px[c], t) is not None}
        target = routine.same_company_codes(chosen, companies, set(held), priced)
        codes = list(target["code"])
        prices = {c: close_at(px[c], t) for c in set(codes) | set(held) if c in px and close_at(px[c], t)}
        keep = {c for c in set(codes) | set(held) if c in px and selection.is_suspect(px[c], t, rules)}
        book = PortfolioContext(cash=cash, positions=[
            Position(ticker=c, quantity=q, average_price=cost_basis[c]) for c, q in held.items() if q > 0])
        plan = plan_orders(book, codes, prices, sell=rebalance, pending=pending & set(held), keep=keep,
                           loss_carryforward=carry)
        pending = {o.code for o in plan.orders + plan.deferred if o.side == "venda"}
        deferred_months += bool(plan.deferred)

        # Execution at the next session's close: sales first, then buys within the cash.
        after = sessions[sessions > t]
        if not len(after):
            break
        ex = after[0]
        sales, gain = 0.0, 0.0
        for o in (o for o in plan.orders if o.side == "venda"):
            p = close_at(px[o.code], ex)
            if p is None:
                continue
            proceeds = o.quantity * p * (1 - COST)
            gain += proceeds - o.quantity * cost_basis[o.code]
            sales += o.quantity * p
            cash += proceeds
            held[o.code] -= o.quantity
            orders += 1
        sold_total += sales
        if sales > EXEMPT_MONTHLY_SALES:
            base = gain - carry
            tax = TAX_RATE * max(base, 0.0)
            carry = max(-base, 0.0)
            cash -= tax
            tax_paid += tax
            taxable_months += 1
        else:
            carry += max(-gain, 0.0)
        for o in (o for o in plan.orders if o.side == "compra"):
            p = close_at(px[o.code], ex)
            if p is None:
                continue
            q = min(o.quantity, int(math.floor(cash / (p * (1 + COST)))))
            if q <= 0:
                continue
            spent = q * p * (1 + COST)
            old = held.get(o.code, 0)
            cost_basis[o.code] = (old * cost_basis.get(o.code, 0.0) + spent) / (old + q)
            held[o.code] = old + q
            cash -= spent
            orders += 1
        held = {c: q for c, q in held.items() if q > 0}
        cash_seen.append(cash)
        last_exec = ex

    end = sessions[-1]
    for code, q in held.items():
        if code in px:
            div = px[code]["Dividends"]
            cash += q * float(div[(div.index > last_exec) & (div.index <= end)].sum())
    final = cash + sum(q * (close_at(px[c], end) or 0.0) for c, q in held.items() if c in px)
    v, f = pd.Series(values), pd.Series(flows)
    return {"final_value": round(final, 2), "contributed": round(float(f.sum()), 2),
            "irr": xirr([(t, -x) for t, x in flows.items()] + [(end, final)]), **unit_stats(v, f),
            "tax_paid": round(tax_paid, 2), "taxable_months": taxable_months, "months_with_deferred_sales":
            deferred_months, "orders": orders, "sales_total": round(sold_total, 2),
            "mean_idle_cash": round(float(np.mean(cash_seen)), 2), "holdings_at_end": sorted(held),
            "unrealized_tax_note": "valor final antes do IR sobre ganhos não realizados"}, v, f


def benchmark(price: pd.Series, days, initial, end) -> dict:
    """The same flows into a fund, fractional units, at each decision day's close."""
    units, values, flows = 0.0, {}, {}
    for i, t in enumerate(days):
        p = float(price.loc[:t].iloc[-1])
        values[t] = units * p
        flow = MONTHLY + (initial if i == 0 else 0.0)
        flows[t] = flow
        units += flow / p
    final = units * float(price.loc[:end].iloc[-1])
    v, f = pd.Series(values), pd.Series(flows)
    return {"final_value": round(final, 2), "contributed": round(float(f.sum()), 2),
            "irr": xirr([(t, -x) for t, x in flows.items()] + [(end, final)]), **unit_stats(v, f)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "results" / "routine_replay"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    codes, sectors = fca(CACHE)
    companies = {cnpj: b3_market.Company(cnpj, cnpj, set(cs), sector_at(sectors.get(cnpj, {}), 2026))
                 for cnpj, cs in codes.items()}
    px = load_prices(set().union(*codes.values()))
    bvsp, divo, bova = series("^BVSP", CACHE), series("DIVO11.SA", CACHE), series("BOVA11.SA", CACHE)
    sessions = bvsp.index[bvsp.index <= pd.Timestamp("2026-10-30")]
    ends = sessions.to_series().groupby(sessions.to_period("M")).max()
    days = [t for t in ends if pd.Timestamp(FIRST) <= t <= pd.Timestamp(LAST)]
    end = sessions[-1]
    cdi = pd.read_csv(CACHE / "sgs_12.csv", index_col=0, parse_dates=True)["value"]
    cdi_net = (1 + 0.85 * cdi / 100).cumprod()     # 15% income tax on the yield, approximately

    report = {"design": "README, 'Replay da rotina real'", "first_decision": days[0].date().isoformat(),
              "last_decision": days[-1].date().isoformat(), "valued_on": end.date().isoformat(),
              "survivorship": "Yahoo lacks several delisted payers", "results": {}}
    for scale, initial in SCALES.items():
        refs = {"DIVO11": benchmark(divo, days, initial, end), "BOVA11": benchmark(bova, days, initial, end),
                "CDI líquido": benchmark(cdi_net, days, initial, end)}
        for name, months in VARIANTS.items():
            rules = selection.Rules(rebalance_months=months)
            result, v, f = replay(companies, px, sessions, days, rules, initial)
            report["results"][f"{scale}_{name}"] = result
            pd.DataFrame({"value_before_flow": v, "flow": f}).to_csv(out / f"monthly_{scale}_{name}.csv")
            print(f"{scale} {name}: TIR {result['irr']:.2%}, final R$ {result['final_value']:,.0f}, "
                  f"IR R$ {result['tax_paid']:,.0f}", flush=True)
        for name, r in refs.items():
            report["results"][f"{scale}_{name}"] = r
            print(f"{scale} {name}: TIR {r['irr']:.2%}, final R$ {r['final_value']:,.0f}", flush=True)
    b = report["results"]
    report["decision"] = {
        "irr_routine_minus_divo11_B_semestral": b["B_semestral"]["irr"] - b["B_DIVO11"]["irr"],
        "rule": "routine stays as is if >= 0.01; else DIVO11 becomes the default stock vehicle"}
    (out / "replay_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str),
                                            encoding="utf-8")
    print(json.dumps(report["decision"], indent=1))


if __name__ == "__main__":
    print(f"run on {date.today().isoformat()}")
    main()
