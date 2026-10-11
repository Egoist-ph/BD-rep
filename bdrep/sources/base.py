"""Source adapter contract and shared HTTP plumbing."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bdrep import __version__
from bdrep.models import Lead

USER_AGENT = f"bdrep/{__version__} (+https://github.com/Egoist-ph/BD-rep)"

# urllib3 logs full request URLs (query string included, so the SAM.gov API
# key) on retries and at debug level. Keep it quiet; errors surface through
# SourceError with secrets redacted.
logging.getLogger("urllib3").setLevel(logging.ERROR)

_SECRET_PARAM = re.compile(r"(api_key|apikey|key|token)=[^&\s'\"]+", re.IGNORECASE)


class SourceError(Exception):
    """A source failed; the message is safe to print (secrets redacted)."""


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1=REDACTED", text)


@dataclass
class FetchResult:
    leads: list[Lead] = field(default_factory=list)
    requests: int = 0
    errors: list[str] = field(default_factory=list)  # part of the fetch failed
    notes: list[str] = field(default_factory=list)   # informational


# Called with a lead; True means it is worth spending an extra request on.
Prefilter = Callable[[Lead], bool]


class Source(Protocol):
    name: str

    def fetch(self, since: dt.date, until: dt.date, prefilter: Prefilter | None = None) -> FetchResult: ...


def http_session() -> requests.Session:
    """GET session that retries server errors only. 429s are not retried:
    with a daily request quota, retrying a rate limit just burns more of it."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    retry = Retry(total=2, backoff_factor=2, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _short(exc: Exception, host: str) -> str:
    """One readable line: exception type, host, and the root cause."""
    text = str(exc)
    if "Caused by " in text:
        text = text.rsplit("Caused by ", 1)[1].rstrip(")")
    return redact(f"{type(exc).__name__} reaching {host}: {text}")[:240]


def get(session: requests.Session, url: str, *, params: dict | None = None, timeout: float = 30) -> requests.Response:
    """GET that raises SourceError with secrets stripped from the message."""
    host = url.split("/")[2] if "://" in url else url
    try:
        resp = session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SourceError(_short(exc, host)) from None
    if resp.status_code == 429:
        raise SourceError(f"rate limited (HTTP 429) by {resp.url.split('?')[0]}")
    if resp.status_code >= 400:
        body = redact(resp.text[:300].replace("\n", " "))
        raise SourceError(f"HTTP {resp.status_code} from {resp.url.split('?')[0]}: {body}")
    return resp
