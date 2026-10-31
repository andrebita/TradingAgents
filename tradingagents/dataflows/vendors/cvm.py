"""Company filings and financial statements of B3-listed issuers, as delivered to CVM.

Yahoo tags no news with a ``.SA`` symbol, so a Brazilian company's news channel
is empty. CVM (Comissão de Valores Mobiliários) publishes every periodic and
event filing a listed company delivers -- material facts (fatos relevantes),
market notices, shareholder notices with dividend/JCP declarations, meeting
minutes -- as open data, each with the date it was delivered. That date is
when the document became public, so a run dated ``D`` is served exactly what
was on file by ``D``: re-deliveries count from their own delivery date.

Open files, no key: the IPE dataset (one zip per delivery year) for the
filings, DFP/ITR for statements, and the FCA dataset (securities by trading
code) to find the issuer of a ticker. Only B3 tickers (``.SA``) are served;
anything else falls through to the next configured vendor.

Point-in-time limits. CVM's FCA and statement files keep only the latest
version of each document. A document whose only kept version was delivered
after ``D`` is therefore left out, and nothing tells the agents why: saying a
period "was later restated" would itself be news from after ``D``. What
cannot be reconstructed is the leaving-out itself, which happens only for
documents re-delivered after ``D``.
"""

from __future__ import annotations

import csv
import functools
import io
import logging
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
_IPE_URL = _BASE + "/IPE/DADOS/ipe_cia_aberta_{year}.zip"
_FCA_URL = _BASE + "/FCA/DADOS/fca_cia_aberta_{year}.zip"

# CVM refreshes the current year's files about weekly; a past year's file only
# gains late deliveries. One fetch a day (a month for past years) serves every
# date a sweep asks about.
_CURRENT_TTL = 24 * 60 * 60
_PAST_TTL = 30 * 24 * 60 * 60
REQUEST_TIMEOUT = 60

# When the window holds no filing, the latest ones before it give the agents
# the company's recent disclosure context; still nothing after the run date.
_EARLIER_DAYS = 90
_EARLIER_MAX = 8

# Monthly reports of the securities insiders hold (CVM Resolution 44, ex-
# Instruction 358, art. 11) carry no subject and would crowd out the material
# facts; they are counted on one line instead of listed.
_HOLDINGS_REPORT = "Valores Mobiliários negociados e detidos"


def _ttl(year: int) -> int:
    return _CURRENT_TTL if year >= date.today().year else _PAST_TTL


def _cached_zip(url: str, name: str, year: int, gaps: list[str] | None = None) -> Path | None:
    """The zip at ``url``, from the cache when fresh; None when CVM has no such file.

    ``gaps`` collects what limits coverage -- a missing year file, a stale copy
    served because the refresh failed -- so a caller never presents an
    incomplete register as a complete one.
    """
    path = Path(get_config()["data_cache_dir"]) / "cvm" / name
    if path.exists() and time.time() - path.stat().st_mtime < _ttl(year):
        return path
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            if gaps is not None:
                gaps.append(f"CVM has no {name} file")
            return None
        response.raise_for_status()
    except requests.RequestException as exc:
        if path.exists():  # a stale copy beats no filings at all
            logger.warning("CVM refresh of %s failed (%s); using the cached copy", name, exc)
            if gaps is not None:
                stamp = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
                gaps.append(f"{name} could not be refreshed; served the copy fetched {stamp}")
            return path
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise VendorUnavailableError(f"CVM open data request failed ({status or type(exc).__name__})") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_bytes(response.content))
    return path


def _rows(path: Path, member_suffix: str) -> list[dict]:
    return _read_rows(str(path), path.stat().st_mtime, member_suffix)


