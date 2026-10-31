"""News on a B3 ticker: its filings with CVM and the Brazilian financial press, together.

The router serves a method from the first vendor in the chain that answers; a
Brazilian stock's news needs both sources, so this vendor joins them. A source
with nothing for the ticker is left out; one that is down is named as such.
Other tickers raise NoMarketDataError and pass to the next vendor (Yahoo).
"""

from __future__ import annotations

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import br_press, cvm


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    if cvm.b3_code(ticker) is None:
        raise NoMarketDataError(ticker, detail="B3 news covers only B3-listed companies (.SA tickers)")
    sections, no_data = [], []
    for label, fetch in (("CVM filings", cvm.get_news), ("Brazilian press", br_press.get_news)):
        try:
            sections.append(fetch(ticker, start_date, end_date).rstrip())
        except NoMarketDataError as exc:
            no_data.append(exc)
        except VendorUnavailableError as exc:
            sections.append(f"## {label}\n<{label} unavailable: {exc}>")
    if not sections:
        raise no_data[0]
    return "\n\n".join(sections) + "\n"
