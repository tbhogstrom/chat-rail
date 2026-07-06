# Detection Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect spoken emails/phones in call transcripts, suppress nickname/rep-name false positives, validate matches, and make every highlight clickable to populate its field.

**Architecture:** A new pure module `src/spoken.py` tokenizes the transcript into spans, produces normalized views (spoken separators/digit-words → symbols/digits) that preserve a map back to original character offsets, and lets `src/extractor.py` detect spoken forms while still highlighting the real transcript text. Highlights gain `value` (canonical field value) and `field` (which field a click fills). The dashboard renders those as data attributes and wires click-to-populate.

**Tech Stack:** Python 3.14 (stdlib `re`, `dataclasses`), pytest, vanilla JS in `dashboard.html`.

## Global Constraints

- No new Python dependencies. Standard library only in `src/spoken.py`.
- Python type hints use `str | None` style (3.10+ unions) — matches existing code.
- Run tests with `python -m pytest` from the repo root.
- All 41 existing tests in `tests/test_extractor.py` must stay green.
- Commit messages: **do NOT** include any `Co-Authored-By: Claude` trailer (repo convention).
- Work happens on branch `detection-improvements` (already checked out).

---

## File Structure

- **Create `src/spoken.py`** — `Token`, `tokenize_with_spans`, `SpanMappedText`, `normalize_for_email`, `normalize_for_phone`. Pure/deterministic.
- **Create `tests/test_spoken.py`** — unit tests for the tokenizer, span mapping, and both normalizers.
- **Modify `src/extractor.py`** — email/phone use spoken normalization + validation; stopword set expanded; `find_highlights` emits `value`/`field`, adds spoken passes, dedups, and applies guards.
- **Modify `tests/test_extractor.py`** — extend with spoken-form, validation, stopword, and schema tests.
- **Modify `src/api/static/dashboard.html`** — emit `data-value`/`data-field`; add click-to-populate handler.

---

## Task 1: Span-tracking tokenizer (`src/spoken.py` core)

**Files:**
- Create: `src/spoken.py`
- Test: `tests/test_spoken.py`

