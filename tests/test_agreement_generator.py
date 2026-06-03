from src.agreement_tool.generator import split_sections, build_user_message
from src.agreement_tool.models import AgreementInput


WELL_FORMED = """HEADER:
ACTION REQUIRED | SFW Construction Service Agreement | Jane Doe | 2026-Jun

DEAL DESCRIPTION:
Repair the active roof leak at 123 Main Street.

SCOPE:
SFW will inspect the affected roof and ceiling area to locate the source of the leak. We will remove damaged materials, confirm the findings, and repair or replace what is needed to stop the water intrusion.

EMAIL:
Thank you for trusting SFW with your home. Thank you for choosing SFW Construction."""


def test_split_well_formed():
    out = split_sections(WELL_FORMED)
    assert out["header"].startswith("ACTION REQUIRED")
    assert out["deal_description"] == "Repair the active roof leak at 123 Main Street."
    assert out["scope"].startswith("SFW will inspect")
    assert out["email"].endswith("Thank you for choosing SFW Construction.")
    assert out["partial"] is False
    assert out["raw"] is None


def test_split_missing_email_is_partial():
    text = WELL_FORMED.rsplit("EMAIL:", 1)[0].rstrip()
    out = split_sections(text)
    assert out["email"] is None
    assert out["partial"] is True
    assert out["raw"] == text


def test_split_trims_whitespace_and_blank_lines():
    out = split_sections(WELL_FORMED)
    # no leading/trailing whitespace survives
    assert out["scope"] == out["scope"].strip()
    assert "\n\nDEAL DESCRIPTION" not in out["header"]


# Real-world output observed from the model: markdown-bold labels, `---`
# dividers between sections, and the literal `HEADER:` label dropped (the
# header line emitted directly).
MARKDOWN_FORMAT = """ACTION REQUIRED | SFW Construction Service Agreement | Test Homeowner | 2026-Jun

---

**DEAL DESCRIPTION:**
SFW Construction will inspect and repair an active roof leak at the residence.

---

**SCOPE:**
This is an active leak and will be treated as a priority. SFW will inspect the roof, locate the source, remove damaged materials, and complete all necessary repairs.

---

**EMAIL:**

Dear Test Homeowner,

We take your situation seriously. Thank you for choosing SFW Construction."""


def test_split_markdown_bold_and_missing_header_label():
    out = split_sections(MARKDOWN_FORMAT)
    assert out["header"] == (
        "ACTION REQUIRED | SFW Construction Service Agreement | Test Homeowner | 2026-Jun"
    )
    assert out["deal_description"].startswith("SFW Construction will inspect")
    assert out["scope"].startswith("This is an active leak")
    assert out["email"].endswith("Thank you for choosing SFW Construction.")
    assert "**" not in out["scope"]
    assert "---" not in out["header"]
    assert out["partial"] is False
    assert out["raw"] is None


def test_split_bold_labels_with_explicit_header():
    text = (
        "**HEADER:**\nACTION REQUIRED | SFW | Bob | 2026-Jun\n\n"
        "**DEAL DESCRIPTION:**\nReplace the water heater.\n\n"
        "**SCOPE:**\nSFW will replace the failed water heater and test the install.\n\n"
        "**EMAIL:**\nThanks Bob. Thank you for choosing SFW Construction."
    )
    out = split_sections(text)
    assert out["header"].startswith("ACTION REQUIRED")
    assert out["deal_description"] == "Replace the water heater."
    assert out["scope"].startswith("SFW will replace")
    assert out["partial"] is False


def test_build_user_message_includes_all_fields():
    inp = AgreementInput(
        customer_name="Jane Doe",
        issue_type="roof leak",
        active_leak=True,
        delivery_method="text",
        notes="ceiling stain spreading over the kitchen",
    )
    msg = build_user_message(inp, "2026-Jun")
    assert "Today's date: 2026-Jun" in msg
    assert "Customer name: Jane Doe" in msg
    assert "Issue type: roof leak" in msg
    assert "Active leak: yes" in msg
    assert "Delivery method: text" in msg
    assert "Notes / scope details: ceiling stain spreading over the kitchen" in msg


def test_build_user_message_active_leak_false_and_blank_issue():
    inp = AgreementInput(customer_name="Bob", notes="repaint trim")
    msg = build_user_message(inp, "2026-Jun")
    assert "Active leak: no" in msg
    assert "Issue type: \n" in msg  # empty issue renders blank
    assert "Delivery method: email" in msg


import pytest
import respx
import httpx
from unittest.mock import patch

from src.agreement_tool.generator import generate_package, AgreementError

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


@pytest.mark.asyncio
@respx.mock
async def test_generate_package_returns_sections():
    route = respx.post(_ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": WELL_FORMED}],
        })
    )
    inp = AgreementInput(customer_name="Jane Doe", notes="roof leak in kitchen", active_leak=True)
    with patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test"), \
         patch("src.agreement_tool.generator.Config.ANTHROPIC_MODEL", "claude-sonnet-4-6"):
        out = await generate_package(inp, today="2026-Jun")
    assert out["partial"] is False
    assert out["scope"].startswith("SFW will inspect")
    # request shape
    body = route.calls.last.request.content.decode()
    assert '"model": "claude-sonnet-4-6"' in body
    assert '"max_tokens": 1500' in body
    assert "Active leak: yes" in body
    assert route.calls.last.request.headers["anthropic-version"] == "2023-06-01"
    assert route.calls.last.request.headers["x-api-key"] == "sk-test"


@pytest.mark.asyncio
@respx.mock
async def test_generate_package_raises_on_http_error():
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    inp = AgreementInput(customer_name="X", notes="y")
    with patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test"):
        with pytest.raises(AgreementError, match="401"):
            await generate_package(inp, today="2026-Jun")


@pytest.mark.asyncio
async def test_generate_package_requires_key():
    inp = AgreementInput(customer_name="X", notes="y")
    with patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", ""):
        with pytest.raises(AgreementError, match="ANTHROPIC_API_KEY"):
            await generate_package(inp, today="2026-Jun")
