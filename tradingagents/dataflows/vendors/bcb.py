"""Brazilian macro data from Banco Central do Brasil, point in time.

Two open APIs, no key:

* SGS (Sistema Gerenciador de Séries Temporais): policy rate, CDI, the PTAX
  dollar, IPCA, IGP-M, IBC-Br, unemployment.
* Focus market expectations (Olinda/OData): the median forecast of ~140
  institutions for IPCA, Selic, GDP, the exchange rate and IGP-M.

SGS keeps no vintages and dates a monthly figure by the month it measures, not
by its release, so a series is cut where it had been released by the run date:
each monthly series carries a conservative release lag past the end of its
reference month (IPCA comes out between the 8th and the 12th of the next month). Values are
the current revision; only IBC-Br and unemployment are revised materially.

Focus statistics are computed daily but published weekly: the Monday report
carries the survey up to the previous Friday. A run dated ``D`` reads the
statistics of the Friday before the last Monday on or before ``D``.

Indicators not listed here fall through to the next configured macro vendor
(FRED), so a US ticker's ``cpi`` keeps working.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import date, datetime, timedelta

import requests

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
FOCUS_URL = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
REQUEST_TIMEOUT = 30
DEFAULT_LOOKBACK_DAYS = 365
MAX_ROWS = 40

# alias -> (SGS code, title, unit, frequency, release lag in days after the end
# of the reference month; None for a daily series known on its own date)
SGS_SERIES = {
    "selic": (432, "Selic target rate (Copom)", "% p.a.", "daily", None),
    "selic_effective": (1178, "Selic effective rate", "% p.a.", "daily", None),
    "cdi": (4389, "CDI rate, annualized (252 days)", "% p.a.", "daily", None),
    "usd_brl": (1, "USD/BRL PTAX (sell)", "BRL per USD", "daily", None),
    "ipca": (433, "IPCA consumer inflation, monthly", "% m/m", "monthly", 12),
    "ipca_12m": (13522, "IPCA consumer inflation, 12 months", "% 12m", "monthly", 12),
    "igpm": (189, "IGP-M general prices, monthly", "% m/m", "monthly", 1),
    "ibc_br": (24364, "IBC-Br economic activity index (seasonally adjusted)", "index", "monthly", 50),
    "unemployment_br": (24369, "Unemployment rate (PNAD, rolling quarter)", "%", "monthly", 35),
}

# alias -> Focus indicator name
FOCUS_SERIES = {
    "focus_ipca": "IPCA",
    "focus_selic": "Selic",
    "focus_gdp": "PIB Total",
    "focus_usd_brl": "Câmbio",
    "focus_igpm": "IGP-M",
}

# Other names a model is likely to use for the same series.
_SYNONYMS = {
    "selic_rate": "selic", "brazil_policy_rate": "selic", "brazil_interest_rate": "selic",
    "ptax": "usd_brl", "brl": "usd_brl", "usdbrl": "usd_brl", "dollar_real": "usd_brl", "cambio": "usd_brl",
    "brazil_inflation": "ipca_12m", "brazil_cpi": "ipca_12m", "inflation_br": "ipca_12m",
    "ibcbr": "ibc_br", "brazil_activity": "ibc_br",
    "desemprego": "unemployment_br", "brazil_unemployment": "unemployment_br",
    "focus_pib": "focus_gdp", "focus_cambio": "focus_usd_brl",
}

ALIASES = sorted([*SGS_SERIES, *FOCUS_SERIES])


def _key(indicator: str) -> str:
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    return _SYNONYMS.get(key, key)


def _get(url: str, params: dict) -> list | dict:
    # OData rejects spaces sent as "+", so encode them as %20.
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$,'")
    try:
        response = requests.get(f"{url}?{query}", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(f"Banco Central request failed ({status or type(exc).__name__})") from exc
    except ValueError as exc:
        raise VendorUnavailableError("Banco Central returned an unreadable response") from exc


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def released(reference: date, lag_days: int | None, as_of: date) -> bool:
    """Whether an observation dated ``reference`` was public on ``as_of``."""
    if lag_days is None:
        return reference <= as_of
    return _month_end(reference) + timedelta(days=lag_days) <= as_of


def _sgs(alias: str, as_of_date: str, look_back_days: int) -> str:
    code, title, unit, frequency, lag = SGS_SERIES[alias]
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    # A monthly figure is dated by its month; reach back far enough that the
    # window still holds released values after the lag cut.
    start = as_of - timedelta(days=look_back_days + (lag or 0) + 31)
    data = _get(SGS_URL.format(code=code), {"formato": "json", "dataInicial": f"{start:%d/%m/%Y}",
                                             "dataFinal": f"{as_of:%d/%m/%Y}"})
    points = []
    for row in data if isinstance(data, list) else []:
        try:
            day = datetime.strptime(row["data"], "%d/%m/%Y").date()
            value = float(row["valor"])
        except (KeyError, ValueError, TypeError):
            continue
        # SGS fills the Selic target forward past today; the lag cut drops
        # anything not yet public on the run date.
        if released(day, lag, as_of) and (lag is not None or day >= as_of - timedelta(days=look_back_days)):
            points.append((day, value))

    rule = ("known on its own date" if lag is None
            else f"each month counted as released {lag} days after it ends")
    header = (f"## Banco Central (SGS {code}): {title}\n- Units: {unit}\n- Frequency: {frequency} ({rule})\n"
              f"- As of: {as_of_date}\n")
    if not points:
        return header + "\nNo observation of this series had been released in the window."

    if frequency == "daily":
        # One row per month (its last value) keeps a year of daily data readable.
        by_month: dict[tuple[int, int], tuple[date, float]] = {}
        for day, value in points:
            by_month[(day.year, day.month)] = (day, value)
        shown = list(by_month.values())
        if shown[-1] != points[-1]:
            shown.append(points[-1])
        note = "\n_(last value of each month, then the latest)_\n"
    else:
        shown, note = points, ""
    if len(shown) > MAX_ROWS:
        shown = shown[-MAX_ROWS:]

    (first_day, first), (last_day, last) = points[0], points[-1]
    summary = (f"\n**Latest:** {last:g} ({last_day.isoformat()}) | **Change since {first_day.isoformat()}:** "
               f"{last - first:+.4g} from {first:g}\n")
    table = "\n| Date | Value |\n| --- | --- |\n" + "\n".join(f"| {d.isoformat()} | {v:g} |" for d, v in shown)
    return header + summary + note + table + "\n"


def focus_cutoff(as_of: date) -> date:
    """The last survey day a published Focus report covered by ``as_of``."""
    monday = as_of - timedelta(days=as_of.weekday())
    return monday - timedelta(days=3)


def _focus(alias: str, as_of_date: str, look_back_days: int) -> str:
    indicator = FOCUS_SERIES[alias]
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    cutoff = focus_cutoff(as_of)
    start = cutoff - timedelta(days=look_back_days)
    years = [cutoff.year, cutoff.year + 1, cutoff.year + 2]
    flt = (f"Indicador eq '{indicator}' and baseCalculo eq 0 and Data ge '{start.isoformat()}' "
           f"and Data le '{cutoff.isoformat()}' and ("
           + " or ".join(f"DataReferencia eq '{y}'" for y in years) + ")")
    data = _get(FOCUS_URL, {"$filter": flt, "$orderby": "Data asc", "$format": "json",
                            "$select": "Data,DataReferencia,Mediana,Media,DesvioPadrao,numeroRespondentes",
                            "$top": 5000})
    rows = data.get("value", []) if isinstance(data, dict) else []
    header = (f"## Banco Central Focus survey: market median forecast for {indicator}\n"
              f"- Survey statistics as of {cutoff.isoformat()} (the Focus report published by {as_of_date} "
              f"covers the survey up to that Friday)\n- Units: annual figure for each calendar year "
              f"(Selic: end of year, % p.a.; exchange rate: BRL per USD, end of year)\n")
    if not rows:
        return header + "\nNo Focus statistics were published for this indicator in the window."

    latest_day = max(r["Data"] for r in rows)
    latest = {r["DataReferencia"]: r for r in rows if r["Data"] == latest_day}
    lines = [f"| {y} | {latest[str(y)]['Mediana']:g} | {latest[str(y)]['Media']:g} | "
             f"{latest[str(y)]['DesvioPadrao']:g} | {latest[str(y)]['numeroRespondentes']} |"
             for y in years if str(y) in latest]
    out = (header + f"\n**Latest survey ({latest_day}):**\n\n| Year | Median | Mean | Std dev | Respondents |\n"
           "| --- | --- | --- | --- | --- |\n" + "\n".join(lines) + "\n")

    # How the current-year median moved: one point a week (Fridays) through the window.
    trail = [r for r in rows if r["DataReferencia"] == str(cutoff.year)
             and datetime.strptime(r["Data"], "%Y-%m-%d").date().weekday() == 4]
    if len(trail) > 1:
        trail = trail[-MAX_ROWS:]
        out += (f"\n**Median forecast for {cutoff.year}, weekly:**\n\n| Survey date | Median |\n| --- | --- |\n"
                + "\n".join(f"| {r['Data']} | {r['Mediana']:g} |" for r in trail) + "\n")
    return out


def get_macro_data(indicator: str, as_of_date: str, look_back_days: int | None = None) -> str:
    """A Brazilian macro series or Focus forecast as it stood on ``as_of_date``."""
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS
    alias = _key(indicator)
    if alias in SGS_SERIES:
        return _sgs(alias, as_of_date, look_back_days)
    if alias in FOCUS_SERIES:
        return _focus(alias, as_of_date, look_back_days)
    raise NoMarketDataError(indicator, detail=f"not a Banco Central indicator; BCB aliases: {', '.join(ALIASES)}")
