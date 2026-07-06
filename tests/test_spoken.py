from src.spoken import (
    Token,
    tokenize_with_spans,
    SpanMappedText,
    normalize_for_email,
    normalize_for_phone,
)


# --- tokenizer / span mapping ---
def test_tokenize_spans_reconstruct_original():
    text = "call 503 now"
    toks = tokenize_with_spans(text)
    assert all(text[t.start:t.end] == t.surface for t in toks)
    assert "".join(t.surface for t in toks) == text


def test_tokenize_splits_words_and_nonwords():
    toks = tokenize_with_spans("a-b")
    assert [t.surface for t in toks] == ["a", "-", "b"]


def test_span_mapped_text_builds_normalized_string():
    smt = SpanMappedText([("john", 0, 4), ("@", 5, 7), ("x", 8, 9)])
    assert smt.text == "john@x"


def test_span_mapped_text_maps_back_to_original():
    smt = SpanMappedText([("john", 0, 4), ("@", 5, 7), ("x", 8, 9)])
    assert smt.map_span(0, 6) == (0, 9)
    assert smt.map_span(4, 6) == (5, 9)


def test_span_mapped_text_ignores_dropped_segments():
    smt = SpanMappedText([("5", 0, 4), ("", 4, 5), ("0", 5, 8)])
    assert smt.text == "50"
    assert smt.map_span(0, 2) == (0, 8)


# --- email normalization ---
def test_normalize_email_at_and_dot():
    toks = tokenize_with_spans("john at gmail dot com")
    assert normalize_for_email(toks).text == "john@gmail.com"


def test_normalize_email_underscore_and_dash():
    toks = tokenize_with_spans("a underscore b dash c at x dot io")
    assert normalize_for_email(toks).text == "a_b-c@x.io"


def test_normalize_email_collapses_g_mail():
    toks = tokenize_with_spans("bob at g mail dot com")
    assert normalize_for_email(toks).text == "bob@gmail.com"


def test_normalize_email_preserves_preceding_words():
    toks = tokenize_with_spans("call me john at x dot com")
    assert normalize_for_email(toks).text == "call me john@x.com"


# --- phone normalization ---
def test_normalize_phone_single_digits():
    toks = tokenize_with_spans("five oh three four four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_teens_and_tens():
    toks = tokenize_with_spans("five oh three four four four eleven twenty three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_double():
    toks = tokenize_with_spans("five oh three double four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_filler_does_not_break_run():
    toks = tokenize_with_spans("five oh three um four four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_short_run_left_as_words():
    toks = tokenize_with_spans("I have four dogs")
    assert normalize_for_phone(toks).text == "I have four dogs"
