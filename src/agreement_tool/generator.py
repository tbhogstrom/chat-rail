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

_PATTERNS = {
    "header": r"HEADER:\n([\s\S]*?)(?=\nDEAL DESCRIPTION:|$)",
    "deal_description": r"DEAL DESCRIPTION:\n([\s\S]*?)(?=\nSCOPE:|$)",
    "scope": r"SCOPE:\n([\s\S]*?)(?=\nEMAIL:|$)",
    "email": r"EMAIL:\n([\s\S]*?)$",
}
_SECTION_KEYS = ("header", "deal_description", "scope", "email")


class AgreementError(Exception):
    """Anthropic call failed, timed out, or returned an unusable response."""


def split_sections(text: str) -> dict:
    """Split the model's single text block into the four labelled sections.

    Mirrors the regexes in the design spec (Appendix C). Any section that fails
    to match is None and flips `partial` to True; `raw` then carries the full
    text so the UI can still show something editable.
    """
    out: dict = {}
    for key, pat in _PATTERNS.items():
        m = re.search(pat, text)
        out[key] = m.group(1).strip() if m else None
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
