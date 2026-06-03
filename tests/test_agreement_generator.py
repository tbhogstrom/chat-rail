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
