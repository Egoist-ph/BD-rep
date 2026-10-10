"""SAM.gov Get Opportunities Public API (v2) adapter.

One search request per target state (place of performance) per run, plus
pagination. Search results carry no description text, only a link to it, so
descriptions are fetched separately and only for leads that already show a
service-line hit (bounded by `description_budget`).

Needs a SAM.gov public API key in the env var named by `api_key_env`.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import requests

from bdrep.config import Config
from bdrep.models import Lead
from bdrep.normalize import clean_title, pick_state, strip_html
from bdrep.sources.base import FetchResult, Prefilter, SourceError, get, http_session

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"


def _sam_date(d: dt.date) -> str:
    return d.strftime("%m/%d/%Y")


def iso_date(value: str) -> str:
    """SAM returns both 'YYYY-MM-DD...' and 'MM/DD/YYYY' style dates."""
    value = (value or "").strip()
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    try:
        return dt.datetime.strptime(value[:10], "%m/%d/%Y").date().isoformat()
    except ValueError:
        return value


def _name(node: Any) -> str:
    if isinstance(node, dict):
        return node.get("name") or node.get("code") or ""
    return node or ""


def parse_notice(n: dict[str, Any], targets: list[str]) -> Lead:
    pop = n.get("placeOfPerformance") or {}
    state = ((pop.get("state") or {}).get("code") or "").upper() or None
    extra: dict[str, Any] = {
        "solicitation_number": n.get("solicitationNumber") or "",
        "psc": n.get("classificationCode") or "",
    }
    if not state:
        state = pick_state(n.get("title", ""), targets)
    if not state:
        office_state = ((n.get("officeAddress") or {}).get("state") or "").upper()
        if office_state:
            state = office_state
            extra["state_from"] = "contracting office"

    contacts = n.get("pointOfContact") or []
    primary = next((c for c in contacts if c.get("type") == "primary"), contacts[0] if contacts else {})
    contact = {k: v for k, v in {
        "name": primary.get("fullName") or "",
        "email": primary.get("email") or "",
        "phone": primary.get("phone") or "",
        "title": primary.get("title") or "",
    }.items() if v}

    description = n.get("description") or ""
    if description.startswith("http"):
        extra["description_url"] = description
        description = ""
    else:
        description = strip_html(description)

    award = n.get("award") or {}
    awardee = award.get("awardee") or {}
    if awardee.get("name"):
        extra["awardee"] = awardee["name"]
    if award.get("amount"):
        extra["award_amount"] = award["amount"]

    notice_id = n.get("noticeId") or ""
    return Lead(
        source="sam_gov",
        source_id=notice_id,
        title=clean_title(n.get("title") or ""),
        url=n.get("uiLink") or (f"https://sam.gov/opp/{notice_id}/view" if notice_id else ""),
        description=description,
        address=pop.get("streetAddress") or "",
        city=_name(pop.get("city")),
        state=state,
        zip=pop.get("zip") or "",
        organization=(n.get("fullParentPathName") or "").replace(".", " > "),
        notice_type=n.get("type") or n.get("baseType") or "",
        set_aside=n.get("typeOfSetAsideDescription") or "",
        naics=n.get("naicsCode") or "",
        posted_date=iso_date(n.get("postedDate") or ""),
        deadline=n.get("responseDeadLine") or "",
        contact=contact,
        extra=extra,
    )


def parse_search(payload: dict[str, Any], targets: list[str]) -> list[Lead]:
    """Active notices only: archived ones are past their response window."""
    return [
        parse_notice(n, targets)
        for n in payload.get("opportunitiesData") or []
        if n.get("noticeId") and str(n.get("active", "Yes")).lower() != "no"
    ]


class BudgetExhausted(SourceError):
    pass


class SamGovSource:
    name = "sam_gov"

    def __init__(self, cfg: Config, session: requests.Session | None = None, used_today: int = 0):
        self.cfg = cfg
        self.opts = cfg.source(self.name)
        self.session = session or http_session()
        self.targets = list(cfg.states)
        # A personal key with no SAM.gov role gets 10 requests/day; requests
        # already spent by earlier runs today count against it.
        self.budget = int(self.opts.get("daily_request_budget", 10)) - used_today

    def _api_key(self) -> str:
        env = self.opts.get("api_key_env", "SAM_API_KEY")
        key = os.environ.get(env, "").strip()
        if not key:
            raise SourceError(f"no API key: set the {env} environment variable")
        return key

    def _get(self, url: str, params: dict[str, Any], timeout: float, result: FetchResult) -> requests.Response:
        if result.requests >= self.budget:
            raise BudgetExhausted("daily request budget reached (sources.sam_gov.daily_request_budget)")
        result.requests += 1
        try:
            return get(self.session, url, params=params, timeout=timeout)
        except SourceError as exc:
            if str(exc).startswith(("HTTP 401", "HTTP 403")):
                raise SourceError(f"{exc} -- SAM.gov API keys expire every 90 days; generate a new key in your SAM.gov profile") from None
            raise

    def fetch(self, since: dt.date, until: dt.date, prefilter: Prefilter | None = None) -> FetchResult:
        """Per-state failures become errors; the run fails only if every state fails."""
        key = self._api_key()
        result = FetchResult()
        searched: list[str] = []
        timeout = float(self.opts.get("timeout_seconds", 60))
        for i, state in enumerate(self.targets):
            try:
                self._search_state(state, since, until, key, timeout, result)
                searched.append(state)
            except BudgetExhausted as exc:
                result.errors.append(f"{', '.join(self.targets[i:])} not searched: {exc}")
                break
            except SourceError as exc:
                result.errors.append(f"{state}: {exc}")
        if not searched:
            raise SourceError("; ".join(result.errors))
        self._fetch_descriptions(result, key, prefilter, timeout)
        return result

    def _search_state(self, state: str, since: dt.date, until: dt.date, key: str, timeout: float, result: FetchResult) -> None:
        page_size = int(self.opts.get("page_size", 1000))
        offset = 0
        while True:
            params = {
                "api_key": key,
                "postedFrom": _sam_date(since),
                "postedTo": _sam_date(until),
                "state": state,
                "limit": page_size,
                "offset": offset,
            }
            resp = self._get(SEARCH_URL, params, timeout, result)
            try:
                payload = resp.json()
            except ValueError:
                raise SourceError("non-JSON response") from None
            notices = payload.get("opportunitiesData") or []
            result.leads.extend(parse_search(payload, self.targets))
            offset += page_size
            if not notices or offset >= int(payload.get("totalRecords") or 0):
                return

    def _fetch_descriptions(self, result: FetchResult, key: str, prefilter: Prefilter | None, timeout: float) -> None:
        budget = min(int(self.opts.get("description_budget", 0)), self.budget - result.requests)
        if prefilter is None or int(self.opts.get("description_budget", 0)) <= 0:
            return
        wanted = [lead for lead in result.leads if lead.extra.get("description_url") and not lead.description and prefilter(lead)]
        for lead in wanted[:max(budget, 0)]:
            try:
                resp = self._get(lead.extra["description_url"], {"api_key": key}, timeout, result)
            except SourceError as exc:
                result.notes.append(f"description fetch stopped: {exc}")
                break  # likely quota; stop spending requests
            try:
                body = resp.json()
                text = body.get("description", "") if isinstance(body, dict) else str(body)
            except ValueError:
                text = resp.text
            lead.description = strip_html(text)
        skipped = len(wanted) - max(budget, 0)
        if skipped > 0:
            result.notes.append(f"{skipped} notice descriptions not fetched (request budget)")
