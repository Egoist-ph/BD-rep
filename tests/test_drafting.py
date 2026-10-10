import json
from types import SimpleNamespace

from bdrep.drafting import (
    DRAFT_SCHEMA,
    draft_lead,
    run_drafts,
    system_prompt,
    template_draft,
)
from bdrep.pipeline import score_all
from bdrep.scoring import score_lead
from bdrep.sources.sam_gov import parse_search

from conftest import TODAY, FakeAnthropic

GOOD = {
    "is_real_opportunity": True,
    "reject_reason": "",
    "why_it_matters": "Direct arc flash study solicitation in CA, small business set-aside.",
    "recipient": "Contracting officer on the notice",
    "subject": "Arc flash study, Building 22",
    "body": "Hi Pat,\n\nShort draft.\n\nRoben Mata, PE\nMata Engineering",
}


def sam_leads(cfg, payload):
    return {l.source_id: l for l in parse_search(payload, list(cfg.states))}


def test_template_draft_basics(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0001"]
    score = score_lead(lead, cfg).to_dict()
    d = template_draft(lead, score, cfg)
    assert d.keep and d.method == "template"
    assert "Building 22" in d.body and "Roben Mata, PE" in d.body
    assert "CA/IL" in d.body and "FL" not in d.body
    assert d.body.startswith("Hi Pat,")
    assert "design" not in d.body.lower()
    assert "responses due 2026-10-24" in d.why


def test_template_targets_contractor_on_award_and_construction(cfg, sam_payload):
    leads = sam_leads(cfg, sam_payload)
    award = template_draft(leads["test-0007"], score_lead(leads["test-0007"], cfg).to_dict(), cfg)
    assert award.recipient.startswith("Example Electric Inc.")
    assert "as your sub" in award.body
    assert award.body.startswith("Hi,")


def test_template_sources_sought_asks_for_capability_statement(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0006"]
    d = template_draft(lead, score_lead(lead, cfg).to_dict(), cfg)
    assert "capability statement" in d.body


def test_llm_request_shape(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0001"]
    client = FakeAnthropic([GOOD])
    d = draft_lead(lead, score_lead(lead, cfg).to_dict(), cfg, client)
    assert d.method == "llm" and d.why == GOOD["why_it_matters"]
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": DRAFT_SCHEMA}
    assert call["output_config"]["effort"] == "medium"
    assert "thinking" not in call and "temperature" not in call
    user = call["messages"][0]["content"]
    assert user.startswith("<lead>") and "Building 22" in user


def test_system_prompt_guards(cfg):
    prompt = system_prompt(cfg)
    assert "California, Illinois only" in prompt
    assert "Florida" not in prompt
    assert "Never invent a person" in prompt
    assert "ground-up electrical design" in prompt
    assert "2026-06-30" in prompt          # 179D cutoff reaches the model
    assert "untrusted" in prompt
    assert "outside licensed states" in prompt


def test_llm_failure_falls_back_to_template(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0001"]
    client = FakeAnthropic([RuntimeError("overloaded")])
    d = draft_lead(lead, score_lead(lead, cfg).to_dict(), cfg, client)
    assert d.method == "template" and d.keep
    assert "LLM draft failed" in d.notes[0]


def test_llm_refusal_falls_back_to_template(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0001"]
    refusal = SimpleNamespace(stop_reason="refusal", model="claude-opus-5-5", content=[])
    d = draft_lead(lead, score_lead(lead, cfg).to_dict(), cfg, FakeAnthropic([refusal]))
    assert d.method == "template"


def _load(store, cfg, payload):
    for lead in parse_search(payload, list(cfg.states)):
        store.upsert(lead, TODAY)
    score_all(store, cfg)


def test_run_drafts_with_reviewer_rejection(store, cfg, sam_payload):
    _load(store, cfg, sam_payload)
    reject = {**GOOD, "is_real_opportunity": False, "reject_reason": "Already awarded with study in scope.",
              "why_it_matters": "", "recipient": "", "subject": "", "body": ""}
    # qualified, ordered by score desc: test-0007 (85), test-0001 (84), test-0006 (83)
    client = FakeAnthropic([reject, GOOD, GOOD])
    counts = run_drafts(store, cfg, client=client)
    assert counts["drafted"] == 2 and counts["rejected"] == 1 and counts["held"] == 1
    rows = {json.loads(r["sources_json"])[0]["source_id"]: r for r in store.iter_leads()}
    assert rows["test-0007"]["verdict"] == "llm_rejected"
    assert rows["test-0001"]["draft"].startswith("Subject: Arc flash study")
    held = rows["test-0003"]
    assert held["verdict"] == "held" and held["draft"] is None and held["why"]


def test_run_drafts_without_llm_uses_templates(store, cfg, sam_payload):
    _load(store, cfg, sam_payload)
    counts = run_drafts(store, cfg, use_llm=False)
    assert counts["drafted"] == 3 and counts["template"] == 3
    assert run_drafts(store, cfg, use_llm=False)["drafted"] == 0   # idempotent


def test_179d_news_draft_is_not_a_subcontract_pitch(cfg, news_xml):
    from bdrep.sources.news_rss import parse_feed

    lead = next(l for l in parse_feed(news_xml, list(cfg.states)) if l.url.endswith("test-179d"))
    d = template_draft(lead, score_lead(lead, cfg).to_dict(), cfg)
    assert "as your sub" not in d.body
    assert "June 30, 2026" in d.body
    assert d.recipient.startswith("Building owner")
    assert d.subject.startswith("179D certification: ") and not d.subject.endswith("deductio")
    assert len(d.subject) <= 100


def test_direct_vs_implied_service_sentence(cfg, sam_payload):
    leads = sam_leads(cfg, sam_payload)
    direct = template_draft(leads["test-0001"], score_lead(leads["test-0001"], cfg).to_dict(), cfg)
    assert "core of my practice" in direct.body and "often need" not in direct.body


def test_empty_llm_body_falls_back_to_template(cfg, sam_payload):
    lead = sam_leads(cfg, sam_payload)["test-0001"]
    d = draft_lead(lead, score_lead(lead, cfg).to_dict(), cfg, FakeAnthropic([{**GOOD, "body": "  "}]))
    assert d.method == "template" and "empty draft body" in d.notes[0]
