import datetime as dt
import json

from bdrep.models import Lead
from bdrep.store import titles_match

from conftest import TODAY


def lead(**kw):
    base = dict(source="sam_gov", source_id="1", title="Arc Flash Study, Building 22", state="CA", city="San Diego")
    base.update(kw)
    return Lead(**base)


def test_new_then_seen_by_source_id(store):
    assert store.upsert(lead(), TODAY)[0] == "new"
    outcome, lead_id = store.upsert(lead(deadline="2026-10-30"), TODAY + dt.timedelta(days=1))
    assert outcome == "seen"
    row = store.get(lead_id)
    assert row["deadline"] == "2026-10-30"
    assert row["last_seen"] == "2026-10-11"


def test_cross_source_exact_key_merges(store):
    _, first = store.upsert(lead(), TODAY)
    outcome, second = store.upsert(
        lead(source="news_rss", source_id="abc", title="The Arc Flash Study - Building 22", url="https://news"),
        TODAY,
    )
    assert (outcome, second) == ("merged", first)
    sources = json.loads(store.get(first)["sources_json"])
    assert [s["source"] for s in sources] == ["sam_gov", "news_rss"]


def test_fuzzy_merge_when_address_missing(store):
    _, first = store.upsert(lead(title="Arc Flash Hazard Analysis and Short Circuit Study, Building 22"), TODAY)
    outcome, second = store.upsert(
        lead(source="news_rss", source_id="x", title="Arc Flash Hazard Analysis & Short-Circuit Study Bldg 22", city="", state=None),
        TODAY,
    )
    assert outcome == "merged" and second == first


def test_different_building_numbers_never_merge(store):
    store.upsert(lead(title="Arc Flash Study, Building 100"), TODAY)
    outcome, _ = store.upsert(lead(source_id="2", title="Arc Flash Study, Building 200"), TODAY)
    assert outcome == "new"


def test_different_states_never_merge(store):
    store.upsert(lead(), TODAY)
    outcome, _ = store.upsert(lead(source_id="2", state="IL", city="Chicago"), TODAY)
    assert outcome == "new"


def test_merge_fills_gaps_but_never_overwrites(store):
    _, first = store.upsert(lead(url="https://sam.gov/opp/1", organization=""), TODAY)
    store.upsert(lead(source="news_rss", source_id="n", url="https://news", organization="Example Bee"), TODAY)
    row = store.get(first)
    assert row["url"] == "https://sam.gov/opp/1"
    assert row["organization"] == "Example Bee"


def test_longer_description_triggers_rescore_before_digest(store):
    _, lead_id = store.upsert(lead(), TODAY)
    store.set_score(lead_id, 50, "near_miss", {})
    store.upsert(lead(description="Scope includes an IEEE 1584 arc flash study."), TODAY)
    assert store.get(lead_id)["score"] is None


def test_titles_match_numbers_guard():
    assert titles_match("arc flash study bldg 22", "arc flash study bldg 22", 0.88)
    assert not titles_match("arc flash study bldg 22", "arc flash study bldg 23", 0.5)


def test_mark_review_validates(store):
    _, lead_id = store.upsert(lead(), TODAY)
    store.mark_review(lead_id, "sent", "emailed Monday")
    assert store.get(lead_id)["review"] == "sent"
    try:
        store.mark_review(lead_id, "maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid status accepted")


def test_same_generic_title_in_different_states_is_not_merged(store):
    store.upsert(lead(source_id="a", title="Arc Flash Study", city="", state="CA"), TODAY)
    outcome, _ = store.upsert(lead(source_id="b", title="Arc Flash Study", city="", state="FL"), TODAY)
    assert outcome == "new"


def test_state_filled_by_merge_triggers_rescore(store):
    _, lead_id = store.upsert(lead(source="news_rss", source_id="n", city="", state=None), TODAY)
    store.set_score(lead_id, 70, "qualified", {})
    store.upsert(lead(source_id="s", city="", state="FL"), TODAY)
    row = store.get(lead_id)
    assert row["state"] == "FL" and row["score"] is None
