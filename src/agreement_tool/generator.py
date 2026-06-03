"""SFW Service Agreement Generator: build the prompt, call Anthropic, split output."""
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from src.config import Config
from src.agreement_tool.models import AgreementInput

_PROMPT_PATH = Path(__file__).parent / "prompts" / "service_agreement.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_SECTION_KEYS = ("header", "deal_description", "scope", "email")
_KEY_BY_LABEL = {
    "HEADER": "header",
    "DEAL DESCRIPTION": "deal_description",
    "SCOPE": "scope",
    "EMAIL": "email",
}

# A line that is *only* a section label, tolerant of how the model formats it:
# optional markdown bold/heading/quote/list prefix, optional `**` wrappers, and an
# optional colon. Matches `HEADER:`, `**DEAL DESCRIPTION:**`, `## SCOPE`, etc.
_LABEL_LINE = re.compile(
    r"(?im)^[ \t>#_-]*\*{0,2}[ \t]*(HEADER|DEAL DESCRIPTION|SCOPE|EMAIL)[ \t]*:?[ \t]*\*{0,2}[ \t]*$"
)
# A markdown horizontal-rule / divider line (e.g. `---`, `***`, `___`).
_HR_LINE = re.compile(r"^[\s*_-]{3,}$")


class AgreementError(Exception):
    """Anthropic call failed, timed out, or returned an unusable response."""


def _clean_section(text: str | None) -> str | None:
    """Strip surrounding whitespace and drop markdown divider lines."""
    if text is None:
        return None
    kept = [ln for ln in text.splitlines() if not _HR_LINE.fullmatch(ln.strip())]
    return "\n".join(kept).strip() or None


def split_sections(text: str) -> dict:
    """Split the model's single text block into the four labelled sections.

    Locates each label line tolerantly (the model tends to bold the labels and
    add `---` dividers, and often drops the literal `HEADER:` line — emitting the
    header content directly). Content for a section runs from the end of its
    label line to the start of the next label. When no `HEADER:` label is found,
    the text preceding the first labelled section is treated as the header.

    Any section that can't be found is None and flips `partial` to True; `raw`
    then carries the full text so the UI can still show something editable.
    """
    out: dict = {k: None for k in _SECTION_KEYS}
    matches = list(_LABEL_LINE.finditer(text))
    if matches:
        for i, m in enumerate(matches):
            key = _KEY_BY_LABEL[m.group(1).upper()]
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            out[key] = _clean_section(text[m.end():end])
        if out["header"] is None:
            out["header"] = _clean_section(text[: matches[0].start()])
    out["partial"] = any(out[k] is None for k in _SECTION_KEYS)
    out["raw"] = text if out["partial"] else None
    return out


def build_user_message(inp: AgreementInput, today: str) -> str:
    """Render the dynamic user message from the form fields."""
    return (
        f"Today's date: {today}\n"
        f"Customer name: {inp.customer_name}\n"
        f"Issue type: {inp.issue_type or ''}\n"
        f"Active leak: {'yes' if inp.active_leak else 'no'}\n"
        f"Delivery method: {inp.delivery_method}\n"
        f"Notes / scope details: {inp.notes}"
    )


async def generate_package(
    inp: AgreementInput,
    *,
    api_key: str | None = None,
    model: str | None = None,
    today: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Call the Anthropic Messages API and split the reply into four sections."""
    api_key = api_key if api_key is not None else Config.ANTHROPIC_API_KEY
    model = model or Config.ANTHROPIC_MODEL
    today = today or datetime.now().strftime("%Y-%b")
    if not api_key:
        raise AgreementError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": model,
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_message(inp, today)}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                content=json.dumps(body).encode("utf-8"),
            )
    except httpx.TimeoutException as e:
        raise AgreementError("generator timed out") from e

    if r.status_code >= 400:
        raise AgreementError(f"Anthropic {r.status_code}: {r.text[:300]}")

    data = r.json()
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    if not text.strip():
        raise AgreementError("Anthropic returned empty content")
    return split_sections(text)