**Interfaces:**
- Produces:
  - `Token` — frozen dataclass with `.surface: str`, `.start: int`, `.end: int`.
  - `tokenize_with_spans(text: str) -> list[Token]` — word (`\w+`) and single non-word tokens, spans into the original string.
  - `SpanMappedText(segments: list[tuple[str, int, int]])` — each segment is `(normalized_text, orig_start, orig_end)`. Exposes `.text: str` and `.map_span(norm_start: int, norm_end: int) -> tuple[int, int] | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spoken.py
from src.spoken import Token, tokenize_with_spans, SpanMappedText


def test_tokenize_spans_reconstruct_original():
    text = "call 503 now"
    toks = tokenize_with_spans(text)
    # Every token's span slices back to its surface.
    assert all(text[t.start:t.end] == t.surface for t in toks)
    # Concatenating surfaces in order reproduces the text.
    assert "".join(t.surface for t in toks) == text


def test_tokenize_splits_words_and_nonwords():
    toks = tokenize_with_spans("a-b")
    assert [t.surface for t in toks] == ["a", "-", "b"]


def test_span_mapped_text_builds_normalized_string():
    smt = SpanMappedText([("john", 0, 4), ("@", 5, 7), ("x", 8, 9)])
    assert smt.text == "john@x"


def test_span_mapped_text_maps_back_to_original():
    # normalized "john@x" where "@" came from the word "at" at 5..7.
    smt = SpanMappedText([("john", 0, 4), ("@", 5, 7), ("x", 8, 9)])
    # A normalized span covering all three segments maps to 0..9 in the source.
    assert smt.map_span(0, 6) == (0, 9)
    # A normalized span covering only "@x" maps to 5..9.
    assert smt.map_span(4, 6) == (5, 9)


def test_span_mapped_text_ignores_dropped_segments():
    # A dropped token (norm "") contributes no characters and no span.
    smt = SpanMappedText([("5", 0, 4), ("", 4, 5), ("0", 5, 8)])
    assert smt.text == "50"
    assert smt.map_span(0, 2) == (0, 8)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spoken.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.spoken'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/spoken.py
"""Span-preserving tokenization + normalization for spoken contact info.

Deepgram transcribes voiced emails/phones as words ("john at gmail dot com",
"five oh three ..."). To BOTH extract the canonical value AND highlight the real
transcript text, we normalize a copy while keeping a map back to the original
character offsets.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    surface: str
    start: int
    end: int


_TOKEN_RE = re.compile(r"\w+|\W")


def tokenize_with_spans(text: str) -> list[Token]:
    """Split text into word runs (\\w+) and single non-word characters, each
    carrying its (start, end) offsets in the original string."""
    return [Token(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


class SpanMappedText:
    """A normalized string plus a map from normalized offsets back to the
    original. Built from segments of (normalized_text, orig_start, orig_end).
    A segment with empty normalized_text is 'dropped' — it occupies no space in
    the normalized string and never contributes to a mapped span.
    """

    def __init__(self, segments: list[tuple[str, int, int]]):
        self._parts: list[tuple[int, int, int, int]] = []  # ns, ne, orig_start, orig_end
        chunks: list[str] = []
        pos = 0
        for norm, orig_start, orig_end in segments:
            start = pos
            chunks.append(norm)
            pos += len(norm)
            self._parts.append((start, pos, orig_start, orig_end))
        self.text = "".join(chunks)

    def map_span(self, norm_start: int, norm_end: int) -> tuple[int, int] | None:
        """Map a [norm_start, norm_end) span in the normalized string to the
        covering [orig_start, orig_end) span in the original, or None if nothing
        overlaps."""
        orig_start: int | None = None
        orig_end: int | None = None
        for ns, ne, os_, oe in self._parts:
            if ne <= norm_start or ns >= norm_end:  # no overlap
                continue
            if ne == ns:  # dropped (zero-width) segment
                continue
            if orig_start is None or os_ < orig_start:
                orig_start = os_
            if orig_end is None or oe > orig_end:
                orig_end = oe
        if orig_start is None or orig_end is None:
            return None
        return (orig_start, orig_end)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_spoken.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/spoken.py tests/test_spoken.py
git commit -m "feat(spoken): span-tracking tokenizer and normalized-text mapping"
```

---

## Task 2: Spoken-email normalization + `extract_email` upgrade

**Files:**
- Modify: `src/spoken.py`
- Modify: `src/extractor.py:11-15`
- Test: `tests/test_spoken.py`, `tests/test_extractor.py`

**Interfaces:**
- Consumes: `Token`, `tokenize_with_spans`, `SpanMappedText` (Task 1).
- Produces: `normalize_for_email(tokens: list[Token]) -> SpanMappedText`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spoken.py  (append)
from src.spoken import normalize_for_email  # add to imports at top


def test_normalize_email_at_and_dot():
    toks = tokenize_with_spans("john at gmail dot com")
    smt = normalize_for_email(toks)
    assert smt.text == "john@gmail.com"


def test_normalize_email_underscore_and_dash():
    toks = tokenize_with_spans("a underscore b dash c at x dot io")
    smt = normalize_for_email(toks)
    assert smt.text == "a_b-c@x.io"


def test_normalize_email_collapses_g_mail():
    toks = tokenize_with_spans("bob at g mail dot com")
    smt = normalize_for_email(toks)
    assert smt.text == "bob@gmail.com"


def test_normalize_email_preserves_preceding_words():
    # Words before the local part stay separated so the email regex bounds
    # the local part correctly.
    toks = tokenize_with_spans("call me john at x dot com")
    smt = normalize_for_email(toks)
    assert smt.text == "call me john@x.com"
```

```python
# tests/test_extractor.py  (append to the email section)
def test_email_spoken_form():
    assert extract_email("reach me at john at gmail dot com ok") == "john@gmail.com"


def test_email_spoken_maps_to_real_span():
    text = "my email is bob at x dot com thanks"
    hl = find_highlights(text)
    emails = [h for h in hl if h["ruleId"] == "email"]
    assert len(emails) == 1
    assert emails[0]["value"] == "bob@x.com"
    # The highlighted span covers the ORIGINAL spoken words.
    assert text[emails[0]["start"]:emails[0]["end"]] == "bob at x dot com"


