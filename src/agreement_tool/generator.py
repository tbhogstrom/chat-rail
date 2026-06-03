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
