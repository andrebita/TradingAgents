"""Deterministic market-data verification snapshot.

The market analyst is an LLM that can confabulate exact numbers — citing a
Bollinger band or a "historically validated bounce" that the underlying data
doesn't support (#830). This module computes a ground-truth snapshot (latest
OHLCV row on or before the analysis date, common indicators, recent closes)
the analyst is told to treat as the source of truth for any exact numeric
claim. Deterministic, no LLM involved.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

# A fixed, common indicator set so the snapshot is the same shape every run.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, as_of_date: str) -> pd.DataFrame:
    """OHLCV on or before as_of_date, date-sorted. Raises NoMarketDataError if nothing usable.

    ``load_ohlcv`` already normalizes the Date column and filters out
    look-ahead rows, but we re-apply the cutoff defensively — this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    # As reported: this snapshot is quoted by the agents as exact prices, so a
    # gap-filled cell would put the previous session's number under this date.
    data = load_ohlcv(symbol, as_of_date, fill_gaps=False)
    if data is None or data.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), "no price rows")

    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(as_of_date)].sort_values("Date")
    if df.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), f"no price rows on or before {as_of_date}")
    return df


# Windows, in trading sessions, over which the instrument is set against its benchmark.
RELATIVE_WINDOWS: tuple[int, ...] = (5, 20, 60, 120)
BETA_SESSIONS = 120
# A benchmark close older than this, against an instrument's session, is a gap
# in the benchmark's data, not a day it stood still.
BENCHMARK_STALE = pd.Timedelta(days=5)


def _relative_lines(symbol: str, as_of_date: str, df: pd.DataFrame) -> list[str]:
    """The instrument's returns against its regional benchmark, from closes up to the run date.

    A call is judged on the instrument's return relative to this benchmark, so
    the agents need the benchmark's own path: without it, relative strength and
    beta can only be guessed. Each window ends at the instrument's latest row and
    reads the benchmark at its last close on or before each end.
    """
    from tradingagents.memory.settlement import resolve_benchmark  # memory imports dataflows

    benchmark = resolve_benchmark(symbol, get_config())
    if normalize_symbol(benchmark) == normalize_symbol(symbol):
        return []
    head = ["", f"### Relative to the benchmark ({benchmark})", ""]
    try:
        bench = _verified_rows(benchmark, as_of_date)
    except Exception as exc:  # noqa: BLE001 — the snapshot stands without the comparison
        return head + [f"Benchmark prices unavailable ({type(exc).__name__}); relative strength cannot be computed."]

    own = df.set_index("Date")["Close"].astype(float)
    # Two downloads can carry different datetime resolutions, which merge_asof refuses.
    left = df[["Date"]].astype({"Date": "datetime64[ns]"})
    right = bench[["Date", "Close"]].astype({"Date": "datetime64[ns]"}).rename(columns={"Close": "ref"})
    aligned = pd.merge_asof(left, right, on="Date", direction="backward", tolerance=BENCHMARK_STALE)
    ref = pd.Series(aligned["ref"].astype(float).to_numpy(), index=own.index)
    if pd.isna(ref.iloc[-1]):
        return head + [f"The benchmark's latest close ({_fmt(bench['Date'].iloc[-1])}) is too old to compare "
                       f"with the instrument's ({_fmt(df['Date'].iloc[-1])}); relative strength not computed."]
    lines = head + ["| Window (sessions) | Instrument | Benchmark | Difference |", "|---|---:|---:|---:|"]
    for n in RELATIVE_WINDOWS:
        if len(own) <= n or pd.isna(ref.iloc[-n - 1]):
            continue
        mine = own.iloc[-1] / own.iloc[-n - 1] - 1
        theirs = ref.iloc[-1] / ref.iloc[-n - 1] - 1
        lines.append(f"| {n} | {mine:+.2%} | {theirs:+.2%} | {mine - theirs:+.2%} |")
    daily = pd.DataFrame({"own": own.pct_change(), "ref": ref.pct_change()}).dropna().tail(BETA_SESSIONS)
    if len(daily) >= 30 and daily["ref"].var() > 0:
        beta = daily["own"].cov(daily["ref"]) / daily["ref"].var()
        corr = daily["own"].corr(daily["ref"])
        lines += ["", f"- Beta to {benchmark} over the last {len(daily)} sessions: {beta:.2f} "
                      f"(correlation {corr:.2f})"]
    return lines


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return price_text(value)
    return str(value)


def build_verified_market_snapshot(
    symbol: str,
    as_of_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    # `df` keeps the original capitalized OHLCV columns (Open/High/Low/Close/
    # Volume); stockstats `wrap()` lowercases columns and adds indicator
    # columns, so read raw prices from `df` and indicators from `stock_df`.
    df = _verified_rows(symbol, as_of_date)
    stock_df = wrap(df.copy())

    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # triggers stockstats calculation
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:  # noqa: BLE001 — one bad indicator shouldn't sink the snapshot
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {as_of_date}",
        f"- Latest trading row used: {latest_date}",
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")
    volumes = df["Volume"].tail(20).dropna() if "Volume" in df else pd.Series(dtype=float)
    if len(volumes) >= 10:
        lines.append(f"| Average volume, last {len(volumes)} sessions with volume | "
                     f"{_fmt(int(round(volumes.mean())))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    try:
        lines += _relative_lines(symbol, as_of_date, df)
    except Exception as exc:  # noqa: BLE001 — the comparison is extra; the snapshot must stand without it
        lines += ["", f"Relative strength against the benchmark unavailable ({type(exc).__name__})."]

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
