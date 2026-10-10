import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from bdrep.sources.base import SourceError, redact
from bdrep.sources.news_rss import NewsRssSource, google_news_url, parse_feed
from bdrep.sources.sam_gov import SamGovSource, iso_date, parse_search

from conftest import FakeResponse, FakeSession

SINCE, UNTIL = dt.date(2026, 10, 7), dt.date(2026, 10, 10)


# -- SAM.gov ----------------------------------------------------------------------

def test_parse_search_maps_fields(cfg, sam_payload):
    leads = {l.source_id: l for l in parse_search(sam_payload, list(cfg.states))}
    l = leads["test-0001"]
    assert l.title == "Arc Flash Hazard Analysis and Short Circuit Study, Building 22"
    assert (l.city, l.state, l.zip) == ("San Diego", "CA", "92101")
    assert l.notice_type == "Combined Synopsis/Solicitation"
    assert l.naics == "541330"
    assert l.deadline.startswith("2026-10-24")
    assert l.contact["email"] == "pat.example@example.com"
    assert l.extra["description_url"].startswith("https://api.sam.gov/")
    assert l.organization == "EXAMPLE AGENCY > EXAMPLE REGIONAL OFFICE"
    assert leads["test-0003"].posted_date == "2026-10-07"   # MM/DD/YYYY normalized
    assert leads["test-0007"].extra["awardee"] == "Example Electric Inc."


def test_parse_search_drops_inactive_and_handles_null_place(cfg, sam_payload):
    leads = {l.source_id: l for l in parse_search(sam_payload, list(cfg.states))}
    assert "test-0008" not in leads
    assert leads["test-0009"].state == "TX"
    assert leads["test-0009"].extra["state_from"] == "contracting office"


def test_iso_date():
    assert iso_date("2026-10-08 14:23:11-04") == "2026-10-08"
    assert iso_date("10/07/2026") == "2026-10-07"


def test_sam_fetch_one_request_per_state(cfg, sam_payload, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "secret-key")
    session = FakeSession([FakeResponse(200, sam_payload)] + [FakeResponse(200, {"opportunitiesData": [], "totalRecords": 0})] * 2)
    result = SamGovSource(cfg, session=session).fetch(SINCE, UNTIL)
    assert [c["params"]["state"] for c in session.calls] == ["CA", "IL", "FL"]
    first = session.calls[0]["params"]
    assert first["postedFrom"] == "10/07/2026" and first["postedTo"] == "10/10/2026"
    assert first["limit"] == 1000
    assert result.requests == 3
    assert len(result.leads) == 8


