from bdrep.normalize import (
    clean_title,
    detect_states,
    match_terms,
    norm_address,
    norm_text,
    norm_title,
    pick_state,
)


def test_norm_text_punctuation_and_case():
    assert norm_text("Arc-Flash & Short-Circuit (IEEE 1584)") == "arc flash and short circuit ieee 1584"


def test_clean_title_strips_sam_product_code():
    assert clean_title("Z1DA--Replace Main Switchgear") == "Replace Main Switchgear"
    assert clean_title("J065-- Arc Flash Study") == "Arc Flash Study"
    assert clean_title("Arc Flash Study -- Phase 2") == "Arc Flash Study -- Phase 2"


def test_norm_title_drops_stopwords_and_prefix():
    assert norm_title("J065--The Arc Flash Study for Building 22") == "arc flash study building 22"


def test_norm_address_abbreviates():
    assert norm_address("100 North Main Street, Suite 4", "San Jose") == "100 n main st ste 4 san jose"


def test_match_terms_whole_words_only():
    text = norm_text("Business park in Bessemer gets a new BESS and switchgear")
    assert match_terms(text, ["bess", "switchgear", "ness"]) == ["bess", "switchgear"]


def test_detect_states_names_cities_and_abbreviations():
    assert detect_states("New substation in Fresno County") == ["CA"]
    assert detect_states("Peoria, Ill. plant") == ["IL"]
    assert set(detect_states("Project in Austin, TX and Miami")) == {"TX", "FL"}


def test_pick_state_prefers_target_state():
    assert pick_state("A Texas firm wins a Chicago job", ["CA", "IL", "FL"]) == "IL"
    assert pick_state("Texas data center", ["CA", "IL", "FL"]) == "TX"
    assert pick_state("A data center somewhere", ["CA", "IL", "FL"]) is None


def test_ambiguous_places_are_not_guessed():
    assert pick_state("Springfield council approves substation", ["CA", "IL", "FL"]) is None
