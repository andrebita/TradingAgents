"""The routine's report, in Portuguese, as Markdown."""

from __future__ import annotations

import pandas as pd

from tradingagents.dividend_portfolio.monitor import CutRisk, NewsAlert
from tradingagents.dividend_portfolio.orders import EXEMPT_MONTHLY_SALES, Plan

LEVEL = {"normal": "🟢 normal", "atenção": "🟡 atenção", "alerta": "🔴 alerta"}


def brl(x: float) -> str:
    return "R$ " + f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{100 * x:+.{digits}f}%".replace(".", ",")


def render(as_of: str, rebalance: bool, target: pd.DataFrame, holdings: list[dict], plan: Plan | None,
           alerts: dict[str, object], cut: dict[str, object], shadow: dict | None, spent: float,
           llm: bool) -> str:
    kind = "Rebalanceamento semestral" if rebalance else "Revisão semanal"
    out = [f"# Carteira de dividendos: {kind.lower()} de {as_of}", "",
           "> Regra testada nos backtests do projeto, sem garantia de resultado e sem caráter de recomendação. "
           "A inteligência artificial lê e alerta; ela não escolhe ações nem decide ordens.", ""]

    out += ["## Seleção-alvo", "", "As 10 maiores médias de dividendos de 24 meses entre as 50 ações mais negociadas "
            "que pagaram em cada um dos últimos 3 anos, com pesos iguais.", "",
            "| Ação | Empresa | Setor | DY 12m | DY 24m | Risco de corte (LLM) |", "|---|---|---|---|---|---|"]
    for _, r in target.iterrows():
        risk = cut.get(r["code"])
        risk_txt = (f"{risk.probability_cut:.0f}%" if isinstance(risk, CutRisk) else (risk or "—"))
        out.append(f"| {r['code']} | {r['name']} | {r['sector']} | {pct(r['dy12']).lstrip('+')} | "
                   f"{pct(r['dy24']).lstrip('+')} | {risk_txt} |")
    if cut:
        out += ["", "Risco de corte: probabilidade estimada pelo LLM, a partir dos demonstrativos anonimizados, de os "
                "proventos dos próximos 12 meses caírem 30% ou mais. É um alerta para planejar a renda: no backtest, "
                "excluir essas ações reduziu o retorno, porque o mercado já precifica o corte."]

    out += ["", "## Sua carteira", ""]
    if holdings:
        out += ["| Ação | Quantidade | Preço | Valor | Peso | Na seleção |", "|---|---|---|---|---|---|"]
        total = sum(h["value"] for h in holdings) or 1
        for h in holdings:
            quantity = f"{h['quantity']:,.0f}".replace(",", ".")
            weight = f"{100 * h['value'] / total:.1f}%".replace(".", ",")
            out.append(f"| {h['code']} | {quantity} | {brl(h['price'])} | {brl(h['value'])} | {weight} | "
                       f"{'sim' if h['in_target'] else 'não'} |")
    else:
        out.append("Nenhuma posição informada.")

    if plan is not None:
        out += ["", "## Ordens sugeridas", ""]
        if plan.orders:
            out += ["| Ordem | Ação | Quantidade | Preço | Valor | Motivo |", "|---|---|---|---|---|---|"]
            for o in plan.orders:
                out.append(f"| {o.side} | {o.code} | {o.quantity} | {brl(o.price)} | {brl(o.value)} | {o.reason} |")
        else:
            out.append("Nenhuma ordem: a carteira está dentro da regra.")
        if plan.deferred:
            out += ["", "Vendas adiadas (próximos meses): " + ", ".join(f"{o.code} {o.quantity}" for o in plan.deferred)]
        out += ["", f"- Vendas no mês: {brl(plan.sales_this_month)} (isenção até {brl(EXEMPT_MONTHLY_SALES)})",
                f"- IR estimado sobre ganhos: {brl(plan.estimated_tax)}"
                + (" (vendas acima da isenção)" if plan.taxable else " (dentro da isenção)"),
                f"- Prejuízo a compensar depois: {brl(plan.loss_carryforward_after)}",
                f"- Valor-alvo por ação: {brl(plan.target_value)}; caixa que sobra: {brl(plan.cash_left)}"]
        out += [f"- {n}" for n in plan.notes]
        out += ["", "Estimativa de IR para planejamento, não é orientação tributária."]

    out += ["", "## Notícias e documentos da semana", ""]
    if not llm:
        out.append("Leitura por LLM desligada nesta execução.")
    elif not alerts:
        out.append("Nenhuma posição para acompanhar.")
    for code, alert in alerts.items():
        if alert is None:
            out.append(f"- **{code}**: nada entregue à CVM nem publicado na imprensa acompanhada.")
        elif isinstance(alert, NewsAlert):
            out.append(f"- **{code}** · {LEVEL.get(alert.level, alert.level)}"
                       f"{' · dividendos' if alert.dividend_news else ''}: {alert.summary}")
        else:
            out.append(f"- **{code}**: {alert}")

    out += ["", "## Carteira-sombra", ""]
    if shadow:
        out += [f"Todas as seleções registradas desde {shadow['since']}, compradas no pregão seguinte, com proventos "
                f"reinvestidos e sem custos, até {shadow['to']}.", "",
                "| | Retorno no período |", "|---|---|",
                f"| Carteira-sombra | {pct(shadow['portfolio'])} |"]
        for name, label in (("bova11", "BOVA11 (Ibovespa)"), ("divo11", "DIVO11 (dividendos)")):
            if name in shadow:
                out.append(f"| {label} | {pct(shadow[name])} |")
    else:
        out.append("Ainda sem histórico: a primeira seleção registrada começa a contar no pregão seguinte.")

    out += ["", f"_Custo de LLM desta execução: US$ {spent:.3f}._"]
    return "\n".join(out) + "\n"