def test_email_trailing_dot_trimmed():
    assert extract_email("write to me at jim@x.com.") == "jim@x.com"


def test_email_spoken_requires_known_tld():
    # "meet me at four" must not become an email.
    assert extract_email("let's meet me at four") is None
```

Note: `test_email_spoken_maps_to_real_span` depends on `find_highlights` emitting
`value` and spoken passes — implemented in Task 5. If running tasks strictly in
order, move that single test to Task 5. The `extract_email` tests here pass at
the end of Task 2.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_spoken.py -q -k email`
Expected: FAIL with `ImportError: cannot import name 'normalize_for_email'`.

- [ ] **Step 3: Implement `normalize_for_email` in `src/spoken.py`**

```python
# src/spoken.py  (append)

_EMAIL_WORDS = {
    "at": "@", "dot": ".", "period": ".",
    "underscore": "_", "dash": "-", "hyphen": "-",
}
# Two-token providers Deepgram sometimes splits ("g mail" -> "gmail").
_PROVIDER_COLLAPSE = {("g", "mail"): "gmail"}


def normalize_for_email(tokens: list[Token]) -> SpanMappedText:
    """Normalize spoken email separators into symbols while preserving offsets.

    - separator words ("at","dot","underscore","dash","hyphen") -> symbols,
      dropping whitespace immediately adjacent so "john at x" -> "john@x";
    - known split providers collapse ("g mail" -> "gmail");
    - all other text is preserved verbatim (so word boundaries still bound the
      email's local part).
    """
    segments: list[tuple[str, int, int]] = []
    strip_next_ws = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        low = tok.surface.lower()

        # Provider collapse: single letter + whitespace + word.
        if i + 2 < n and tokens[i + 1].surface.isspace():
            key = (low, tokens[i + 2].surface.lower())
            if key in _PROVIDER_COLLAPSE:
                segments.append((_PROVIDER_COLLAPSE[key], tok.start, tokens[i + 2].end))
                i += 3
                strip_next_ws = False
                continue

        if low in _EMAIL_WORDS:
            # Drop a whitespace segment we already emitted right before this.
            if segments and segments[-1][0].isspace():
                prev = segments[-1]
                segments[-1] = ("", prev[1], prev[2])
            segments.append((_EMAIL_WORDS[low], tok.start, tok.end))
            strip_next_ws = True
            i += 1
            continue

        if tok.surface.isspace() and strip_next_ws:
            segments.append(("", tok.start, tok.end))  # dropped
            strip_next_ws = False
            i += 1
            continue

        strip_next_ws = False
        segments.append((tok.surface, tok.start, tok.end))
        i += 1

    return SpanMappedText(segments)
```

- [ ] **Step 4: Upgrade `extract_email` in `src/extractor.py`**

Replace the EMAIL section (`src/extractor.py:10-15`) with:

```python
# ---------------------------------------------------------------- EMAIL
from src.spoken import tokenize_with_spans, normalize_for_email  # top-of-file import

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_EMAIL_TLD_RE = re.compile(r"\.(com|net|org|edu|gov|io|co|us)$", re.IGNORECASE)


def _clean_email(match: str) -> str:
    """Trim sentence-final dots the domain regex may swallow ('x.com.')."""
    return match.rstrip(".")


def extract_email(text: str) -> str | None:
    # First pass: literal emails in the original text.
    matches = _EMAIL_RE.findall(text)
    if matches:
        return _clean_email(matches[-1])
    # Second pass: spoken emails ("john at gmail dot com"), TLD-guarded.
    normalized = normalize_for_email(tokenize_with_spans(text)).text
    spoken = [m for m in _EMAIL_RE.findall(normalized) if _EMAIL_TLD_RE.search(m)]
    return _clean_email(spoken[-1]) if spoken else None
```

