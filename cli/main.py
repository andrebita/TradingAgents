import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
    workers: int = typer.Option(
        1, "--workers", help="Tickers analyzed at once, each date in order; mind the provider's rate limits"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "workers": workers,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


# The plans `login` and `logout` take: the providers signed in to, not keyed.
Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in with your ChatGPT Plus or Pro plan, for the chatgpt provider."""
    from tradingagents.llm_clients import chatgpt_auth

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = chatgpt_auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except chatgpt_auth.ChatGPTAuthError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Signed in to ChatGPT as {escape(email)}.[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of your ChatGPT plan: revoke this machine's access and forget it."""
    from tradingagents.llm_clients import chatgpt_auth

    outcome = chatgpt_auth.sign_out()
    if outcome == "revoked":
        console.print("Signed out of ChatGPT.")
    elif outcome == "forgotten":
        console.print("[yellow]Signed out here, but OpenAI could not be reached to revoke the sign-in; "
                      "it stays valid at OpenAI until it expires.[/yellow]")
    else:
        console.print("Not signed in to ChatGPT.")


carteira = typer.Typer(help="Carteira de dividendos das grandes da B3: seleção por regra, ordens com IR, "
                            "alertas por LLM e carteira-sombra.")
app.add_typer(carteira, name="carteira")


@carteira.command("revisar")
def carteira_revisar(
    arquivo: str = typer.Option(None, "--carteira", help="JSON com posições e caixa (mesmo formato de --portfolio)"),
    aporte: float = typer.Option(0.0, "--aporte", help="Dinheiro novo a investir nesta revisão, em R$"),
    data: str = typer.Option(None, "--data", help="Data da revisão, AAAA-MM-DD (padrão: último pregão)"),
    rebalancear: bool = typer.Option(
        None, "--rebalancear/--sem-rebalancear", help="Força ou impede o rebalanceamento (padrão: junho e dezembro)"
    ),
    sem_llm: bool = typer.Option(False, "--sem-llm", help="Não chama o LLM: sem alertas nem risco de corte"),
    orcamento: float = typer.Option(1.0, "--orcamento", help="Teto de gasto com LLM nesta execução, em US$"),
    vendas_no_mes: float = typer.Option(0.0, "--vendas-no-mes", help="Vendas de ações já feitas neste mês, em R$"),
    ganho_no_mes: float = typer.Option(0.0, "--ganho-no-mes", help="Ganho líquido dessas vendas, em R$"),
    prejuizo_acumulado: float = typer.Option(
        0.0, "--prejuizo-acumulado", help="Prejuízo de meses anteriores ainda a compensar, em R$"
    ),
    renda_fixa: float = typer.Option(None, "--renda-fixa", help="Valor atual em renda fixa, em R$"),
    alvo_acoes: float = typer.Option(
        None, "--alvo-acoes", help="Percentual do patrimônio em ações (0 a 100); divide o aporte e a transição"
    ),
    meses_transicao: int = typer.Option(12, "--meses-transicao", help="Meses para chegar ao alvo de ações"),
    faixa: float = typer.Option(5.0, "--faixa", help="Tolerância em pontos percentuais antes de transferir"),
):
    """Revisa a carteira: seleção, ordens sugeridas com IR estimado, alertas da semana e carteira-sombra."""
    from datetime import date as _date

    from rich.markdown import Markdown

    from tradingagents.dividend_portfolio.routine import review

    try:
        book = load_portfolio(arquivo) if arquivo else None
        day = _date.fromisoformat(data) if data else None
        result = review(book, contribution=aporte, as_of=day, rebalance=rebalancear, use_llm=not sem_llm,
                        budget_usd=orcamento, sold_earlier_this_month=vendas_no_mes,
                        gain_earlier_this_month=ganho_no_mes, loss_carryforward=prejuizo_acumulado,
                        fixed_income=renda_fixa, target_share=None if alvo_acoes is None else alvo_acoes / 100,
                        band=faixa / 100, transition_months=meses_transicao)
    except Exception as exc:  # a bad file, a missing key or an unreachable source is a setup error
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(Markdown(result.report))
    console.print(f"\nRelatório salvo em {result.report_path}")


comite = typer.Typer(help="Comitê de investimentos: cinco analistas e um diretor financeiro, dentro da sua política.")
app.add_typer(comite, name="comite")


@comite.command("reunir")
def comite_reunir(
    politica: str = typer.Option(..., "--politica", help="JSON da política de investimentos"),
    patrimonio: str = typer.Option(..., "--patrimonio", help="JSON do patrimônio completo"),
    aporte: float = typer.Option(None, "--aporte", help="Aporte deste mês, em R$ (padrão: o da política)"),
    data: str = typer.Option(None, "--data", help="Data da reunião, AAAA-MM-DD (padrão: hoje)"),
    sem_llm: bool = typer.Option(False, "--sem-llm", help="Só os fatos e a regra, sem analistas nem diretor"),
):
    """Reúne o comitê: analistas, decisão do diretor conferida pela política e carta ao investidor."""
    from datetime import date as _date

    from rich.markdown import Markdown

    from tradingagents.committee.meeting import hold
    from tradingagents.committee.policy import load_policy
    from tradingagents.committee.wealth import load_wealth

    try:
        meeting = hold(load_policy(politica), load_wealth(patrimonio), contribution=aporte,
                       as_of=_date.fromisoformat(data) if data else None, use_llm=not sem_llm)
    except Exception as exc:  # a bad file, a missing key or an unreachable source is a setup error
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(Markdown(meeting.letter))
    console.print(f"\nCarta salva em {meeting.path} · custo US$ {meeting.spent_usd:.3f}")


if __name__ == "__main__":
    app()