@functools.lru_cache(maxsize=16)
def _read_rows(path: str, mtime: float, member_suffix: str) -> list[dict]:
    """The rows of the CSV in the zip whose name ends with ``member_suffix``.

    Keyed on the file's mtime, so a refreshed download is read again.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            member = next(n for n in archive.namelist() if n.endswith(member_suffix))
            with archive.open(member) as raw:
                return list(csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";"))
    except (zipfile.BadZipFile, StopIteration, OSError) as exc:
        Path(path).unlink(missing_ok=True)  # a broken download is a miss next time
        raise VendorUnavailableError(f"CVM file {Path(path).name} could not be read ({exc})") from exc


def _delivered(path: Path, kind: str, year: int, cnpj: str | None = None) -> dict[str, list[tuple[int, date]]]:
    """``{DT_REFER: [(version, delivery date), ...]}`` from a DFP/ITR/FCA index file,
    for one issuer or (``cnpj`` None) keyed ``CNPJ|DT_REFER`` for all."""
    out: dict[str, list[tuple[int, date]]] = {}
    for row in _rows(path, f"{kind}_cia_aberta_{year}.csv"):
        if cnpj is not None and row.get("CNPJ_CIA") != cnpj:
            continue
        try:
            key = row["DT_REFER"] if cnpj is not None else f"{row['CNPJ_CIA']}|{row['DT_REFER']}"
            out.setdefault(key, []).append((int(row["VERSAO"]), date.fromisoformat(row["DT_RECEB"][:10])))
        except (KeyError, ValueError):
            continue
    return out


def _kept_version_public(filed: list[tuple[int, date]], as_of: date) -> tuple[int, date] | None:
    """The version CVM keeps (the latest), when it was public on ``as_of``; else None.

    Only the latest version's figures are in the files, so an earlier version
    delivered by ``as_of`` cannot be served: its figures are gone.
    """
    version, received = max(filed)
    return (version, received) if received <= as_of else None


def b3_code(ticker: str) -> str | None:
    """The B3 trading code of a ``.SA`` ticker (``itub4.sa`` -> ``ITUB4``), else None.

    A fractional-lot code (``PETR4F``) is the same security as ``PETR4``.
    """
    canonical = normalize_symbol(ticker)
    if not canonical.endswith(".SA"):
        return None
    code = canonical[:-3]
    if len(code) >= 6 and code.endswith("F") and code[-2].isdigit():
        code = code[:-1]
    return code


def issuer_for(code: str, as_of_date: str) -> tuple[str, str] | None:
    """``(cnpj, name)`` of the company whose security traded as ``code`` on ``as_of_date``.

    Read from FCA forms public by then: the run's year and up to four before,
    newest first, each only when its kept version had been delivered by the run
    date. A later year's form is never read, since it would describe tickers
    and issuers as they became after the run date.
    """
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    for y in range(as_of.year, max(as_of.year - 5, 2009), -1):
        path = _cached_zip(_FCA_URL.format(year=y), f"fca_cia_aberta_{y}.zip", y)
        if path is None:
            continue
        # A row belongs to one form (issuer, reference date, version); it counts only
        # when that very form was public, not merely some form of the same issuer.
        public = set()
        for key, filed in _delivered(path, "fca", y).items():
            kept = _kept_version_public(filed, as_of)
            if kept:
                cnpj, refer = key.split("|")
                public.add((cnpj, refer, str(kept[0])))
        for row in _rows(path, f"valor_mobiliario_{y}.csv"):
            form = (row.get("CNPJ_Companhia"), row.get("Data_Referencia"), (row.get("Versao") or "").strip())
            if (row.get("Codigo_Negociacao") or "").strip().upper() == code and form in public:
                return row["CNPJ_Companhia"], row.get("Nome_Empresarial", "")
    return None


def _filings(cnpj: str, start: date, end: date, gaps: list[str]) -> list[dict]:
    """The issuer's filings delivered in ``[start, end]``, latest version of each."""
    found: dict[str, dict] = {}
    for year in range(start.year, end.year + 1):
        path = _cached_zip(_IPE_URL.format(year=year), f"ipe_cia_aberta_{year}.zip", year, gaps)
        if path is None:
            continue
        for row in _rows(path, f"ipe_cia_aberta_{year}.csv"):
            if row.get("CNPJ_Companhia") != cnpj:
                continue
            try:
                delivered = date.fromisoformat(row["Data_Entrega"][:10])
            except (KeyError, ValueError):
                continue
            if not start <= delivered <= end:
                continue
            row = {**row, "_delivered": delivered}
            key = row.get("Protocolo_Entrega") or f"{row.get('Link_Download')}"
            kept = found.get(key)
            if kept is None or _version(row) > _version(kept):
                found[key] = row
    return sorted(found.values(), key=lambda r: (r["_delivered"], r.get("Protocolo_Entrega", "")), reverse=True)


