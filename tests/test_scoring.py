from bdrep.models import Lead
from bdrep.scoring import has_service_signal, score_lead
from bdrep.sources.news_rss import parse_feed
from bdrep.sources.sam_gov import parse_search


def by_id(leads):
    return {l.source_id: l for l in leads}


def test_sam_fixture_verdicts(cfg, sam_payload):
    leads = by_id(parse_search(sam_payload, list(cfg.states)))
    verdict = {k: score_lead(v, cfg).verdict for k, v in leads.items()}
    assert verdict["test-0001"] == "qualified"   # arc flash + short circuit study, CA
    assert verdict["test-0002"] == "excluded"    # PPE purchase
    assert verdict["test-0003"] == "held"        # strong match but FL license pending
    assert verdict["test-0004"] == "near_miss"   # switchgear only: weak match
    assert verdict["test-0005"] == "low"         # janitorial
    assert verdict["test-0006"] == "qualified"   # forensics sources sought, IL
    assert verdict["test-0007"] == "qualified"   # award notice with coordination study
    assert verdict["test-0009"] == "excluded"    # out of area (TX)


def test_news_fixture_verdicts(cfg, news_xml):
    leads = {l.url.rsplit("/", 1)[-1]: l for l in parse_feed(news_xml, list(cfg.states))}
    verdict = {k: score_lead(v, cfg) for k, v in leads.items()}
    assert verdict["test-market"].verdict == "excluded"
    assert verdict["test-fire"].verdict == "qualified"
    assert verdict["test-fire"].service_line == "forensics"
    assert verdict["test-hospital"].verdict == "near_miss"
    assert verdict["test-texas"].verdict == "excluded"
    assert verdict["test-tampa"].verdict == "held"
    assert verdict["test-179d"].verdict == "qualified"
    assert verdict["test-179d"].service_line == "179d"
    assert verdict["test-dup"].verdict == "qualified"


def test_market_research_press_release_excluded(cfg):
    l = Lead(source="news_rss", source_id="m", title="Arc Flash Protection Market Size to Grow at 6% CAGR", state=None)
    r = score_lead(l, cfg)
    assert r.verdict == "excluded" and r.total == 0


def test_training_excluded(cfg):
    l = Lead(source="sam_gov", source_id="t", title="NFPA 70E Training for Maintenance Staff", state="CA", notice_type="Solicitation")
    assert score_lead(l, cfg).verdict == "excluded"


def test_design_scope_is_penalized(cfg):
    base = dict(source="sam_gov", source_id="d", state="CA", notice_type="Solicitation")
    plain = score_lead(Lead(title="Arc Flash Study, Building 9", **base), cfg)
    design = score_lead(Lead(title="Design-Build Electrical Design Services incl. Arc Flash Study, Building 9", **base), cfg)
    assert design.penalty == 15
    assert design.total < plain.total


def test_179d_future_construction_is_penalized(cfg):
    l = Lead(source="news_rss", source_id="f", title="Proposed office building plans 179D lighting retrofit in San Jose", state="CA")
    r = score_lead(l, cfg)
    assert r.service_line == "179d"
    assert any("179D cutoff" in reason for reason in r.reasons)
    assert r.verdict != "qualified"


def test_weak_only_never_qualifies(cfg):
    l = Lead(
        source="sam_gov", source_id="w", state="CA", notice_type="Solicitation", naics="238210",
        title="Electrical upgrade: replace switchgear and substation at hospital building, $4 million",
    )
    r = score_lead(l, cfg)
    assert r.total >= cfg.scoring["threshold"]
    assert r.verdict == "near_miss"
    assert "weak match only" in " ".join(r.reasons)


def test_unknown_state_scores_lower_than_licensed(cfg):
    known = score_lead(Lead(source="news_rss", source_id="1", title="Arc flash study RFP for hospital", state="CA"), cfg)
    unknown = score_lead(Lead(source="news_rss", source_id="2", title="Arc flash study RFP for hospital", state=None), cfg)
    assert known.total - unknown.total == 15
    assert "state not detected" in unknown.reasons


def test_has_service_signal(cfg):
    assert has_service_signal(Lead(source="s", source_id="1", title="Replace switchgear", state="CA"), cfg)
    assert not has_service_signal(Lead(source="s", source_id="2", title="Janitorial services", state="CA"), cfg)
    assert not has_service_signal(Lead(source="s", source_id="3", title="Arc flash study", state="TX"), cfg)
