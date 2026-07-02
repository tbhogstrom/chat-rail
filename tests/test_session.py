from src.api.session import make_token, valid_token


def test_make_token_is_deterministic_and_password_specific():
    assert make_token("sellit") == make_token("sellit")
    assert make_token("sellit") != make_token("other")


def test_valid_token_accepts_matching():
    assert valid_token(make_token("sellit"), "sellit") is True


def test_valid_token_rejects_tampered_blank_and_none():
    assert valid_token(make_token("sellit") + "x", "sellit") is False
    assert valid_token("", "sellit") is False
    assert valid_token(None, "sellit") is False


def test_valid_token_false_when_password_empty():
    assert valid_token(make_token(""), "") is False