def _version(row: dict) -> int:
    try:
        return int(row.get("Versao") or 1)
    except ValueError:
        return 1


def _line(row: dict) -> str:
    kind = " / ".join(p for p in (row.get("Categoria"), row.get("Tipo"), row.get("Especie")) if p)
    subject = (row.get("Assunto") or "").strip()
    version = f" (re-delivery, version {_version(row)})" if _version(row) > 1 else ""
    ref = row.get("Data_Referencia") or ""
    ref_note = f", reference date {ref}" if ref and ref != row["_delivered"].isoformat() else ""
    return f"- {row['_delivered'].isoformat()} · {kind}: {subject or '(no subject)'}{version}{ref_note}"


def _listing(rows: list[dict], limit: int) -> str:
    """Filings newest first, insiders' holdings reports counted on one line."""
    holdings = [r for r in rows if (r.get("Categoria") or "").startswith(_HOLDINGS_REPORT)]
    filings = [r for r in rows if r not in holdings]
    lines = [_line(r) for r in filings[:limit]]
    if len(filings) > limit:
        lines.append(f"_(showing the {limit} most recent of {len(filings)} filings)_")
    if holdings:
        days = sorted({r["_delivered"].isoformat() for r in holdings})
        lines.append(f"- {len(holdings)} monthly report(s) of securities held by insiders and related "
                     f"parties (CVM art. 11), delivered {', '.join(days)}")
    return "\n".join(lines) + "\n"


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Filings ``ticker``'s issuer delivered to CVM between the two dates, newest first."""
    code = b3_code(ticker)
    if code is None:
        raise NoMarketDataError(ticker, detail="CVM covers only B3-listed companies (.SA tickers)")
    issuer = issuer_for(code, end_date)
    if issuer is None:
        raise NoMarketDataError(ticker, canonical=code, detail="no CVM-registered issuer trades under this code")
    cnpj, name = issuer
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    limit = get_config().get("news_article_limit", 20)

    gaps: list[str] = []
    rows = _filings(cnpj, start, end, gaps)
    header = (f"## Filings by {name} ({code}, CNPJ {cnpj}) delivered to CVM, {start_date} to {end_date}\n"
              "Source: CVM open data (IPE). Official company disclosures, each dated by its delivery "
              "to CVM, when it became public. Only the type and subject of each filing are given, "
              "not its content. Subjects are in Portuguese.\n")
    if gaps:
        header += f"Coverage incomplete: {'; '.join(gaps)}.\n"
    header += "\n"
    if rows:
        return header + _listing(rows, limit)

    earlier = _filings(cnpj, start - timedelta(days=_EARLIER_DAYS), start - timedelta(days=1), gaps)
    body = ("No filing by this company in the window is in CVM's register."
            + (" The register's coverage is incomplete (see above), so this may be missing data.\n" if gaps
               else " CVM's register is complete for the window, so this is an absence of disclosures.\n"))
    if earlier:
        body += (f"\nMost recent earlier filings (up to {_EARLIER_DAYS} days before the window):\n"
                 + _listing(earlier, _EARLIER_MAX))
    return header + body


# --- Financial statements (DFP annual, ITR quarterly) ---------------------------------

_ITR_URL = _BASE + "/ITR/DADOS/itr_cia_aberta_{year}.zip"
_DFP_URL = _BASE + "/DFP/DADOS/dfp_cia_aberta_{year}.zip"

# Statement files per kind; cash flow comes by the indirect method when filed so.
_STATEMENTS = {
    "balance_sheet": ("Balance Sheet", ("BPA", "BPP")),
    "income_statement": ("Income Statement", ("DRE",)),
    "cashflow": ("Cash Flow", ("DFC_MI", "DFC_MD")),
}
_PERIODS = {"quarterly": 4, "annual": 3}
_SCALES = {"MIL": 1000.0, "UNIDADE": 1.0}

