"""Illinois manufacturing segment: membership, scoring, exclusions, drafts, digest."""

import json

from bdrep.digest import write_digest
from bdrep.drafting import _lead_payload, run_drafts, template_draft
from bdrep.models import Lead
from bdrep.pipeline import score_all
from bdrep.scoring import score_lead

from conftest import TODAY


def news(title, state="IL", source_id=None, description=""):
    return Lead(source="news_rss", source_id=source_id or title[:12], title=title, state=state,
                notice_type="News", description=description)


OSHA = news("OSHA cites Rockford manufacturer for electrical hazards after inspection")
INCIDENT = news("Worker burned in arc flash at Joliet plant; OSHA investigating")
EXPANSION = news("Elgin plastics manufacturer announces plant expansion and new production line")
PPE_STORY = news("OSHA cites Peoria manufacturer for failing to provide PPE against electrical hazards")


def test_segment_membership_and_bonus(cfg):
    r = score_lead(OSHA, cfg)
    assert r.segment == "il_manufacturing"
    assert r.segment_bonus == 10
    assert "electrical hazards" in r.strong_terms      # strong only inside the segment
    assert r.verdict == "qualified"


def test_segment_terms_do_not_apply_outside_segment(cfg):
    storm = score_lead(news("Storm leaves electrical hazards across Chicago neighborhoods"), cfg)
    assert storm.segment is None and storm.service_line is None and storm.verdict == "low"
    ca_plant = score_lead(news("Fresno food processing plant cited for electrical hazards", state="CA"), cfg)
    assert ca_plant.segment is None and ca_plant.segment_bonus == 0


def test_expansion_is_a_near_miss_not_a_draft(cfg):
    r = score_lead(EXPANSION, cfg)
    assert r.segment == "il_manufacturing"
    assert r.strong_terms == [] and r.verdict == "near_miss"


def test_ppe_in_news_is_a_lead_but_ppe_procurement_is_excluded(cfg):
    assert score_lead(PPE_STORY, cfg).verdict == "qualified"
    notice = Lead(source="sam_gov", source_id="p", title="Electrical PPE and gloves for maintenance shop",
                  state="IL", notice_type="Solicitation")
    assert score_lead(notice, cfg).verdict == "excluded"


def test_incident_and_enforcement_wording_is_flagged(cfg):
    r = score_lead(INCIDENT, cfg)
    assert r.sensitive == ["burned"]
    assert r.verdict == "qualified" and r.segment == "il_manufacturing"
    assert score_lead(OSHA, cfg).sensitive == ["cites"]
    assert score_lead(EXPANSION, cfg).sensitive == []


def test_segment_template_pitches_a_study_review(cfg):
    d = template_draft(OSHA, score_lead(OSHA, cfg).to_dict(), cfg)
    assert "independent review of an existing study" in d.body
    assert d.subject == "Arc flash study review for your facility"
    assert d.recipient.startswith("Plant or maintenance manager")
    lowered = d.body.lower()
    assert "unqualified" not in lowered and "illegal" not in lowered


def test_incident_and_citation_drafts_never_quote_the_headline(cfg):
    for lead, word in ((INCIDENT, "burned"), (OSHA, "osha"), (PPE_STORY, "cites")):
        d = template_draft(lead, score_lead(lead, cfg).to_dict(), cfg)
        assert word not in d.body.lower() and word not in d.subject.lower()
        assert d.body.startswith("Hi,\n\nMany plants keep their arc flash study in-house.")
        assert "I work with" not in d.body


def test_plain_segment_lead_still_names_the_news(cfg):
    lead = news("Rockford manufacturer adds arc flash labels during switchgear upgrade")
    d = template_draft(lead, score_lead(lead, cfg).to_dict(), cfg)
    assert d.body.startswith("Hi,\n\nI came across the news about Rockford manufacturer")


def test_llm_payload_carries_segment_pitch_and_incident_terms(cfg):
    payload = _lead_payload(INCIDENT, score_lead(INCIDENT, cfg).to_dict(), cfg)
    screen = json.loads(payload.split("Keyword pre-screen (rule-based, may be wrong):\n", 1)[1])
    assert screen["segment"].startswith("Illinois manufacturer")
    assert "Never say or imply that their staff are unqualified" in screen["segment_pitch"]
    assert screen["incident_terms"] == ["burned"]
    from bdrep.drafting import system_prompt
    assert "Never imply existing clients" in system_prompt(cfg)


def test_digest_labels_segment_and_incident(store, cfg, tmp_path):
    cfg.data["drafting"]["enabled"] = True
    for lead in (OSHA, INCIDENT, EXPANSION):
        store.upsert(lead, TODAY)
    score_all(store, cfg)
    run_drafts(store, cfg, use_llm=False)
    text = write_digest(store, cfg, TODAY, tmp_path).read_text()
    assert text.count("- **Segment:** Illinois manufacturer") == 2   # near misses are table rows
    assert "segment +10" in text
    assert "- **Handle with care:** incident or enforcement wording (burned)" in text
    assert "Elgin plastics manufacturer" in text                      # near-miss table
