# Detection Improvements — Design

**Date:** 2026-07-06
**Status:** Approved, ready for implementation plan

## Goal

Improve real-time detection of **emails, phone numbers, and names** in sales-call
transcripts, and make every detection **clickable to populate its field**. The
work targets the deterministic regex layer in `src/extractor.py` — no LLM is
introduced (an end-of-call LLM enrichment pass is a separate, deferred effort).

Scope for this effort (the "first-tier bundle"):

1. **Spoken email** detection — `"john at gmail dot com"` → `john@gmail.com`.
2. **Richer spoken phone** detection — teens/tens/`double`/`triple` + filler-word
   tolerance in spoken number runs.
3. **Stopword expansion** — suppress familiar-address nicknames (`bubba`, `bro`,
   …) and weekday/month names that the name regexes falsely capture.
4. **Validation guards** — NANP phone check; email trailing-dot trim.
5. **Highlight + click-to-populate** — spoken forms are highlighted on the
   transcript (not just field-populated), and clicking any highlight writes its
   canonical value into the corresponding field.

Explicitly **out of scope**: address regex overhaul, ZIP↔state cross-validation,
first-name dictionary, streaming/batch diarization reuse, and the end-of-call LLM
pass. These remain on the backlog.

## Background: how detection works today

- All detection is pure Python regex in **`src/extractor.py`**. No LLM, no NER.
- `src/extraction_worker.py` runs every field extractor + `find_highlights()`
  every 3s over each active session's flat transcript, writing results to Redis.
- Live transcripts come from Deepgram `nova-3` over **8 kHz mulaw, single mixed
  channel, no diarization** (`softphone-bridge/src/deepgram.ts`) — so there is no
  "who said what" signal live, and spoken contact info (emails/phones) frequently
  arrives as words, not literal `@`/digits.
- `find_highlights()` emits `{ruleId, start, end, text}` spans. The dashboard
  (`src/api/static/dashboard.html`) renders them as visual `<mark>` spans and
  **auto-populates** fields from the separate `extracted` dict, skipping any field
  the user has edited (`touchedFields`). There is **no click-to-populate today**.
- `src/sellometer.py` scores calls partly on *which fields were extracted*, so
  recall wins here also improve call scores.

The existing spoken-phone normalizer (`_normalize_spoken_numbers`) builds a
normalized string and **discards offsets** — which is exactly why spoken phones
are not highlighted today.

## Approach

**Span-tracking tokenizer (Approach A).** Tokenize the source once into tokens
that each remember their original character span. Build a normalized string plus
a map back to original offsets. Run the existing regexes against the normalized
string, then map each match back to the original characters for highlighting.
This is reused by both spoken-email and spoken-phone detection.

Rejected alternatives: two-scan reconciliation (brittle — two regexes must agree)
and per-cycle LLM extraction (cost/latency/nondeterminism; belongs in the
deferred end-of-call pass).

## Components

### New module: `src/spoken.py`

The reusable span-preserving machinery.

- `tokenize_with_spans(text) -> list[Token]`
  Splits `text` into word / non-word tokens using `re.finditer`, each carrying
  `(surface: str, start: int, end: int)` referring to the **original** string.

- `SpanMappedText`
  Holds a normalized string plus an offset map back to the original. Exposes:
  - `.text` — the normalized string (what regexes run against).
  - `.map_span(norm_start, norm_end) -> (orig_start, orig_end)` — maps a span in
    the normalized string to the covering span in the original (min original
    start / max original end of the tokens overlapped).

