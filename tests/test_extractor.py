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
