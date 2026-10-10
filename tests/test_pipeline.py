"""End to end: two daily runs over both sources, no network."""

import datetime as dt

from bdrep.cli import main
from bdrep.digest import write_digest
from bdrep.drafting import run_drafts
from bdrep.models import Lead
from bdrep.pipeline import fetch_window, ingest, score_all
from bdrep.sources import FetchResult, SourceError
from bdrep.sources.news_rss import parse_feed
from bdrep.sources.sam_gov import parse_search

from conftest import TODAY


class StaticSource:
    def __init__(self, name, leads, error=None):
        self.name = name
        self.leads = leads
        self.error = error
        self.windows = []

    def fetch(self, since, until, prefilter=None):
        self.windows.append((since, until))
        if self.error:
            raise SourceError(self.error)
        return FetchResult(leads=list(self.leads), requests=1)


def sources(cfg, sam_payload, news_xml):
    return {
        "sam_gov": StaticSource("sam_gov", parse_search(sam_payload, list(cfg.states))),
        "news_rss": StaticSource("news_rss", parse_feed(news_xml, list(cfg.states))),
    }


def test_full_day(store, cfg, sam_payload, news_xml, tmp_path):
    reports = ingest(store, cfg, TODAY, sources=sources(cfg, sam_payload, news_xml))
    assert [r.status for r in reports] == ["ok", "ok"]
    news = reports[1].counts
    assert news["merged"] == 1     # "Building 22" news item merged into the SAM notice

    score_all(store, cfg)
    run_drafts(store, cfg, use_llm=False)
    path = write_digest(store, cfg, TODAY, tmp_path / "digests")
    text = path.read_text()

    assert path.name == "2026-10-10.md"
    assert "nothing in this digest has been sent" in text
    assert "**5 to review**" in text
    assert "Held — license pending" in text and "Tampa school district" in text
    assert "HOLD: Florida PE license pending" in text
    assert "Professional Design Firm" in text            # IL note on the IL leads
    assert "SAM.gov + News" in text                      # merged lead shows both sources
    assert "Near misses" in text and "Replace Main Switchgear" in text
    assert "Janitorial" not in text and "PPE" not in text and "Market" not in text
    assert "## Source health" in text and "| SAM.gov | ok |" in text
    assert "```text\nSubject:" in text
    assert "Surfaced (qualified + held): **7**" in text
    assert "($1,250,000)" in text


def test_second_day_shows_only_new_leads(store, cfg, sam_payload, news_xml, tmp_path):
    srcs = sources(cfg, sam_payload, news_xml)
    ingest(store, cfg, TODAY, sources=srcs)
    score_all(store, cfg)
    run_drafts(store, cfg, use_llm=False)
    write_digest(store, cfg, TODAY, tmp_path)

    tomorrow = TODAY + dt.timedelta(days=1)
    srcs["news_rss"].leads.append(Lead(
        source="news_rss", source_id="new", title="Joliet plant seeks arc flash study bids", state="IL", notice_type="News",
    ))
    reports = ingest(store, cfg, tomorrow, sources=srcs)
    assert reports[0].counts["seen"] == 8 and reports[0].counts["new"] == 0
    assert srcs["sam_gov"].windows[-1][0] == TODAY - dt.timedelta(days=1)
    score_all(store, cfg)
    run_drafts(store, cfg, use_llm=False)
    text = write_digest(store, cfg, tomorrow, tmp_path).read_text()
    assert "Joliet plant" in text
    assert "Building 22" not in text
    assert "**1 to review**" in text


def test_failed_source_is_visible_not_fatal(store, cfg, sam_payload, news_xml, tmp_path):
    srcs = sources(cfg, sam_payload, news_xml)
    srcs["sam_gov"].error = "no API key: set the SAM_API_KEY environment variable"
    reports = ingest(store, cfg, TODAY, sources=srcs)
    assert [r.status for r in reports] == ["error", "ok"]
    score_all(store, cfg)
    text = write_digest(store, cfg, TODAY, tmp_path).read_text()
    assert "| SAM.gov | ERROR |" in text and "SAM_API_KEY" in text
    assert store.get_meta("sam_gov:last_success") is None


def test_fetch_window(store, cfg):
    opts = {"lookback_days": 3, "max_window_days": 30}
    assert fetch_window(store, "x", opts, TODAY) == TODAY - dt.timedelta(days=3)
    store.set_meta("x:last_success", "2026-10-05")
    assert fetch_window(store, "x", opts, TODAY) == dt.date(2026, 10, 4)
    store.set_meta("x:last_success", "2026-01-01")
    assert fetch_window(store, "x", opts, TODAY) == TODAY - dt.timedelta(days=30)


def test_cli_mark_stats_show(store, cfg, sam_payload, news_xml, tmp_path, capsys):
    db = store.path
    ingest(store, cfg, TODAY, sources=sources(cfg, sam_payload, news_xml))
    score_all(store, cfg)
    run_drafts(store, cfg, use_llm=False)
    write_digest(store, cfg, TODAY, tmp_path)
    qualified = [r["id"] for r in store.iter_leads() if r["verdict"] == "qualified"]
    store.close()

    assert main(["--db", str(db), "--date", "2026-10-10", "mark", str(qualified[0]), "sent"]) == 0
    assert main(["--db", str(db), "--date", "2026-10-10", "mark", str(qualified[1]), "not-relevant"]) == 0
    assert main(["--db", str(db), "--date", "2026-10-10", "mark", "9999", "sent"]) == 1
    capsys.readouterr()
    assert main(["--db", str(db), "--date", "2026-10-10", "stats"]) == 0
    out = capsys.readouterr().out
    assert '"precision": 0.5' in out and '"sent": 1' in out
    assert main(["--db", str(db), "show", str(qualified[0])]) == 0
    assert "Subject:" in capsys.readouterr().out


def test_descriptions_are_not_refetched_on_overlapping_days(store, cfg, sam_payload, monkeypatch):
    from bdrep.sources.sam_gov import SamGovSource

    from conftest import FakeResponse, FakeSession

    monkeypatch.setenv("SAM_API_KEY", "k")
    cfg.data["states"] = {"CA": cfg.states["CA"]}
    desc = FakeResponse(200, {"description": "Includes an arc flash study."})

    def day(date, responses):
        session = FakeSession(responses)
        src = SamGovSource(cfg, session=session, used_today=store.requests_on(date, "sam_gov"))
        ingest(store, cfg, date, sources={"sam_gov": src}, only=["sam_gov"])
        return session

    first = day(TODAY, [FakeResponse(200, sam_payload), desc, desc, desc, desc, desc])
    described = len([c for c in first.calls if "noticedesc" in c["url"]])
    assert described >= 1
    second = day(TODAY + dt.timedelta(days=1), [FakeResponse(200, sam_payload)])
    assert [c for c in second.calls if "noticedesc" in c["url"]] == []