def test_sam_paginates(cfg, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    cfg.data["sources"]["sam_gov"]["page_size"] = 2
    cfg.data["states"] = {"CA": cfg.states["CA"]}
    page = lambda ids: {"totalRecords": 3, "opportunitiesData": [{"noticeId": i, "title": i} for i in ids]}
    session = FakeSession([FakeResponse(200, page(["a", "b"])), FakeResponse(200, page(["c"]))])
    result = SamGovSource(cfg, session=session).fetch(SINCE, UNTIL)
    assert [c["params"]["offset"] for c in session.calls] == [0, 2]
    assert [l.source_id for l in result.leads] == ["a", "b", "c"]


def test_sam_missing_key_is_a_clear_error(cfg, monkeypatch):
    monkeypatch.delenv("SAM_API_KEY", raising=False)
    with pytest.raises(SourceError, match="SAM_API_KEY"):
        SamGovSource(cfg, session=FakeSession([])).fetch(SINCE, UNTIL)


def test_sam_errors_never_leak_api_key(cfg, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "super-secret")
    session = FakeSession([requests.ConnectionError("failed for url ...?api_key=super-secret&state=CA")] * 3)
    with pytest.raises(SourceError) as exc:
        SamGovSource(cfg, session=session).fetch(SINCE, UNTIL)
    assert "super-secret" not in str(exc.value)


def test_sam_expired_key_hint(cfg, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    session = FakeSession([FakeResponse(403, text="API_KEY_INVALID")] * 3)
    with pytest.raises(SourceError, match="expire every 90 days"):
        SamGovSource(cfg, session=session).fetch(SINCE, UNTIL)


def test_sam_one_state_failing_is_partial(cfg, sam_payload, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    session = FakeSession([FakeResponse(200, sam_payload), FakeResponse(500, text="boom"), FakeResponse(200, {"opportunitiesData": []})])
    result = SamGovSource(cfg, session=session).fetch(SINCE, UNTIL)
    assert result.leads and any(e.startswith("IL:") for e in result.errors)


def test_sam_request_budget_counts_earlier_runs(cfg, sam_payload, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    session = FakeSession([FakeResponse(200, sam_payload)] * 3)
    result = SamGovSource(cfg, session=session, used_today=8).fetch(SINCE, UNTIL)
    assert result.requests == 2
    assert "FL not searched" in result.errors[0]


def test_sam_descriptions_only_for_prefiltered_leads_within_budget(cfg, sam_payload, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    cfg.data["sources"]["sam_gov"]["description_budget"] = 2
    desc = FakeResponse(200, {"description": "<p>Includes an <b>IEEE 1584</b> arc flash study.</p>"})
    session = FakeSession([FakeResponse(200, sam_payload), FakeResponse(200, {"opportunitiesData": []}),
                           FakeResponse(200, {"opportunitiesData": []}), desc, desc])
    result = SamGovSource(cfg, session=session).fetch(SINCE, UNTIL, prefilter=lambda l: "Arc Flash" in l.title)
    described = [l for l in result.leads if l.description]
    assert len(described) == 2
    assert described[0].description == "Includes an IEEE 1584 arc flash study."
    assert session.calls[3]["params"] == {"api_key": "k"}
    assert any("not fetched" in n for n in result.notes)


def test_redact():
    assert redact("https://x/y?api_key=abc123&state=CA") == "https://x/y?api_key=REDACTED&state=CA"


# -- News RSS ---------------------------------------------------------------------

def test_google_news_url_encodes_window():
    q = parse_qs(urlparse(google_news_url('"arc flash" California', 2)).query)["q"][0]
    assert q == '"arc flash" California when:2d'


def test_parse_feed(cfg, news_xml):
    leads = {l.url.rsplit("/", 1)[-1]: l for l in parse_feed(news_xml, list(cfg.states))}
    fire = leads["test-fire"]
    assert fire.title == "Chicago warehouse fire blamed on electrical failure; insurer hires investigators"
    assert fire.organization == "Example Tribune"
    assert fire.state == "IL"
    assert fire.posted_date == "2026-10-09"
    assert not fire.description.endswith("Example Tribune")
    assert leads["test-texas"].state == "TX"
    assert leads["test-market"].state is None


def test_parse_atom(cfg):
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Example Journal</title>
      <entry><id>tag:1</id><title>Orlando plant switchgear upgrade</title>
      <link href="https://example.com/a"/><updated>2026-10-09T10:00:00Z</updated>
      <summary>New substation.</summary></entry></feed>"""
    [lead] = parse_feed(atom, list(cfg.states))
    assert (lead.state, lead.url, lead.posted_date) == ("FL", "https://example.com/a", "2026-10-09")


def test_parse_feed_rejects_garbage(cfg):
    with pytest.raises(SourceError):
        parse_feed("<html>not a feed", list(cfg.states))


def test_news_fetch_dedupes_and_drops_old_items(cfg, news_xml):
    cfg.data["sources"]["news_rss"]["google_news_queries"] = ["q1", "q2"]
    session = FakeSession([FakeResponse(200, text=news_xml), FakeResponse(200, text=news_xml)])
    result = NewsRssSource(cfg, session=session).fetch(SINCE, UNTIL)
    assert result.requests == 2
    titles = [l.title for l in result.leads]
    assert len(titles) == len(set(titles)) == 7      # 8 items, one too old
    assert "when%3A4d" in session.calls[0]["url"]


def test_news_all_feeds_failing_raises(cfg):
    cfg.data["sources"]["news_rss"]["google_news_queries"] = ["q1"]
    with pytest.raises(SourceError):
        NewsRssSource(cfg, session=FakeSession([FakeResponse(503, text="down")])).fetch(SINCE, UNTIL)


def test_urllib3_url_logging_is_silenced():
    import logging

    import bdrep.sources.base  # noqa: F401

    assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.ERROR


def test_proxy_403_is_not_reported_as_expired_key(cfg, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k")
    err = requests.exceptions.ProxyError("Max retries (Caused by ProxyError('Tunnel connection failed: 403 Forbidden'))")
    with pytest.raises(SourceError) as exc:
        SamGovSource(cfg, session=FakeSession([err] * 3)).fetch(SINCE, UNTIL)
    assert "expire" not in str(exc.value)
    assert "ProxyError reaching api.sam.gov" in str(exc.value)
