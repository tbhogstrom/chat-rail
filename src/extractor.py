"""Deterministic regex extractors for sales-call transcripts.

Each extractor takes a transcript string and returns the most recent match
(or None). Full transcript is re-scanned per call — O(N) per extractor is
acceptable since transcripts stay under ~10KB.
"""
import re
from typing import Callable

from src.spoken import tokenize_with_spans, normalize_for_email, normalize_for_phone

# ---------------------------------------------------------------- EMAIL
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
    # familiar-address nicknames
    "bubba", "bro", "brother", "chief", "pal", "champ", "captain", "mister", "doc",
    # weekdays / months (Deepgram title-cases these; greeting regex grabs them)
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


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

    The dashboard wraps each span in a `<mark>` styled by ruleId. `value` is the
    canonical value a click drops into `field`; `field` is the extracted-dict key
    to populate (None for rep-name, which must never fill the caller field).

    Name handling: any detected first-name match is tagged `rep-name` when it
    equals `rep_first_name` (case-insensitive), otherwise `caller-name`. With
    no rep hint, every name is `caller-name`.
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

    # Names — introduced via trigger phrase or greeting.
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

    # State — abbreviation in address context, or full name (value -> abbrev).
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
