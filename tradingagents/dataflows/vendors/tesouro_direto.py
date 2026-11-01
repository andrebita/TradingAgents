"""Tesouro Direto: the bonds on offer and their rates, from the Treasury's open data (Tesouro Transparente).

The dataset has one row per bond and day with the buying rate and unit price; the last day is the offer.
Cached for a day.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from tradingagents.dataflows.config import get_config

URL = ("https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/"
       "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv")
TTL = 24 * 60 * 60


@dataclass
class Offer:
    kind: str            # "Tesouro Selic", "Tesouro IPCA+", "Tesouro Prefixado", ...
    maturity: date
    rate: float          # % a year: the spread over Selic, the real rate over IPCA, or the fixed rate
    unit_price: float    # R$ per bond
    as_of: date

    @property
    def name(self) -> str:
        return f"{self.kind} {self.maturity.year}"


def _csv() -> bytes:
    path = Path(get_config()["data_cache_dir"]) / "tesouro" / "PrecoTaxaTesouroDireto.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or time.time() - path.stat().st_mtime > TTL:
        for attempt in range(4):
            try:
                response = requests.get(URL, timeout=120)
                response.raise_for_status()
                path.write_bytes(response.content)
                break
            except requests.RequestException:
                if path.exists():
                    break
                time.sleep(3 * 2 ** attempt)
        else:
            raise RuntimeError("Tesouro Direto rates unavailable")
    return path.read_bytes()


def parse(raw: bytes, as_of: date | None = None) -> list[Offer]:
    frame = pd.read_csv(io.BytesIO(raw), sep=";", decimal=",")
    frame["Data Base"] = pd.to_datetime(frame["Data Base"], dayfirst=True)
    frame["Data Vencimento"] = pd.to_datetime(frame["Data Vencimento"], dayfirst=True)
    if as_of is not None:
        frame = frame[frame["Data Base"] <= pd.Timestamp(as_of)]
    last = frame["Data Base"].max()
    day = frame[frame["Data Base"] == last]
    return [Offer(r["Tipo Titulo"], r["Data Vencimento"].date(), float(r["Taxa Compra Manha"]),
                  float(r["PU Compra Manha"]), last.date())
            for _, r in day.iterrows() if r["Data Vencimento"] > last and float(r["PU Compra Manha"]) > 0]


def offers(as_of: date | None = None) -> list[Offer]:
    return parse(_csv(), as_of)


def selic_to_buy(available: list[Offer], today: date, years: int = 5) -> Offer | None:
    """The Tesouro Selic bond to buy: the highest spread among those maturing within ``years``."""
    limit = date(today.year + years, today.month, min(today.day, 28))
    selic = [o for o in available if o.kind == "Tesouro Selic" and o.maturity <= limit]
    return max(selic, key=lambda o: (o.rate, -o.maturity.toordinal())) if selic else None
