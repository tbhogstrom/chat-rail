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
    r"""Split text into word runs (\w+) and single non-word characters, each
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


# ---------------------------------------------------------------- EMAIL
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


# ---------------------------------------------------------------- PHONE
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
            # Span the number itself — trim trailing/leading whitespace and
            # filler tokens the run absorbed (e.g. "three please" -> not "three ").
            core = [t for t in run_tokens
                    if not t.surface.isspace() and t.surface.lower() not in _PHONE_FILLER]
            segments.append((digits, core[0].start, core[-1].end))
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

        if surf.isspace() or low in _PHONE_FILLER:
            if run_tokens:
                run_tokens.append(tok)          # keep span, add no digits
            else:
                segments.append((surf, tok.start, tok.end))
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