Move the `from src.spoken import ...` line to the top of `src/extractor.py` with
the other imports (line 7 area), importing the names used across the module:
`from src.spoken import tokenize_with_spans, normalize_for_email, normalize_for_phone`.
(`normalize_for_phone` is added in Task 3; add the import now to avoid churn — if
running Task 2 in isolation, import only the two email names and extend in Task 3.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_spoken.py tests/test_extractor.py -q -k "email"`
Expected: PASS. (`test_email_spoken_maps_to_real_span` still fails until Task 5 —
skip it here if executing strictly in order.)

- [ ] **Step 6: Commit**

```bash
git add src/spoken.py src/extractor.py tests/test_spoken.py tests/test_extractor.py
git commit -m "feat(extractor): detect spoken emails and trim trailing dots"
```

---

## Task 3: Spoken-phone normalization + `extract_phone` upgrade

**Files:**
- Modify: `src/spoken.py`
- Modify: `src/extractor.py:18-76`
- Test: `tests/test_spoken.py`, `tests/test_extractor.py`

**Interfaces:**
- Consumes: `Token`, `tokenize_with_spans`, `SpanMappedText` (Task 1).
- Produces: `normalize_for_phone(tokens: list[Token]) -> SpanMappedText`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spoken.py  (append)
from src.spoken import normalize_for_phone  # add to imports


def test_normalize_phone_single_digits():
    toks = tokenize_with_spans("five oh three four four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_teens_and_tens():
    # 503 444 11 23  spoken with teens/tens
    toks = tokenize_with_spans("five oh three four four four eleven twenty three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_double():
    toks = tokenize_with_spans("five oh three double four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_filler_does_not_break_run():
    toks = tokenize_with_spans("five oh three um four four four one one two three")
    assert normalize_for_phone(toks).text == "5034441123"


def test_normalize_phone_short_run_left_as_words():
    # Fewer than 7 spoken digits: not converted, original words preserved.
    toks = tokenize_with_spans("I have four dogs")
    assert normalize_for_phone(toks).text == "I have four dogs"
```

```python
# tests/test_extractor.py  (append to phone section)
def test_phone_spoken_teens_tens():
    assert extract_phone("five oh three four four four eleven twenty three") == "5034441123"


def test_phone_spoken_double():
    assert extract_phone("five oh three double four four one one two three") == "5034441123"


def test_phone_spoken_with_filler():
    assert extract_phone("five oh three um four four four one one two three") == "5034441123"


def test_phone_rejects_invalid_nanp():
    # Area code starting with 1 is not a valid NANP number.
    assert extract_phone("Call 155-444-1123 now") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_spoken.py -q -k phone`
Expected: FAIL with `ImportError: cannot import name 'normalize_for_phone'`.

- [ ] **Step 3: Implement `normalize_for_phone` in `src/spoken.py`**

```python
# src/spoken.py  (append)

_ONES = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_TEENS = {
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19",
}
_TENS = {
    "twenty": "2", "thirty": "3", "forty": "4", "fifty": "5",
    "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9",
}
_MULT = {"double": 2, "triple": 3}
_PHONE_FILLER = {"um", "uh", "er", "like", "so", "well"}
_MIN_SPOKEN_RUN = 7  # digits — matches the conservative gate in the old code


def normalize_for_phone(tokens: list[Token]) -> SpanMappedText:
    """Convert runs of >=7 spoken digits to raw digits, preserving offsets.

    Supports single digits, teens (ten-nineteen), tens (twenty-ninety incl.
    'twenty three'->'23'), and 'double'/'triple' multipliers. Whitespace and
    filler words ('um','uh',...) don't break a run. Runs shorter than 7 digits
    are emitted as their original words (so prose like 'four dogs' is untouched).
    """
    segments: list[tuple[str, int, int]] = []
    run_digits: list[str] = []
    run_tokens: list[Token] = []
    pending_mult = 1
    pending_tens: str | None = None

    def resolve_tens() -> None:
        nonlocal pending_tens
        if pending_tens is not None:
            run_digits.append(pending_tens + "0")  # e.g. "twenty" alone -> "20"
            pending_tens = None

    def flush() -> None:
        nonlocal pending_mult, pending_tens
        resolve_tens()
        digits = "".join(run_digits)
        if len(digits) >= _MIN_SPOKEN_RUN:
            segments.append((digits, run_tokens[0].start, run_tokens[-1].end))
        else:
            for t in run_tokens:
                segments.append((t.surface, t.start, t.end))
        run_digits.clear()
        run_tokens.clear()
        pending_mult = 1
        pending_tens = None

    for tok in tokens:
        surf = tok.surface
        low = surf.lower()

        if surf.isspace():
            (run_tokens if run_tokens else segments).append(
                tok if run_tokens else (surf, tok.start, tok.end)
            )
            continue
        if low in _PHONE_FILLER:
            (run_tokens if run_tokens else segments).append(
                tok if run_tokens else (surf, tok.start, tok.end)
            )
            continue
        if low in _MULT:
            resolve_tens()
            pending_mult = _MULT[low]
            run_tokens.append(tok)
            continue
        if low in _TENS:
            resolve_tens()
            pending_tens = _TENS[low]
            run_tokens.append(tok)
            continue
        if low in _TEENS:
            resolve_tens()
            run_digits.append(_TEENS[low])
            pending_mult = 1
            run_tokens.append(tok)
            continue
        if low in _ONES:
            digit = _ONES[low]
            if pending_tens is not None:
                run_digits.append(pending_tens + digit)  # "twenty three" -> "23"
                pending_tens = None
            else:
                run_digits.append(digit * pending_mult)  # "double four" -> "44"
            pending_mult = 1
            run_tokens.append(tok)
            continue

        # Non-digit word: break the run, then emit the word.
        flush()
        segments.append((surf, tok.start, tok.end))

    flush()
    return SpanMappedText(segments)
```

Note on the `isspace`/filler branches: `run_tokens` holds `Token` objects while
`segments` holds `(str, int, int)` tuples. Write those two branches explicitly
to keep types clear:

```python
        if surf.isspace() or low in _PHONE_FILLER:
            if run_tokens:
                run_tokens.append(tok)          # keep span, add no digits
            else:
                segments.append((surf, tok.start, tok.end))
            continue
```

Use this explicit form (replacing the two conditional-append branches above).

- [ ] **Step 4: Upgrade `extract_phone` in `src/extractor.py`**

Add a NANP validity helper and route the spoken pass through `normalize_for_phone`.
Replace the PHONE section's `extract_phone` (and delete the now-unused
`_normalize_spoken_numbers` + `_SPOKEN_DIGITS`, which `src/spoken.py` supersedes):

