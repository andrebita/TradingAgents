"""Backtest of the committee's index plan against the investor's real portfolio, from the B3 exports.

    python experiments/backtest_comite/run.py \
        --negociacao negociacao.xlsx --movimentacao movimentacao.xlsx --politica politica_indices.json \
        --eventos eventos.json --cotahist spot.csv.gz --out saida/ [--desde 2024-05-01]

* ``--eventos``: the conversions, offering purchases and quantity adjustments the exports do not explain on their
  own (see ``tradingagents.committee.b3_history``), as JSON: ``{"conversions": [{"day", "source", "to",
  "quantity"?}], "extra_trades": [{"day", "code", "quantity", "value"}], "adjustments": [{"day", "code",
  "delta"}]}``. The script prints the codes whose quantity at the end differs from ``--posicao`` (a wealth file)
  so they can be found and listed.
* ``--cotahist``: B3's COTAHIST closes as traded (``experiments/br_baseline/cotahist/parse.py`` makes it); codes
  missing there fall back to the Yahoo cache in ``--precos``.
* The Tesouro prices and the CDI come from the caches the vendors keep (``--tesouro``, ``--cdi``).

Writes ``resumo.json`` (per period: real, the plan at 60% and 40% in equities, Tesouro Selic only, gross CDI),
monthly values and returns, the simulated orders, and the flows. Everything it reads and writes is the
investor's own data: keep ``--out`` outside the repository.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.committee import b3_history as h, backtest as bt
from tradingagents.committee.b3_import import read_trades
from tradingagents.committee.policy import load_policy

FUND_NAME = re.compile(r"\bFII\b|FUNDO|\bFDO\b|FIAGRO|IMOBILI|\bFIC\b|\bFI\b", re.IGNORECASE)
SCENARIOS = (("Comitê 60% ações", None), ("Comitê 40% ações", 0.40), ("Só Tesouro Selic", 0.0))


def events_from(path: Path | None):
    if path is None:
        return [], [], []
    c = json.loads(path.read_text(encoding="utf-8"))
    conv = [h.Conversion(date.fromisoformat(x["day"]), x["source"], x.get("to"), x.get("quantity"))
            for x in c.get("conversions", [])]
    extra = [h.ExtraTrade(date.fromisoformat(x["day"]), x["code"], x["quantity"], x["value"])
             for x in c.get("extra_trades", [])]
    adj = [h.Adjustment(date.fromisoformat(x["day"]), x["code"], x["delta"]) for x in c.get("adjustments", [])]
    return conv, extra, adj


def fund_codes(movements: pd.DataFrame, extra) -> set[str]:
    """Listed funds (FII, Fiagro, FI-Infra), by the name the movements export gives the product."""
    named = movements[movements["product"].str.contains(FUND_NAME)]
    return set(named["code"]) | {t.code for t in extra if t.code.endswith(("11", "12", "13", "14", "15"))
                                 and t.code[:4] in {c[:4] for c in named["code"]}}


def closes_for(codes, cotahist: Path, cache: Path) -> dict[str, pd.Series]:
    spot = pd.read_csv(cotahist, usecols=["date", "code", "close"], parse_dates=["date"])
    spot = spot[spot["code"].isin(codes)]
    out = {c: g.set_index("date")["close"].sort_index() for c, g in spot.groupby("code")}
    for c in codes:
        if c not in out and (cache / f"{c}.csv").exists():
            y = pd.read_csv(cache / f"{c}.csv", parse_dates=["Date"]).set_index("Date")["Close"]
            if len(y) > 1:
                out[c] = y
    return out


def fund_costs(trades, listed, extra) -> dict[str, float]:
    """Average cost of each code at the end, carrying conversions (as ``b3_history.listed_taxes``)."""
    qty, cost, pend = {}, {}, {}
    rows = [(r.date, 0, r.code, r.side, r.quantity, r.value) for r in trades.itertuples()]
    rows += [(pd.Timestamp(t.day), 0, t.code, "compra", t.quantity, t.value) for t in extra]
    rows += [(r.date, 1, r.code, r.source, r.delta, 0.0) for r in listed.itertuples()
             if r.source not in ("negociação", "oferta")]
    for d, _, c, s, q, v in sorted(rows, key=lambda x: (x[0], x[1])):
        if s == "compra":
            qty[c], cost[c] = qty.get(c, 0) + q, cost.get(c, 0) + v
        elif s == "venda":
            held = qty.get(c, 0)
            if held > 1e-9:
                cost[c] -= cost[c] / held * min(q, held)
            qty[c] = held - q
        elif s == "conversão":
            if q < 0:
                pend[d], qty[c] = cost.pop(c, 0.0), 0
            else:
                qty[c], cost[c] = qty.get(c, 0) + q, cost.get(c, 0) + pend.pop(d, 0.0)
        else:
            qty[c] = qty.get(c, 0) + q
    return {c: cost[c] / qty[c] for c in qty if qty[c] > 1e-9 and c in cost}


def latent_real(trades, listed, tes, closes, prices, extra, funds, end) -> float:
    """Tax if the real portfolio were sold on ``end``: Tesouro by lot and the regressive table, listed funds 20% of
    the gain, stocks nothing (sold within the monthly exemption)."""
    tax, lots = 0.0, {}
    for r in tes.sort_values("date").itertuples():
        if r.delta > 0:
            lots.setdefault(r.bond, []).append([r.date, r.delta, r.value / r.delta])
            continue
        q = -r.delta
        for lot in lots.get(r.bond, []):
            take = min(q, lot[1])
            lot[1] -= take
            q -= take
    for bond, ls in lots.items():
        p = prices.quote(h.bond_key(bond), end, "sell")
        tax += sum(h.regressive_rate((end - d).days) * max((p - c) * q, 0.0) for d, q, c in ls if q > 1e-9)
    costs = fund_costs(trades, listed, extra)
    for code, q in h.holdings(listed, end).items():
        if code in funds and code in costs:
            tax += 0.20 * max((h.last_close(closes.get(code), end) - costs[code]) * q, 0.0)
    return tax


def period(name, start, end, net_flows, policy_path, out, *, trades, listed, tes, closes, prices, market, extra,
           fallback, taxes, funds):
    """Real and simulated from ``start``; what the real portfolio was worth the day before is the first flow."""
    first = start - pd.Timedelta(days=1)
    meetings = h.month_ends(closes["BOVA11"].index, start, end)
    v0 = h.value_on(first, listed, tes, closes, prices, fallback)["total"]
    f = net_flows[net_flows["date"] > first][["date", "amount"]]
    if v0 > 0:
        f = pd.concat([pd.DataFrame({"date": [first], "amount": [v0]}), f]).reset_index(drop=True)
    rows = [{"date": first - pd.Timedelta(days=1), "value": 0.0}]
    for d in [first] + meetings:
        v = h.value_on(d, listed, tes, closes, prices, fallback)
        if v["missing"]:
            raise SystemExit(f"sem preço em {d.date()}: {v['missing']}")
        rows.append({"date": d, "value": v["total"], "listed": v["listed"], "tesouro": v["tesouro"]})
    real = pd.DataFrame(rows).set_index("date")
    policy = load_policy(policy_path)
    policy.index_plan.migration_start = start.date()
    last = meetings[-1]
    real_taxes = taxes[taxes["date"] > first].rename(columns={"tax": "amount"})[["date", "amount"]]
    res = {"Real": bt.summary(real["value"], f, latent_real(trades, listed, tes, closes, prices, extra, funds, last),
                              real_taxes)}
    res["Real"]["taxes_paid"] = float(real_taxes["amount"].sum())
    series, zero = {"Real": real["value"]}, pd.Series({first - pd.Timedelta(days=1): 0.0})
    for label, share in SCENARIOS:
        r = bt.simulate(policy, f, market, meetings, equity_share=share, label=label)
        v = pd.concat([zero, r.months.set_index("date")["value"]])
        series[label] = v
        res[label] = bt.summary(v, f, bt.latent_tax(r.book, market, last), bt.taxes_paid(r))
        res[label]["taxes_paid"], res[label]["fees"] = r.book.taxes, r.book.fees
        tag = re.sub(r"\W+", "_", label.lower())
        r.months.to_csv(out / f"{name}_sim_{tag}.csv", index=False)
        (out / f"{name}_ordens_{tag}.json").write_text(json.dumps(r.orders, ensure_ascii=False, indent=1))
    cash, prev, cdi_rows = 0.0, first - pd.Timedelta(days=1), []
    by_day = f.groupby("date")["amount"].sum()
    for d in sorted(set(by_day.index) | set(meetings)):
        cash = cash * market.growth(prev, d) + float(by_day.get(d, 0.0))
        prev = d
        if d in meetings:
            cdi_rows.append({"date": d, "value": cash})
    series["CDI bruto"] = pd.concat([zero, pd.DataFrame(cdi_rows).set_index("date")["value"]])
    res["CDI bruto"] = bt.summary(series["CDI bruto"], f, 0.0)
    pd.DataFrame(series).to_csv(out / f"{name}_valores.csv")
    rets = pd.DataFrame({k: o.pop("returns") for k, o in res.items()})
    rets.to_csv(out / f"{name}_retornos.csv")
    yearly = (1 + rets).groupby(rets.index.year).prod() - 1
    res["_anos"] = {str(y): {k: float(x) for k, x in row.items()} for y, row in yearly.iterrows()}
    res["_flows"] = {"in": float(f[f.amount > 0].amount.sum()), "out": float(-f[f.amount < 0].amount.sum())}
    print(f"===== {name} ({start.date()} a {last.date()})")
    for k, o in res.items():
        if not k.startswith("_"):
            print(f"{k:18s} final R$ {o['end_value']:>12,.2f}  após IR R$ {o['end_value_after_tax']:>12,.2f}  "
                  f"TIR após IR {o['xirr_after_tax'] or 0:7.2%}  TWR antes do IR {o['twr_annual']:7.2%}")
    return res


def default_start(movements: pd.DataFrame, flows: pd.DataFrame) -> pd.Timestamp:
    """The first month of flows after the last sale of a Tesouro bond the export never shows being bought (held
    from before the export's first date): before it, the money in the portfolio is not all in the export."""
    held, orphan = {}, None
    for r in h.tesouro_events(movements).sort_values("date", kind="stable").itertuples():
        held[r.bond] = held.get(r.bond, 0.0) + r.delta
        if held[r.bond] < -1e-6:
            orphan, held[r.bond] = r.date, 0.0
    later = flows[flows["date"] > orphan] if orphan is not None else flows
    start = later["date"].min().to_period("M").to_timestamp()
    if orphan is not None:
        print(f"Tesouro vendido em {orphan.date()} sem a compra no extrato: o backtest começa em {start.date()}.")
    return start


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--negociacao", type=Path, required=True)
    p.add_argument("--movimentacao", type=Path, required=True)
    p.add_argument("--politica", type=Path, required=True)
    p.add_argument("--eventos", type=Path)
    p.add_argument("--posicao", type=Path, help="wealth file to check the end quantities against")
    p.add_argument("--cotahist", type=Path, required=True)
    p.add_argument("--precos", type=Path, default=Path.home() / ".tradingagents/cache/b3_prices")
    p.add_argument("--tesouro", type=Path, default=Path.home() / ".tradingagents/cache/tesouro/PrecoTaxaTesouroDireto.csv")
    p.add_argument("--cdi", type=Path, default=Path.home() / ".tradingagents/cache/b3_prices/sgs_12.csv")
    p.add_argument("--inicio", help="first month (default: the first flow in the scope)")
    p.add_argument("--fim", help="last day (default: the last day of the movements export)")
    p.add_argument("--desde", action="append", default=[], help="also a period from this date (repeatable)")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)

    trades = read_trades(a.negociacao)
    mv = h.read_movements(a.movimentacao)
    conv, extra, adj = events_from(a.eventos)
    flows = h.external_flows(trades, mv, extra)
    start = pd.Timestamp(a.inicio) if a.inicio else default_start(mv, flows)
    end = pd.Timestamp(a.fim) if a.fim else mv["date"].max()
    mv = mv[mv["date"] >= start]
    flows = flows[flows["date"] >= start].reset_index(drop=True)
    listed = h.listed_events(trades, mv, conv, extra, adj)
    tes = h.tesouro_events(mv)
    funds = fund_codes(mv, extra)
    if a.posicao:
        held, pos = h.holdings(listed, end), {}
        for x in json.loads(a.posicao.read_text(encoding="utf-8")).get("listed", []):
            pos[x["code"]] = pos.get(x["code"], 0) + x["quantity"]
        diff = {c: (held.get(c, 0), pos.get(c, 0)) for c in set(held) | set(pos)
                if abs(held.get(c, 0) - pos.get(c, 0)) > 0.01}
        print("quantidades que não batem (extratos, posição):", diff or "nenhuma")
    closes = closes_for(sorted(set(listed["code"]) | {"BOVA11", "WRLD11"}), a.cotahist, a.precos)
    prices = h.TesouroPrices(h.read_tesouro_prices(a.tesouro))
    cdi = pd.read_csv(a.cdi, index_col=0, parse_dates=True)["value"]
    market = bt.Market(closes, prices, cdi)
    taxes = h.realized_taxes(trades, listed, tes, funds, extra)
    net = pd.concat([flows, pd.DataFrame({"date": taxes["date"], "amount": taxes["tax"],
                                          "source": "IR " + taxes["source"], "code": ""})])
    net = net.sort_values("date").reset_index(drop=True)
    net.to_csv(a.out / "fluxos.csv", index=False)
    ctx = {"trades": trades, "listed": listed, "tes": tes, "closes": closes, "prices": prices, "market": market,
           "extra": extra, "fallback": {t.code: t.value / t.quantity for t in extra}, "taxes": taxes, "funds": funds}
    out = {"periodo_completo": period("periodo_completo", start, end, net, a.politica, a.out, **ctx)}
    for d in a.desde:
        out[f"desde_{d}"] = period(f"desde_{d}", pd.Timestamp(d), end, net, a.politica, a.out, **ctx)
    (a.out / "resumo.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
