"""News adapter: Google News RSS search queries plus any extra RSS/Atom feeds.

News items are the noisy source: they rarely carry an address or a contact,
and the state has to be inferred from the text. Scoring is what separates a
real commercial project from a market-research press release.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import quote_plus

import requests

from bdrep.config import Config
from bdrep.models import Lead
from bdrep.normalize import pick_state, strip_html
from bdrep.sources.base import FetchResult, Prefilter, SourceError, get, http_session

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
ATOM = "{http://www.w3.org/2005/Atom}"


def google_news_url(query: str, days: int) -> str:
    return GOOGLE_NEWS.format(q=quote_plus(f"{query} when:{days}d"))


def _parse_date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _strip_publisher(title: str, publisher: str) -> str:
    """Google News titles end in ' - Publisher'."""
    suffix = f" - {publisher}" if publisher else ""
    if suffix and title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title.strip()


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml_text: str, targets: Iterable[str]) -> list[Lead]:
    """Parse RSS 2.0 or Atom into leads. Undated items are kept."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"unparseable feed: {exc}") from None
    targets = list(targets)
    leads: list[Lead] = []

    for item in root.iter("item"):
        source_el = item.find("source")
        publisher = _text(source_el)
        title = _strip_publisher(strip_html(_text(item.find("title"))), publisher)
        link = _text(item.find("link"))
        desc = strip_html(_text(item.find("description")))
        if desc.endswith(publisher) and publisher:
            desc = desc[: -len(publisher)].strip()
        leads.append(_lead(title, link, _text(item.find("guid")) or link, desc,
                           publisher, _parse_date(_text(item.find("pubDate"))), targets))

    for entry in root.iter(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        title = strip_html(_text(entry.find(f"{ATOM}title")))
        desc = strip_html(_text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content")))
        publisher = _text(root.find(f"{ATOM}title"))
        date = _parse_date(_text(entry.find(f"{ATOM}updated")) or _text(entry.find(f"{ATOM}published")))
        leads.append(_lead(title, link, _text(entry.find(f"{ATOM}id")) or link, desc, publisher, date, targets))

    return [lead for lead in leads if lead.title]


def _lead(title: str, link: str, guid: str, desc: str, publisher: str, date: dt.date | None, targets: list[str]) -> Lead:
    source_id = hashlib.sha1((guid or title).encode()).hexdigest()[:16]
    return Lead(
        source="news_rss",
        source_id=source_id,
        title=title,
        url=link,
        description=desc if desc != title else "",
        state=pick_state(f"{title}. {desc}", targets),
        organization=publisher,
        notice_type="News",
        posted_date=date.isoformat() if date else "",
    )


class NewsRssSource:
    name = "news_rss"

    def __init__(self, cfg: Config, session: requests.Session | None = None, used_today: int = 0):
        self.cfg = cfg
        self.opts = cfg.source(self.name)
        self.session = session or http_session()
        self.targets = list(cfg.states)

    def urls(self, since: dt.date, until: dt.date) -> list[str]:
        days = max(1, (until - since).days + 1)
        urls = [google_news_url(q, days) for q in self.opts.get("google_news_queries") or []]
        return urls + list(self.opts.get("feeds") or [])

    def fetch(self, since: dt.date, until: dt.date, prefilter: Prefilter | None = None) -> FetchResult:
        """Per-feed failures become errors; the run fails only if every feed fails."""
        result = FetchResult()
        urls = self.urls(since, until)
        timeout = float(self.opts.get("timeout_seconds", 30))
        failures = 0
        seen: set[str] = set()
        for url in urls:
            result.requests += 1
            try:
                resp = get(self.session, url, timeout=timeout)
                items = parse_feed(resp.text, self.targets)
            except SourceError as exc:
                failures += 1
                result.errors.append(f"{url.split('?')[0]}: {exc}")
                continue
            for lead in items:
                if lead.source_id in seen:
                    continue
                if lead.posted_date and lead.posted_date < since.isoformat():
                    continue
                seen.add(lead.source_id)
                result.leads.append(lead)
        if urls and failures == len(urls):
            distinct = sorted(set(e.split(": ", 1)[1] for e in result.errors))
            raise SourceError(f"all {failures} feeds failed: " + "; ".join(distinct))
        return result