- `normalize_for_phone(tokens) -> SpanMappedText`
  Applies the digit-word policy (see #2 below), preserving the offset map.

- `normalize_for_email(tokens) -> SpanMappedText`
  Applies the spoken-separator policy (see #1 below), preserving the offset map.

`spoken.py` is pure and deterministic — no I/O, no external calls.

### Changed: `src/extractor.py`

Orchestration only. Each affected extractor runs its regex against the relevant
`SpanMappedText.text`, reads the value from match groups, and derives the
highlight span via `.map_span(...)`.

**Highlight schema** grows from `{ruleId, start, end, text}` to:

```
{ruleId, start, end, text, value, field}
```

- `text` — original substring under the `<mark>` (what is shown; e.g.
  `"john at gmail dot com"`).
- `value` — canonical value to place in a field (e.g. `"john@gmail.com"`, phone
  → `"5034441123"`). For typed forms, `value` is the cleaned match.
- `field` — the `extracted` dict key a click populates
  (`phone`→`phone`, `email`→`email`, `caller-name`→`firstname`,
  `address`/`city`/`state`/`zip`/`company` → themselves).
  **`rep-name` has `field = None`** so clicking the rep's own name never fills the
  caller field — this makes "ignore the rep's name" a structural guarantee.

The schema change is additive; the frontend ignores unknown highlight keys, so it
is backward-safe.

### Changed: `src/api/static/dashboard.html`

Minimal, additive:

- `buildHighlightedHTML` emits `data-value` and (when non-null) `data-field`
  attributes on each `<mark>`.
- A single delegated click handler on the transcript body: clicking a mark that
  has a `data-field` writes its `data-value` into that field's input and marks
  the field `touched` (so the 3s poll won't overwrite the user's pick).
- Auto-populate is unchanged. Spoken forms flow through it for free because
  `extract_email` / `extract_phone` now find them.

## Detection behavior changes

### #1 Spoken email

`normalize_for_email` normalizes spoken separators **only between word-ish
tokens**: `" at "→"@"`, `" dot "→"."`, `" underscore "→"_"`,
`" dash "`/`" hyphen "→"-"`, and collapses split providers (`"g mail"→"gmail"`).
The existing `_EMAIL_RE` then runs on the normalized text.

**Precision guard:** accept a spoken (normalized-only) match only when a plausible
TLD follows (`com|net|org|edu|gov|io|co|us`, i.e. the `dot <tld>` at the end of
the address), so a stray "meet me at four" never becomes an email. Literal typed
emails are unaffected — they match regardless of normalization.

### #2 Richer spoken phone

`normalize_for_phone` extends the digit-word vocabulary beyond single digits:

- teens (`ten`–`nineteen`),
- tens (`twenty`–`ninety`, including "twenty three" → `"23"`),
- multipliers `double`/`triple` ("double four" → `"44"`, "triple seven" →
  `"777"`).

Filler words (`um, uh, er, like, so, well`) are treated as **non-breaking
separators** so they don't split a number run. The conservative **≥7 spoken-digit
run gate** is retained to avoid mangling prose ("four dollars"). Each emitted
digit maps back through `SpanMappedText` to the original words for highlighting.

### #3 Stopword expansion

Extend `_NAME_STOPWORDS` with familiar-address nicknames — `bubba, bro, brother,
chief, pal, champ, captain, mister, doc` — plus weekday and month names (Deepgram
title-cases "Monday", which the greeting regex would otherwise capture). One set
literal; zero runtime cost. (Existing entries such as `buddy`, `sir`, `maam`,
`boss` stay.)

### #5 Validation guards

Self-contained, no cross-field coupling:

- **Phone NANP:** reject a match when the area-code or exchange first digit is not
  2–9 — kills order/invoice numbers masquerading as phones.
- **Email:** strip trailing dot(s) from the match (today `[\w.-]+` can swallow a
  sentence-final `.` in `"...com."`).

Both guards apply in `extract_*` **and** in `find_highlights`, so a rejected value
neither populates a field nor draws a mark.

ZIP↔state cross-validation is intentionally excluded (adds field coupling for
marginal gain).

## Data flow

1. Worker reads transcript from Redis.
2. `extract_email` / `extract_phone` run over their `SpanMappedText` normalized
   views (falling back to literal-text matches first, as today).
3. `find_highlights` produces spans enriched with `value` and `field`, using
   `map_span` to anchor spoken forms to original characters.
4. Worker writes `extracted` (field→best value) + `highlights` to Redis.
5. Dashboard auto-populates fields from `extracted` and renders `<mark>` spans;
   clicking a mark populates its `field` with its `value`.

## Error handling

No new failure surface. `spoken.py` is pure/deterministic; the worker already
swallows per-cycle exceptions (`extraction_worker.py`). Every guard **fails
closed** (reject → no field, no mark). Malformed spoken runs simply yield no
match.

## Testing

- **`tests/test_spoken.py`** (new):
  - tokenizer offset round-trips (reconstructing original from token spans);
  - `map_span` correctness on representative normalized→original mappings;
  - phone normalization: teens, tens ("twenty three"→"23"), `double`/`triple`,
    filler-word tolerance, ≥7-run gate still rejects short prose runs;
  - email normalization: `at`/`dot`/`underscore`/`dash`, `"g mail"` collapse.

- **`tests/test_extractor.py`** (extend):
  - spoken email + spoken phone assert both **value** and **span** — verify
    `text[start:end]` covers the real original words;
  - NANP rejection (e.g. area code starting 0/1 → no match);
  - trailing-dot trim on emails;
  - new stopwords (e.g. `bubba`, a weekday) produce no name highlight;
  - highlight dicts include `value` and `field`; `rep-name` has `field == None`.

- **Frontend:** manual verification — no JS test harness exists in the repo.

## Success criteria

- Spoken emails and spoken phones (with the above forms) are extracted **and**
  highlighted on the real transcript with correct spans.
- Clicking any highlight populates its field; clicking a rep-name does not.
- `bubba`/weekday/nickname false-positive names are suppressed.
- Invalid NANP numbers and trailing-dot emails are rejected.
- All existing `tests/test_extractor.py` cases still pass.