# A flow statement carries several spans (the quarter, the year to date). The
# shortest span is served when it covers nearly every account the longest does;
# otherwise the longest, so accounts are never dropped for being reported only
# over the longer span.
_SPAN_COVERAGE = 0.8


def _depth_ok(code: str) -> bool:
    """Top two levels of the chart of accounts, plus basic earnings per share by class."""
    return (code.count(".") <= 1 and not code.startswith("3.99")) or code.startswith("3.99.01.")


@functools.lru_cache(maxsize=64)
def _statement_rows(path: str, mtime: float, member: str, cnpj: str) -> tuple:
    """``(dt_refer, versao, moeda, escala, dt_ini, dt_fim, cd_conta, ds_conta, vl_conta)``
    rows of one issuer's current-period accounts in one statement file; empty if
    the file lacks it."""
    with zipfile.ZipFile(path) as archive:
        if member not in archive.namelist():
            return ()
        with archive.open(member) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="latin-1"), delimiter=";")
            head = {name: i for i, name in enumerate(next(reader))}
            out = []
            for row in reader:
                if row[head["CNPJ_CIA"]] != cnpj or not row[head["ORDEM_EXERC"]].startswith("Ú"):
                    continue
                code = row[head["CD_CONTA"]]
                if not _depth_ok(code):
                    continue
                out.append((row[head["DT_REFER"]], row[head["VERSAO"]], row[head["MOEDA"]],
                            row[head["ESCALA_MOEDA"]],
                            row[head["DT_INI_EXERC"]] if "DT_INI_EXERC" in head else "",
                            row[head["DT_FIM_EXERC"]], code, row[head["DS_CONTA"]], row[head["VL_CONTA"]]))
    return tuple(out)


def _documents(cnpj: str, as_of: date, freq: str) -> list[dict]:
    """The statement documents whose figures were public on ``as_of``, newest period first.

    A period whose kept (latest) version was delivered after ``as_of`` is left
    out silently: see the module docstring.
    """
    plan = [("dfp", y) for y in range(as_of.year - 3, as_of.year + 1)]
    if freq == "quarterly":
        plan += [("itr", y) for y in range(as_of.year - 1, as_of.year + 1)]
    docs = []
    for kind, year in plan:
        url = (_DFP_URL if kind == "dfp" else _ITR_URL).format(year=year)
        path = _cached_zip(url, f"{kind}_cia_aberta_{year}.zip", year)
        if path is None:
            continue
        for refer, filed in _delivered(path, kind, year, cnpj).items():
            kept = _kept_version_public(filed, as_of)
            if kept:
                docs.append({"kind": kind, "refer": refer, "version": str(kept[0]),
                             "received": kept[1], "path": path, "year": year})
    docs.sort(key=lambda d: d["refer"], reverse=True)
    return docs[:_PERIODS[freq]]


def _span(rows: list[tuple]) -> tuple[str, str]:
    """The ``(dt_ini, dt_fim)`` span a statement's column is served over (see ``_SPAN_COVERAGE``)."""
    by_span: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        # Earnings per share does not count towards completeness: a span holding
        # only per-share lines would otherwise displace one holding the accounts.
        codes = by_span.setdefault((r[4], r[5]), set())
        if not r[6].startswith("3.99"):
            codes.add(r[6])
    widest = max(len(codes) for codes in by_span.values())
    usable = [s for s, codes in by_span.items() if len(codes) >= _SPAN_COVERAGE * widest]
    return max(usable)  # latest start: the shortest span among those that are complete


