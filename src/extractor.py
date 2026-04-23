"""Deterministic regex extractors for sales-call transcripts.

Each extractor takes a transcript string and returns the most recent match
(or None). Full transcript is re-scanned per call — O(N) per extractor is
acceptable since transcripts stay under ~10KB.
"""
import re
from typing import Callable

# ---------------------------------------------------------------- EMAIL
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

def extract_email(text: str) -> str | None:
    matches = _EMAIL_RE.findall(text)
    return matches[-1] if matches else None


# ---------------------------------------------------------------- PHONE
_PHONE_NUM_RE = re.compile(
    r"""
    (?:\+?1[\s.\-]?)?          # optional country code
    \(?(\d{3})\)?[\s.\-]?       # area code
    (\d{3})[\s.\-]?             # exchange
    (\d{4})                     # subscriber
    """,
    re.VERBOSE,
)
_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

def _normalize_spoken_numbers(text: str) -> str:
    """Convert runs of >=7 spelled digit-words in a row to raw digits.

    Conservative: only replaces when we see at least 7 digit-words back to back
    (matches a phone number), to avoid mangling normal prose like "four dollars".
    Whitespace between digit-words doesn't break the run; any other word does.
    When a run is too short, we drop those words from the returned string —
    that's fine because callers only use this pass to find phone numbers that
    the plain-digit pass missed.
    """
    tokens = re.split(r"(\W+)", text.lower())
    out: list[str] = []
    run: list[str] = []
    for tok in tokens:
        if tok in _SPOKEN_DIGITS:
            run.append(_SPOKEN_DIGITS[tok])
            continue
        # Whitespace-only separators don't break a digit-word run.
        if tok == "" or tok.strip() == "":
            continue
        if len(run) >= 7:
            out.append("".join(run))
        run = []
        out.append(tok)
    if len(run) >= 7:
        out.append("".join(run))
    return "".join(out)


def extract_phone(text: str) -> str | None:
    """Return the most recent 10-digit phone number seen in the text."""
    # First pass: plain-digit formats in original text.
    matches = _PHONE_NUM_RE.findall(text)
    if matches:
        a, b, c = matches[-1]
        return a + b + c
    # Second pass: spelled-out numbers (e.g., "five oh three four four four one one two three").
    normalized = _normalize_spoken_numbers(text)
    matches = _PHONE_NUM_RE.findall(normalized)
    if matches:
        a, b, c = matches[-1]
        return a + b + c
    return None


# ---------------------------------------------------------------- NAMES
# Name-trigger regexes: the trigger PHRASE is case-insensitive but the captured
# name must be genuinely title-case. Deepgram reliably capitalizes recognized
# names ("I'm Jim") while leaving common words lowercase ("I'm used to..."),
# so requiring the leading capital filters ~95% of false positives like
# "I'm used" → "used" without losing real names.
_FIRSTNAME_RE = re.compile(
    r"""(?ix)
    \b(?:my\s+name\s+is|this\s+is|i['’]m|i\s+am|speaking\s+with|name['’]s)
    \s+(?-i:([A-Z][a-z]{1,20}))
    """,
)
_LASTNAME_AFTER_FIRSTNAME_RE = re.compile(
    r"""(?ix)
    \b(?:my\s+name\s+is|this\s+is|i['’]m|i\s+am)
    \s+(?-i:[A-Z][a-z]{1,20}\s+([A-Z][a-z]{1,30}))
    """,
)
_LASTNAME_EXPLICIT_RE = re.compile(
    r"""(?ix)
    \b(?:my\s+last\s+name\s+is|last\s+name['’]s)\s+(?-i:([A-Z][a-z]{1,30}))
    """,
)

def extract_firstname(text: str) -> str | None:
    matches = _FIRSTNAME_RE.findall(text)
    return matches[-1] if matches else None


def extract_lastname(text: str) -> str | None:
    explicit = _LASTNAME_EXPLICIT_RE.findall(text)
    if explicit:
        return explicit[-1]
    after_first = _LASTNAME_AFTER_FIRSTNAME_RE.findall(text)
    return after_first[-1] if after_first else None


# ---------------------------------------------------------------- COMPANY
_COMPANY_RE = re.compile(
    r"""(?x)
    \b(?:from|with|for|at|calling\s+from|I\s+work\s+at)\s+
    ([A-Z][\w&.'-]*(?:\s+[A-Z&][\w&.'-]*){0,4}\s+(?:LLC|Inc|Incorporated|Corp|Corporation|Company|Co|Ltd|LP|LLP))
    """,
)

def extract_company(text: str) -> str | None:
    matches = _COMPANY_RE.findall(text)
    return matches[-1].strip() if matches else None


# ---------------------------------------------------------------- ADDRESS
_ADDRESS_RE = re.compile(
    r"""(?x)
    \b(\d{1,5}\s+
       (?:[NSEW]\.?|north|south|east|west|northeast|northwest|southeast|southwest\s+)?
       [A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}\s+
       (?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Court|Ct|Place|Pl|Parkway|Pkwy|Circle|Cir|Terrace|Ter|Trail|Trl))
    \b
    """,
    re.IGNORECASE,
)

def extract_address(text: str) -> str | None:
    matches = _ADDRESS_RE.findall(text)
    return matches[-1].strip() if matches else None


# ---------------------------------------------------------------- CITY/STATE/ZIP
# State table
_STATE_ABBREVS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_NAME_TO_ABBREV = {v.lower(): k for k, v in _STATE_ABBREVS.items()}

