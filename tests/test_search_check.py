from urllib.parse import parse_qs, urlparse

from bdrep.cli import main
from bdrep.search_check import format_report, run_search

from conftest import FakeResponse, FakeSession


def test_run_search_scores_every_result(cfg, news_xml):
    cfg.data["sources"]["news_rss"]["google_news_queries"] = ["q1"]
    [report] = run_search(cfg, days=7, session=FakeSession([FakeResponse(200, text=news_xml)]))
    assert report.error == ""
    assert len(report.results) == 8                       # nothing dropped: the check shows everything
    totals = [score.total for _, score in report.results]
    assert totals == sorted(totals, reverse=True)
    counts = report.counts()
    assert counts["excluded"] == 2 and counts["held"] == 1 and counts["qualified"] == 3


def test_adhoc_query_and_window(cfg, news_xml):
    session = FakeSession([FakeResponse(200, text=news_xml)])
    run_search(cfg, queries=['"arc flash" Chicago'], days=30, session=session)
    q = parse_qs(urlparse(session.calls[0]["url"]).query)["q"][0]
    assert q == '"arc flash" Chicago when:30d'


def test_format_report(cfg, news_xml):
    cfg.data["sources"]["news_rss"]["google_news_queries"] = ["q1", "q2"]
    session = FakeSession([FakeResponse(200, text=news_xml), FakeResponse(503, text="down")])
    text = format_report(run_search(cfg, session=session), limit=3)
    assert "Query 1: q1" in text
    assert "8 results: 3 qualified, 1 held, 1 near miss, 1 low, 2 excluded" in text
    assert "... 5 more" in text
    assert "Query 2: q2" in text and "ERROR: HTTP 503" in text
    assert "Summary" in text and "8 unique results across queries" in text


def test_format_report_kept_only_hides_noise(cfg, news_xml):
    cfg.data["sources"]["news_rss"]["google_news_queries"] = ["q1"]
    text = format_report(run_search(cfg, session=FakeSession([FakeResponse(200, text=news_xml)])), kept_only=True, limit=50)
    assert "Market to Reach" not in text and "excluded: cagr" not in text
    assert "Chicago warehouse fire" in text


def test_cli_search_reports_network_failure(capsys, monkeypatch):
    import bdrep.search_check as sc

    monkeypatch.setattr(sc, "http_session", lambda: FakeSession([FakeResponse(403, text="blocked")] * 10))
    assert main(["search", "--query", "x"]) == 1
    assert "ERROR: HTTP 403" in capsys.readouterr().out