```python
def _valid_nanp(area: str, exchange: str) -> bool:
    """NANP: area code and exchange must start with 2-9."""
    return area[0] in "23456789" and exchange[0] in "23456789"


def extract_phone(text: str) -> str | None:
    """Return the most recent valid 10-digit phone number seen in the text."""
    for source in (text, normalize_for_phone(tokenize_with_spans(text)).text):
        valid = [(a, b, c) for a, b, c in _PHONE_NUM_RE.findall(source)
                 if _valid_nanp(a, b)]
        if valid:
            a, b, c = valid[-1]
            return a + b + c
    return None
```

Keep `_PHONE_NUM_RE` as-is. Remove `_SPOKEN_DIGITS` and `_normalize_spoken_numbers`
(lines 28-60) — they are replaced by `src/spoken.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_spoken.py tests/test_extractor.py -q -k "phone"`
Expected: PASS (including the existing `test_phone_spelled_out`).

- [ ] **Step 6: Run the full extractor + spoken suites**

Run: `python -m pytest tests/test_spoken.py tests/test_extractor.py -q`
Expected: PASS (no regressions; `test_email_spoken_maps_to_real_span` still
pending Task 5).

- [ ] **Step 7: Commit**

```bash
git add src/spoken.py src/extractor.py tests/test_spoken.py tests/test_extractor.py
git commit -m "feat(extractor): richer spoken phone parsing and NANP validation"
```

---

## Task 4: Stopword expansion

