from src.extractor import (
    extract_email,
    extract_phone,
    extract_firstname,
    extract_lastname,
    extract_company,
    extract_address,
    extract_city,
    extract_state,
    extract_zip,
    EXTRACTORS,
    find_highlights,
)


# --- email ---
def test_email_basic():
    assert extract_email("Reach me at sebastian@coreelectric.com") == "sebastian@coreelectric.com"


def test_email_plus_tag():
    assert extract_email("Use doug+work@sfwconstruction.com") == "doug+work@sfwconstruction.com"


def test_email_none():
    assert extract_email("No email here just words.") is None


def test_email_takes_latest():
    txt = "old@a.com and then new@b.com"
    assert extract_email(txt) == "new@b.com"


# --- phone ---
def test_phone_paren_format():
    assert extract_phone("Call me at (503) 444-1123 anytime") == "5034441123"


def test_phone_dashes():
    assert extract_phone("My number is 503-444-1123") == "5034441123"


def test_phone_dots():
    assert extract_phone("503.444.1123") == "5034441123"


def test_phone_spelled_out():
    assert extract_phone("five oh three four four four one one two three") == "5034441123"


def test_phone_none():
    assert extract_phone("No digits here friend.") is None


# --- firstname ---
def test_firstname_my_name_is():
    assert extract_firstname("Hi, my name is Sebastian") == "Sebastian"


def test_firstname_this_is():
    assert extract_firstname("Yeah, this is Lavonna speaking") == "Lavonna"


def test_firstname_im():
    assert extract_firstname("Hello there, I'm Bruce") == "Bruce"


def test_firstname_none():
    assert extract_firstname("Good morning, how are you?") is None


# --- lastname ---
def test_lastname_with_firstname():
    assert extract_lastname("my name is Sebastian Torres") == "Torres"


def test_lastname_last_name_is():
    assert extract_lastname("My last name is Torres") == "Torres"


def test_lastname_none():
    assert extract_lastname("my name is Sebastian") is None


# --- company ---
def test_company_from_llc():
    assert extract_company("I'm calling from CORE Electric LLC") == "CORE Electric LLC"


def test_company_with_inc():
    assert extract_company("This is Doug with Acme Construction Inc") == "Acme Construction Inc"


def test_company_none():
    assert extract_company("just a friendly caller") is None


# --- address ---
def test_address_street_number_name():
    assert extract_address("The jobsite is 1516 NE Marie Drive") == "1516 NE Marie Drive"


def test_address_multi_word():
    assert extract_address("We're at 42 West Elm Street, Portland") == "42 West Elm Street"


def test_address_none():
    assert extract_address("No address mentioned") is None


# --- city ---
def test_city_in_portland():
    assert extract_city("We're in Portland, Oregon") == "Portland"


def test_city_in_fall_city():
    assert extract_city("located in Fall City") == "Fall City"


def test_city_none():
    assert extract_city("hello there friend") is None


# --- state ---
def test_state_abbrev():
    assert extract_state("Portland, OR 97230") == "OR"


def test_state_full_name():
    assert extract_state("Portland, Oregon") == "OR"


def test_state_none():
    assert extract_state("just chatting about weather") is None


# --- zip ---
def test_zip_five_digit():
    assert extract_zip("my zip is 97230") == "97230"


def test_zip_plus_four():
    assert extract_zip("97230-1234") == "97230-1234"


def test_zip_none():
    assert extract_zip("let me give you my number 503") is None


# --- bundle ---
def test_extractors_bundle_has_all_nine_keys():
    assert set(EXTRACTORS.keys()) == {
        "firstname", "lastname", "email", "phone", "company",
        "address", "city", "state", "zip",
    }


# --- find_highlights ---
def test_highlights_empty_for_empty_text():
    assert find_highlights("") == []


def test_highlights_returns_spans_with_ruleid():
    text = "Reach me at john@example.com at 503-444-1123."
    hl = find_highlights(text)
    emails = [h for h in hl if h["ruleId"] == "email"]
    phones = [h for h in hl if h["ruleId"] == "phone"]
    assert len(emails) == 1
    assert emails[0]["text"] == "john@example.com"
    assert text[emails[0]["start"]:emails[0]["end"]] == "john@example.com"
    assert len(phones) == 1
    assert phones[0]["text"] == "503-444-1123"


def test_highlights_name_split_by_rep():
    """Given rep_first_name=Doug, 'Jim' is caller-name, 'Doug' is rep-name."""
    text = "Hi, Jim. This is Doug with SFW. Call back at (503) 885-0237."
    hl = find_highlights(text, rep_first_name="Doug")
    names = [(h["ruleId"], h["text"]) for h in hl
             if h["ruleId"] in ("caller-name", "rep-name")]
    assert ("caller-name", "Jim") in names
    assert ("rep-name", "Doug") in names


def test_highlights_name_without_rep_defaults_to_caller():
    """With no rep_first_name, every detected name is tagged caller-name."""
    text = "Hi, Jim. This is Doug."
    hl = find_highlights(text)
    names = {h["text"] for h in hl if h["ruleId"] == "caller-name"}
    assert names == {"Jim", "Doug"}


def test_highlights_ignores_stopword_names():
    """'Hi, there' / 'Hey, guys' should not be tagged as names."""
    hl = find_highlights("Hi, there friend. Hey, guys what's up?")
    names = [h for h in hl if h["ruleId"] in ("caller-name", "rep-name")]
    assert names == []


def test_highlights_rejects_lowercase_filler_after_im():
    """'I'm used to...' must NOT capture 'used' — Deepgram capitalizes real
    names but leaves common words lowercase, so the capture group requires
    title-case even when the trigger phrase is case-insensitive."""
    text = ("I just I'm used to people coming out, looking at it. "
            "Probably just trying to. You gonna come by? "
            "The I'll check back.")
    hl = find_highlights(text)
    names = [h["text"] for h in hl if h["ruleId"] in ("caller-name", "rep-name")]
    assert names == [], f"unexpected name matches: {names}"


def test_firstname_requires_titlecase_after_trigger():
    """Same invariant at the extract_firstname level."""
    assert extract_firstname("I just I'm used to doing that.") is None
    assert extract_firstname("my name is jim (all lower)") is None  # Deepgram would capitalize
    assert extract_firstname("my name is Jim") == "Jim"


def test_highlights_sorted_by_start():
    text = "call 503-444-1123 or email bar@foo.com or hi Jim."
    hl = find_highlights(text)
    starts = [h["start"] for h in hl]
    assert starts == sorted(starts)


def test_highlights_all_rule_ids_are_known():
    """Smoke check on the rule vocabulary the client has to support."""
    text = "Hi, Jim. This is Doug with CORE Electric LLC. Reach me at " \
           "jim@example.com or 503-555-1212. Address is 1516 NE Marie Drive " \
           "in Portland, OR 97230."
    hl = find_highlights(text, rep_first_name="Doug")
    rule_ids = {h["ruleId"] for h in hl}
    allowed = {"caller-name", "rep-name", "email", "phone", "company",
               "address", "city", "state", "zip"}
    assert rule_ids <= allowed
    # Should find at least some of each major category.
    assert "email" in rule_ids
    assert "phone" in rule_ids
    assert "caller-name" in rule_ids
    assert "rep-name" in rule_ids
