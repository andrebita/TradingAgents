"""Brazilian financial press on a B3 ticker, from the publishers' own WordPress feeds.

Yahoo tags no news with a ``.SA`` symbol. Several Brazilian financial news
sites run WordPress, whose public REST API (``/wp-json/wp/v2/posts``) searches
the archive by text and date, keyless, and returns each post's full text with
its publication time and the time of its last edit. This module asks a few of
them for posts naming the ticker's trading code (``PETR4``) in the window.

Point in time. A post counts from its publication time, and only when its text
was last edited before the window closed: the text served today is the latest
edit, which for a post edited after the run date may carry later facts. Such
posts are left out without saying how many, since the count itself would come
from after the run date. Deleted posts are missing.

This is a sample of the press, a handful of free sites, not all coverage. Their
REST endpoints are public but not an offered API: responses are cached, so a
replayed window asks nothing twice.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from html import unescape
from pathlib import Path

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window, to_utc
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors.cvm import b3_code

logger = logging.getLogger(__name__)

PORTALS = {
    "InfoMoney": "https://www.infomoney.com.br",
    "Money Times": "https://www.moneytimes.com.br",
    "Seu Dinheiro": "https://www.seudinheiro.com",
    "BP Money": "https://bpmoney.com.br",
    "NeoFeed": "https://neofeed.com.br",
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TradingAgents research)"}
_PER_PAGE = 50
_PAGES = 2
REQUEST_TIMEOUT = 30
# A window that ended this long ago is settled: its cached answer is kept for
# good. A more recent one is asked again after the live TTL.
_SETTLED_DAYS = 2
_LIVE_TTL = 60 * 60
# With fewer characters of the budget left, an article is not cut to fit.
_MIN_EXCERPT = 1_500

# A B3 trading code: four letters and the class number (PETR4, KLBN11, ROXO34).
_CODE = re.compile(r"\b[A-Z]{4}\d{1,2}\b")
_SKIP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_BLOCK = re.compile(r"</?(?:p|br|li|h[1-6]|div|tr|blockquote|ul|ol|table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _plain(html: str) -> str:
    """Readable text of a post's HTML: tags dropped, one line per paragraph."""
    text = _BLOCK.sub("\n", _SKIP.sub(" ", html or ""))
    lines = [" ".join(line.split()) for line in unescape(_TAG.sub("", text)).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _about(text: str, code: str) -> str:
    """The paragraphs about ``code`` in an article covering several companies.

    A run starts at each paragraph naming ``code`` and goes on until one names
    other companies only: the sections of a daily roundup, the lines of a
    market wrap.
    """
    keep, on = [], False
    for para in text.split("\n"):
        codes = set(_CODE.findall(para))
        if code in codes:
            on = True
        elif codes:
            on = False
        if on and para.strip():
            keep.append(para)
    return "\n\n".join(keep)


def _parse(stamp: str | None) -> datetime | None:
    try:
        return to_utc(datetime.fromisoformat(stamp)) if stamp else None
    except ValueError:
        return None


def _query(base: str, code: str, start: date, end: date) -> list[dict]:
    """Posts ``base`` returns for ``code`` around the window, newest first.

    The site filters by its own local time, so the request reaches a day either
    side; the window itself is applied on the UTC timestamps afterwards.
    """
    posts = []
    for page in range(1, _PAGES + 1):
        response = requests.get(f"{base}/wp-json/wp/v2/posts", headers=_HEADERS, timeout=REQUEST_TIMEOUT, params={
            "search": code, "after": f"{start - timedelta(days=1)}T00:00:00",
            "before": f"{end + timedelta(days=1)}T23:59:59", "orderby": "date", "order": "desc",
            "per_page": _PER_PAGE, "page": page, "_fields": "date_gmt,modified_gmt,title,link,content"})
        if response.status_code == 400 and page > 1:
            break  # WordPress answers 400 past the last page
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        if len(batch) < _PER_PAGE:
            break
    return posts


def _cached_query(name: str, base: str, code: str, start: date, end: date) -> list[dict]:
    path = (Path(get_config()["data_cache_dir"]) / "br_press" / re.sub(r"\W", "_", name.lower())
            / f"{code}_{start}_{end}.json")
    settled = end <= date.today() - timedelta(days=_SETTLED_DAYS)
    if path.exists() and (settled or time.time() - path.stat().st_mtime < _LIVE_TTL):
        return json.loads(path.read_text(encoding="utf-8"))
    posts = _query(base, code, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8"))
    return posts


def _articles(name: str, posts: list[dict], code: str, start: date, end: date) -> list[dict]:
    """The posts published in the window, not edited after it, that name ``code``."""
    window_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
    window_end = datetime(end.year, end.month, end.day, tzinfo=UTC)
    mention = re.compile(rf"\b{re.escape(code)}\b", re.I)
    found = []
    for post in posts:
        published, edited = _parse(post.get("date_gmt")), _parse(post.get("modified_gmt"))
        # Both stamps must be known: an undated post or edit cannot be placed before the run date.
        if published is None or edited is None or not in_window(published, window_start, window_end):
            continue
        # The last edit closes by the window's end, as publication does (a scheduled
        # post may have been last edited before it went out).
        if edited >= window_end + timedelta(days=1):
            continue
        title = _plain((post.get("title") or {}).get("rendered", ""))
        text = _plain((post.get("content") or {}).get("rendered", ""))
        mentions = len(mention.findall(title)) + len(mention.findall(text))
        if not mentions:
            continue
        others = set(_CODE.findall(title) + _CODE.findall(text)) - {code}
        # 2: an article about the company; 1: one of several in the headline; 0: named in passing.
        focus = 0 if not mention.search(title) else 1 if set(_CODE.findall(title)) - {code} else 2
        # A roundup or market wrap is cut to its paragraphs about the company; an
        # article headlined on the company alone keeps its comparisons.
        several = focus < 2 and len(others) >= 2
        found.append({"site": name, "published": published, "title": title,
                      "text": _about(text, code) if several else text, "excerpt": several,
                      "link": post.get("link", ""), "focus": focus, "mentions": mentions})
    return found


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Press articles naming ``ticker``'s trading code, published in the window, most about it first."""
    code = b3_code(ticker)
    if code is None:
        raise NoMarketDataError(ticker, detail="the Brazilian press feeds cover only B3-listed companies (.SA tickers)")
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()

    def ask(item):
        name, base = item
        try:
            return name, _articles(name, _cached_query(name, base, code, start, end), code, start, end), None
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning("Press feed %s failed for %s: %s", name, code, exc)
            return name, [], f"HTTP {status}" if status else type(exc).__name__

    with ThreadPoolExecutor(max_workers=len(PORTALS)) as pool:
        answers = list(pool.map(ask, PORTALS.items()))
    down = [f"{name} ({why})" for name, _, why in answers if why]
    if len(down) == len(PORTALS):
        raise VendorUnavailableError(f"no Brazilian press feed answered: {', '.join(down)}")

    seen, articles = set(), []
    for article in sorted((a for _, found, _ in answers for a in found),
                          key=lambda a: (-a["focus"], -a["mentions"], -a["published"].timestamp())):
        key = re.sub(r"\W", "", article["title"].lower())
        if key not in seen:
            seen.add(key)
            articles.append(article)

    sites = [name for name, _, why in answers if not why]
    header = (f"## Brazilian press: articles naming {code}, {start_date} to {end_date}\n"
              f"Source: the public WordPress feeds of {', '.join(sites)}: a sample of the financial press, "
              f"not all coverage. Times are publication times (UTC). An article whose text was edited "
              f"after {end_date} is left out, since its current text may carry later information. "
              "Ranked by how much each is about the company; an article covering several companies "
              f"is cut to its paragraphs about {code}. In Portuguese.\n")
    if down:
        header += f"Unavailable: {', '.join(down)}.\n"
    if not articles:
        return header + f"\nNone of these sites has an article naming {code} in the window.\n"

    limit = get_config().get("news_article_limit", 20)
    lines = [f"- {a['published']:%Y-%m-%d %H:%M} · {a['site']}: {a['title']} ({a['mentions']} mention(s))"
             for a in articles[:limit]]
    if len(articles) > limit:
        lines.append(f"_(showing the {limit} most relevant of {len(articles)} articles)_")
    return header + "\n" + "\n".join(lines) + "\n" + _texts(articles[:limit],
                                                            get_config().get("br_press_text_chars", 30_000))


def _texts(articles: list[dict], budget: int) -> str:
    """The text of the listed articles in rank order, up to ``budget`` characters."""
    if budget <= 0:
        return ""
    parts, left, skipped = [], budget, 0
    for a in articles:
        if left < _MIN_EXCERPT:
            skipped += 1
            continue
        title = (f"#### {a['published']:%Y-%m-%d %H:%M} · {a['site']}: {a['title']}"
                 + (" (the paragraphs about the company)" if a["excerpt"] else ""))
        text = a["text"]
        if len(text) > left:
            parts.append(f"{title}\n{text[:left]}\n_[... cut here: {len(text) - left} more characters]_")
            left = 0
        else:
            parts.append(f"{title}\n{text}")
            left -= len(text)
    tail = f"\n_(text of {skipped} more article(s) left out: length limit reached)_\n" if skipped else ""
    return "\n### Text of the articles\n\n" + "\n\n".join(parts) + "\n" + tail