**Files:**
- Modify: `src/extractor.py:239-243`
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `find_highlights` (existing).
- Produces: no new symbols; extends `_NAME_STOPWORDS`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extractor.py  (append to find_highlights section)
def test_highlights_ignore_nickname_names():
    hl = find_highlights("Hey, bubba. Hi, chief. Hello bro.")
    names = [h for h in hl if h["ruleId"] in ("caller-name", "rep-name")]
    assert names == []


def test_highlights_ignore_weekday_names():
    hl = find_highlights("Hi, Monday. This is Friday.")
    names = [h for h in hl if h["ruleId"] in ("caller-name", "rep-name")]
    assert names == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractor.py -q -k "nickname or weekday"`
Expected: FAIL (e.g. `bubba`/`Monday` captured as caller-name).

- [ ] **Step 3: Extend `_NAME_STOPWORDS`**

Replace the set at `src/extractor.py:239-243` with:

```python
_NAME_STOPWORDS = {
    "there", "guys", "everyone", "everybody", "you", "back", "man", "dude",
    "sir", "madam", "maam", "folks", "yall", "again", "now", "buddy",
    "honey", "babe", "friend", "sweetie", "boss",
    # familiar-address nicknames
    "bubba", "bro", "brother", "chief", "pal", "champ", "captain", "mister", "doc",
    # weekdays / months (Deepgram title-cases these; greeting regex grabs them)
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extractor.py -q -k "nickname or weekday"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/extractor.py tests/test_extractor.py
git commit -m "feat(extractor): suppress nickname and weekday name false positives"
```

---

## Task 5: Highlight schema (`value`/`field`) + spoken passes + guards

**Files:**
- Modify: `src/extractor.py:246-309` (`find_highlights`)
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `normalize_for_email`, `normalize_for_phone`, `tokenize_with_spans`,
  `SpanMappedText.map_span`, `_valid_nanp`, `_clean_email`, `_EMAIL_TLD_RE`.
- Produces: each highlight dict now has keys `ruleId, start, end, text, value, field`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extractor.py  (append)
def test_highlights_have_value_and_field():
    text = "Reach me at john@example.com or 503-444-1123."
    hl = find_highlights(text)
    email = next(h for h in hl if h["ruleId"] == "email")
    phone = next(h for h in hl if h["ruleId"] == "phone")
    assert email["value"] == "john@example.com"
    assert email["field"] == "email"
    assert phone["value"] == "5034441123"
    assert phone["field"] == "phone"


def test_highlights_rep_name_has_null_field():
    hl = find_highlights("This is Doug.", rep_first_name="Doug")
    rep = next(h for h in hl if h["ruleId"] == "rep-name")
    assert rep["field"] is None
    caller = find_highlights("Hi, Jim.")
    jim = next(h for h in caller if h["ruleId"] == "caller-name")
    assert jim["field"] == "firstname"


def test_highlights_spoken_phone_mapped_and_valued():
    text = "call me at five oh three four four four one one two three please"
    hl = find_highlights(text)
    phones = [h for h in hl if h["ruleId"] == "phone"]
    assert len(phones) == 1
    assert phones[0]["value"] == "5034441123"
    assert text[phones[0]["start"]:phones[0]["end"]] == \
        "five oh three four four four one one two three"


def test_highlights_no_duplicate_typed_phone():
    # A typed number appears in both literal and normalized text; only one mark.
    hl = find_highlights("call 503-444-1123")
    phones = [h for h in hl if h["ruleId"] == "phone"]
    assert len(phones) == 1
```

Plus the `test_email_spoken_maps_to_real_span` from Task 2 now passes here.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extractor.py -q -k "value_and_field or spoken_phone_mapped or duplicate_typed or rep_name_has_null"`
Expected: FAIL with `KeyError: 'value'` / missing marks.

- [ ] **Step 3: Rewrite `find_highlights`**

Replace `find_highlights` (`src/extractor.py:246-309`) with the version below.
It adds `value`/`field`, spoken email/phone passes via `map_span`, guards, and a
final dedup by `(ruleId, start, end)`.

```python
_FIELD_BY_RULE = {
    "caller-name": "firstname",
    "rep-name": None,
    "email": "email",
    "phone": "phone",
    "company": "company",
    "address": "address",
    "city": "city",
    "state": "state",
    "zip": "zip",
}


def find_highlights(text: str, rep_first_name: str | None = None) -> list[dict]:
    """Return every span worth highlighting, each as
    `{ruleId, start, end, text, value, field}` sorted by start offset.

    `value` is the canonical value a click drops into `field`; `field` is the
    extracted-dict key to populate (None for rep-name, which must never fill the
    caller field).
    """
    out: list[dict] = []

    def add(rule_id: str, start: int, end: int, value: str | None = None) -> None:
        surface = text[start:end]
        out.append({
            "ruleId": rule_id,
            "start": start,
            "end": end,
            "text": surface,
            "value": value if value is not None else surface,
            "field": _FIELD_BY_RULE.get(rule_id),
        })

    def add_name(start: int, end: int) -> None:
        token = text[start:end]
        if token.lower() in _NAME_STOPWORDS:
            return
        rule = "caller-name"
        if rep_first_name and token.lower() == rep_first_name.lower():
            rule = "rep-name"
        add(rule, start, end)

    # Names — trigger phrase or greeting.
    for m in _FIRSTNAME_RE.finditer(text):
        add_name(*m.span(1))
    for m in _GREETING_NAME_RE.finditer(text):
        add_name(*m.span(1))

    # Email — literal pass.
    for m in _EMAIL_RE.finditer(text):
        add("email", m.start(0), m.end(0), _clean_email(m.group(0)))
    # Email — spoken pass, mapped back to the original text.
    smt_email = normalize_for_email(tokenize_with_spans(text))
    for m in _EMAIL_RE.finditer(smt_email.text):
        if not _EMAIL_TLD_RE.search(m.group(0)):
            continue
        span = smt_email.map_span(m.start(0), m.end(0))
        if span:
            add("email", span[0], span[1], _clean_email(m.group(0)))

    # Phone — literal pass (NANP-guarded).
    for m in _PHONE_NUM_RE.finditer(text):
        if _valid_nanp(m.group(1), m.group(2)):
            add("phone", m.start(0), m.end(0), m.group(1) + m.group(2) + m.group(3))
    # Phone — spoken pass, mapped back to the original text.
    smt_phone = normalize_for_phone(tokenize_with_spans(text))
    for m in _PHONE_NUM_RE.finditer(smt_phone.text):
        if not _valid_nanp(m.group(1), m.group(2)):
            continue
        span = smt_phone.map_span(m.start(0), m.end(0))
        if span:
            add("phone", span[0], span[1], m.group(1) + m.group(2) + m.group(3))

    # Company, address — group 1 holds the useful span.
    for m in _COMPANY_RE.finditer(text):
        add("company", *m.span(1))
    for m in _ADDRESS_RE.finditer(text):
        add("address", *m.span(1))

    # City.
    for m in _CITY_BEFORE_STATE_RE.finditer(text):
        add("city", *m.span(1))
    for m in _CITY_IN_CITY_RE.finditer(text):
        add("city", *m.span(1))

    # State — abbrev in context, or full name (value normalized to abbrev).
    for m in _STATE_ABBREV_IN_CONTEXT_RE.finditer(text):
        add("state", *m.span(1))
    for m in _STATE_FULLNAME_RE.finditer(text):
        add("state", m.start(1), m.end(1),
            _STATE_NAME_TO_ABBREV[m.group(1).lower()])

    # ZIP.
    for m in _ZIP_RE.finditer(text):
        add("zip", *m.span(1))

    # Dedup identical spans (e.g. a typed number caught by literal + spoken pass).
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict] = []
    for h in out:
        key = (h["ruleId"], h["start"], h["end"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    deduped.sort(key=lambda h: h["start"])
    return deduped
```

- [ ] **Step 4: Run the failing tests to verify they pass**

Run: `python -m pytest tests/test_extractor.py -q -k "value_and_field or spoken_phone_mapped or duplicate_typed or rep_name_has_null or spoken_maps_to_real_span"`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tests, no regressions).

- [ ] **Step 6: Commit**

```bash
git add src/extractor.py tests/test_extractor.py
git commit -m "feat(extractor): value/field on highlights, spoken passes, dedup"
```

---

## Task 6: Dashboard click-to-populate

**Files:**
- Modify: `src/api/static/dashboard.html:193-219` (`buildHighlightedHTML`) and the
  click-handler region (near `:273`).
- Test: manual (no JS harness in the repo).

**Interfaces:**
- Consumes: highlight dicts with `value`/`field` (Task 5); existing
  `getFieldInput(key)`, `touchedFields`.

- [ ] **Step 1: Emit `data-value` / `data-field` in `buildHighlightedHTML`**

In `src/api/static/dashboard.html`, change the `<mark>` emit line (`:215`) from:

```javascript
          html += `<mark class="hl hl-${primary.ruleId}">${escapeHtml(chunk)}</mark>`;
```

to (build the attribute string first so a null field emits no attribute):

```javascript
          const dataAttrs =
            ` data-value="${escapeHtml(primary.value ?? "")}"` +
            (primary.field ? ` data-field="${escapeHtml(primary.field)}"` : "");
          html += `<mark class="hl hl-${primary.ruleId}"${dataAttrs}>${escapeHtml(chunk)}</mark>`;
```

- [ ] **Step 2: Make highlighted marks look clickable (CSS)**

In the `<style>` block near the `mark.hl` rule (`:61`), add:

```css
    mark.hl[data-field] { cursor: pointer; }
```

- [ ] **Step 3: Add a delegated click handler**

After the "inputs track touched" block (`src/api/static/dashboard.html:273-278`), add:

```javascript
    // ---- click a highlight to populate its field ----
    document.getElementById("transcript-body").addEventListener("click", (e) => {
      const mark = e.target.closest("mark.hl[data-field]");
      if (!mark) return;
      const key = mark.dataset.field;
      const input = getFieldInput(key);
      if (!input) return;
      input.value = mark.dataset.value || "";
      touchedFields.add(key);
      const dot = getFieldDot(key);
      if (dot && input.value) dot.classList.add("green");
    });
```

- [ ] **Step 4: Manual verification**

Run the app locally (per project run instructions) or open a session with a live
call. Confirm:
- Typed and spoken emails/phones both show underline highlights.
- Clicking a phone/email/name highlight fills the matching field input.
- Clicking the rep's name (rendered `hl-rep-name`, no `data-field`) does nothing.

If no live call is available, this can be smoke-checked by pasting a sample
`highlights` payload into `buildHighlightedHTML` in the browser console and
asserting the emitted HTML carries `data-value`/`data-field`.

- [ ] **Step 5: Commit**

```bash
git add src/api/static/dashboard.html
git commit -m "feat(dashboard): click a transcript highlight to populate its field"
```

---

## Final verification

- [ ] Run the whole suite: `python -m pytest -q` → all pass.
- [ ] Confirm `git log --oneline` shows the six task commits on `detection-improvements`.
- [ ] (Optional) Use `superpowers:requesting-code-review` before merging.

---

## Self-Review

**Spec coverage:**
- #1 spoken email → Task 2 (+ highlight in Task 5). ✓
- #2 richer spoken phone → Task 3 (+ highlight in Task 5). ✓
- #3 stopword expansion → Task 4. ✓
- #5 validation guards (NANP, trailing dot) → Tasks 2 & 3, enforced in highlights in Task 5. ✓
- Highlight + click-to-populate (`value`/`field`, rep-name null) → Tasks 5 & 6. ✓
- Span-tracking tokenizer (Approach A) → Task 1. ✓
- ZIP↔state excluded per spec. ✓ (no task, intentional)

**Type consistency:** `SpanMappedText.map_span` returns `tuple[int,int] | None`; callers in Task 5 guard on `if span:`. `Token` fields `surface/start/end` used consistently. `normalize_for_email` / `normalize_for_phone` signatures identical across Tasks 2/3/5. `_valid_nanp(area, exchange)`, `_clean_email(match)`, `_EMAIL_TLD_RE` defined in Tasks 2/3 and reused in Task 5.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The one cross-task test note (`test_email_spoken_maps_to_real_span`) is called out explicitly with where it lands.