def _values(doc: dict, cnpj: str, members: tuple[str, ...]) -> tuple[dict, str]:
    """``{code: (label, value in R$, is_eps)}`` for one document, and its period label."""
    kind, year, path = doc["kind"], doc["year"], doc["path"]
    for basis in ("con", "ind"):
        values, label, omitted = {}, "", set()
        for member in members:
            rows = [r for r in _statement_rows(str(path), path.stat().st_mtime,
                                               f"{kind}_cia_aberta_{member}_{basis}_{year}.csv", cnpj)
                    if r[0] == doc["refer"] and r[1] == doc["version"]]
            if not rows:
                continue
            ini, end = _span(rows)
            # Accounts the chosen span lacks but a longer one reports are left out of
            # the column (a column mixing spans would be wrong); the label says so.
            omitted |= ({r[6] for r in rows if not r[6].startswith("3.99")}
                        - {r[6] for r in rows if (r[4], r[5]) == (ini, end)})
            for _refer, _, currency, scale, r_ini, r_end, code, name, raw in rows:
                if (r_ini, r_end) != (ini, end):
                    continue
                if currency.upper() != "REAL" or scale.upper() not in _SCALES:
                    logger.warning("CVM %s %s %s: unexpected currency/scale %s/%s; skipped",
                                   cnpj, doc["refer"], code, currency, scale)
                    continue
                try:
                    amount = float(raw)
                except ValueError:
                    continue
                eps = code.startswith("3.99")
                values[code] = (name, amount if eps else amount * _SCALES[scale.upper()], eps)
            label = f"{ini}..{end}" if ini else end
            if members[0].startswith("DFC") and values:
                break  # one cash-flow method is the statement
        if values:
            note = f" ({len(omitted)} account(s) reported only over a longer span left out)" if omitted else ""
            return values, label + ("" if basis == "con" else " (individual)") + note
    return {}, ""


def _code_key(code: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in code.split("."))


def _statement(kind: str, ticker: str, freq: str, as_of_date: str | None) -> str:
    code = b3_code(ticker)
    if code is None:
        raise NoMarketDataError(ticker, detail="CVM covers only B3-listed companies (.SA tickers)")
    as_of_date = as_of_date or date.today().isoformat()
    issuer = issuer_for(code, as_of_date)
    if issuer is None:
        raise NoMarketDataError(ticker, canonical=code, detail="no CVM-registered issuer trades under this code")
    cnpj, name = issuer
    title, members = _STATEMENTS[kind]
    freq = freq if freq in _PERIODS else "quarterly"
    docs = _documents(cnpj, datetime.strptime(as_of_date, "%Y-%m-%d").date(), freq)

    columns = []
    for doc in docs:
        values, label = _values(doc, cnpj, members)
        if values:
            columns.append((doc, label, values))
    if not columns:
        raise NoMarketDataError(ticker, canonical=code, detail=f"no {title.lower()} filed with CVM by {as_of_date}")

    header = (f"# {title} for {name} ({code}) as filed with CVM by {as_of_date}\n"
              "Source: CVM open data (DFP annual, ITR quarterly), as filed: these are the statement "
              "lines, not the issuer's adjusted or managerial figures. Values in R$ millions, "
              "consolidated unless marked individual; earnings per share in R$. Each column covers "
              "the span in its heading; columns of different lengths (a quarter, a year to date, a "
              "full year) must not be compared as growth or margins. Account names are in Portuguese.\n")
    filed = "; ".join(f"{d['refer']}: {d['kind'].upper()} delivered {d['received']}" for d, _, _ in columns)
    header += f"Filings: {filed}\n"

    codes = sorted({c for _, _, v in columns for c in v}, key=_code_key)
    labels = {c: v[c][0] for _, _, v in reversed(columns) for c in v}
    lines = ["| Account | " + " | ".join(label for _, label, _ in columns) + " |",
             "| --- |" + " --- |" * len(columns)]
    for c in codes:
        cells = []
        for _, _, v in columns:
            if c not in v:
                cells.append("")
            else:
                _, amount, eps = v[c]
                cells.append(f"{amount:.4g}" if eps else f"{amount / 1e6:,.1f}")
        lines.append(f"| {c} {labels[c]} | " + " | ".join(cells) + " |")
    return header + "\n" + "\n".join(lines) + "\n"


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("balance_sheet", ticker, freq, as_of_date)


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("income_statement", ticker, freq, as_of_date)


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return _statement("cashflow", ticker, freq, as_of_date)