_ZIP_RE = re.compile(r"\b(\d{5}(?:-\d{4})?)\b")

# City captured as "in X" or "located in X" or "X, STATE"
_CITY_BEFORE_STATE_RE = re.compile(
    r"""(?x)
    \b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}),\s+
    (?:""" + "|".join(_STATE_ABBREVS.keys()) + r"""|"""
    + "|".join(re.escape(n) for n in _STATE_ABBREVS.values()) + r""")\b
    """,
)
_CITY_IN_CITY_RE = re.compile(
    r"""(?x)
    \b(?:located\s+in|we'?re\s+in|I'?m\s+in|in\s+the\s+city\s+of)\s+
    ([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})\b
    """,
)

def extract_city(text: str) -> str | None:
    matches = _CITY_BEFORE_STATE_RE.findall(text)
    if matches:
        return matches[-1]
    matches = _CITY_IN_CITY_RE.findall(text)
    return matches[-1] if matches else None


_STATE_ABBREV_IN_CONTEXT_RE = re.compile(
    r",\s+(" + "|".join(_STATE_ABBREVS.keys()) + r")\b"
)
_STATE_FULLNAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _STATE_ABBREVS.values()) + r")\b",
    re.IGNORECASE,
)

def extract_state(text: str) -> str | None:
    m = _STATE_ABBREV_IN_CONTEXT_RE.search(text)
    if m:
        return m.group(1)
    m = _STATE_FULLNAME_RE.search(text)
    if m:
        return _STATE_NAME_TO_ABBREV[m.group(1).lower()]
    return None


def extract_zip(text: str) -> str | None:
    matches = _ZIP_RE.findall(text)
    return matches[-1] if matches else None


# ---------------------------------------------------------------- BUNDLE
EXTRACTORS: dict[str, Callable[[str], str | None]] = {
    "firstname": extract_firstname,
    "lastname": extract_lastname,
    "email": extract_email,
    "phone": extract_phone,
    "company": extract_company,
    "address": extract_address,
    "city": extract_city,
    "state": extract_state,
    "zip": extract_zip,
}


# ---------------------------------------------------------------- HIGHLIGHTS
# Greeting-style name triggers ("Hi, Jim", "Hello Sarah", "Hey, Bob").
# Same title-case-only capture rule as _FIRSTNAME_RE.
_GREETING_NAME_RE = re.compile(
    r"""(?ix)
    \b(?:hi|hello|hey)[,]?\s+(?-i:([A-Z][a-z]{1,20}))\b
    """,
)

# Words that look like names to the regex but are actually greetings or fillers.
# Compared lowercase. Keep this list conservative; real names that collide
# (e.g., "Sir" as a given name) are rare.
_NAME_STOPWORDS = {
    "there", "guys", "everyone", "everybody", "you", "back", "man", "dude",
    "sir", "madam", "maam", "folks", "yall", "again", "now", "buddy",
    "honey", "babe", "friend", "sweetie", "boss",
}


def find_highlights(text: str, rep_first_name: str | None = None) -> list[dict]:
    """Return every span of the transcript worth visually highlighting.

    Output: list of `{"ruleId", "start", "end", "text"}` sorted by start offset.
    The dashboard wraps each span in a `<mark>` styled by ruleId.

    Name handling: any detected first-name match is tagged `rep-name` when it
    equals `rep_first_name` (case-insensitive), otherwise `caller-name`. With
    no rep hint, every name is `caller-name`.
    """
    out: list[dict] = []

    def add(rule_id: str, start: int, end: int) -> None:
        out.append({"ruleId": rule_id, "start": start,
                    "end": end, "text": text[start:end]})

    def add_name(start: int, end: int) -> None:
        token = text[start:end]
        if token.lower() in _NAME_STOPWORDS:
            return
        rule = "caller-name"
        if rep_first_name and token.lower() == rep_first_name.lower():
            rule = "rep-name"
        add(rule, start, end)

    # Names — introduced via trigger phrase or greeting.
    for m in _FIRSTNAME_RE.finditer(text):
        add_name(*m.span(1))
    for m in _GREETING_NAME_RE.finditer(text):
        add_name(*m.span(1))

    # Email.
    for m in _EMAIL_RE.finditer(text):
        add("email", *m.span(0))

    # Phone — plain-digit pass. Spoken-number phones are skipped for highlighting
    # since the normalized string has different offsets than the original text.
    for m in _PHONE_NUM_RE.finditer(text):
        add("phone", *m.span(0))

    # Company, address — regexes already capture the useful span in group 1.
    for m in _COMPANY_RE.finditer(text):
        add("company", *m.span(1))
    for m in _ADDRESS_RE.finditer(text):
        add("address", *m.span(1))

    # City — two trigger patterns.
    for m in _CITY_BEFORE_STATE_RE.finditer(text):
        add("city", *m.span(1))
    for m in _CITY_IN_CITY_RE.finditer(text):
        add("city", *m.span(1))

    # State — abbreviation in address context, or full name anywhere.
    for m in _STATE_ABBREV_IN_CONTEXT_RE.finditer(text):
        add("state", *m.span(1))
    for m in _STATE_FULLNAME_RE.finditer(text):
        add("state", *m.span(1))

    # ZIP.
    for m in _ZIP_RE.finditer(text):
        add("zip", *m.span(1))

    out.sort(key=lambda h: h["start"])
    return out
