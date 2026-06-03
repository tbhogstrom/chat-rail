# Scope → HubSpot Agreement Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone `/agreement` web tool where a rep pulls a CallRail transcript, fills a short form, generates a standardized SFW Service Agreement package via one Anthropic call, and creates a HubSpot Contact + Deal (with the SCOPE on the deal) so the PandaDoc can be created from HubSpot's native integration.

**Architecture:** A new `src/agreement_tool/` module mounted into the existing FastAPI app. Stateless, request/response only — no Redis, RingCentral, or Deepgram. The Anthropic/CallRail/HubSpot keys stay server-side; the browser sends the shared `x-api-key`. Reuses and lightly extends `src/hubspot_client.py` and the existing `/api/hubspot/*` endpoints.

**Tech Stack:** Python 3.12+, FastAPI, httpx (direct REST, no SDKs), Pydantic, pytest + respx, vanilla HTML/JS.

**Spec:** `docs/superpowers/specs/2026-06-03-scope-to-agreement-tool-design.md`

---

## File structure

| File | Responsibility |
|---|---|
| `src/config.py` (modify) | New env keys: Anthropic, CallRail, HubSpot portal/scope property |
| `src/agreement_tool/__init__.py` (create) | Package marker |
| `src/agreement_tool/models.py` (create) | `AgreementInput`, `AgreementPackage`, `CallRailReq` Pydantic models |
| `src/agreement_tool/prompts/service_agreement.md` (create) | System prompt, verbatim |
| `src/agreement_tool/generator.py` (create) | Build user message, call Anthropic, split into 4 sections |
| `src/agreement_tool/callrail.py` (create) | CallRail API client |
| `src/agreement_tool/routes.py` (create) | `/api/agreement/generate`, `/api/callrail/transcript` |
| `src/agreement_tool/static/agreement.html` (create) | The single page |
| `src/hubspot_client.py` (modify) | `create_deal` accepts optional `scope` + `scope_property` |
| `src/api/models.py` (modify) | `DealReq` gains optional `scope` |
| `src/api/routes.py` (modify) | Deal endpoint passes scope; contact/deal responses include `url` |
| `src/api/main.py` (modify) | Mount `agreement_router`; serve `GET /agreement` |
| `api/index.py` (modify) | Make Redis store optional so the tool deploys without KV |
| `.env.example` (modify) | Document new env vars |
| `tests/test_agreement_generator.py` (create) | Parser + user-message + Anthropic call tests |
| `tests/test_callrail_client.py` (create) | CallRail client tests |
| `tests/test_agreement_routes.py` (create) | Endpoint integration tests |
| `tests/test_hubspot_client.py` (modify) | New test: deal with scope property |

**Note on the SCOPE deal property:** the deal's SCOPE is written to a configurable property (`HUBSPOT_SCOPE_PROPERTY`, default `scope_of_work`). Before the tool is used in production, a HubSpot admin must create a custom Deal property with that internal name (multi-line text), or set `HUBSPOT_SCOPE_PROPERTY` to whatever property the PandaDoc template already tokenizes. This is a one-time manual HubSpot step, documented in Task 14. If the property does not exist, HubSpot returns a 400 surfaced as a 502 toast.

---

## Task 1: Config additions

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: Add the new config attributes**

Add these inside `class Config:` after the existing `HUBSPOT_PRIVATE_APP_TOKEN` line:

```python
    # Anthropic (Service Agreement Generator)
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # CallRail
    CALLRAIL_API_KEY: str = os.environ.get("CALLRAIL_API_KEY", "")
    CALLRAIL_ACCOUNT_ID: str = os.environ.get("CALLRAIL_ACCOUNT_ID", "")

    # HubSpot extras
    HUBSPOT_PORTAL_ID: str = os.environ.get("HUBSPOT_PORTAL_ID", "")
    HUBSPOT_SCOPE_PROPERTY: str = os.environ.get("HUBSPOT_SCOPE_PROPERTY", "scope_of_work")
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from src.config import Config; print(Config.ANTHROPIC_MODEL, Config.HUBSPOT_SCOPE_PROPERTY)"`
Expected: `claude-sonnet-4-6 scope_of_work`

- [ ] **Step 3: Commit**

```bash
git add src/config.py
git commit -m "feat(config): add Anthropic, CallRail, HubSpot portal/scope config"
```

---

## Task 2: Package skeleton + system prompt

**Files:**
- Create: `src/agreement_tool/__init__.py`
- Create: `src/agreement_tool/prompts/service_agreement.md`

- [ ] **Step 1: Create the package marker**

Create `src/agreement_tool/__init__.py` (empty file):

```python
```

- [ ] **Step 2: Create the system prompt file**

Create `src/agreement_tool/prompts/service_agreement.md` with exactly this content:

```markdown
You are the SFW Construction Service Agreement Generator. You generate complete, standardized SFW Construction Service Agreement packages. You accept plain English notes, text messages, transcripts, and mixed input formats, then output a full SA package automatically.

OUTPUT PACKAGE — always output all sections in this exact order:

HEADER:
ACTION REQUIRED | SFW Construction Service Agreement | [Customer Name] | [YYYY-Mmm]
Rules: current month only in YYYY-Mmm format. Never future months. Never em dashes. Mmm format only (Jan, Feb, Mar, etc.)

DEAL DESCRIPTION:
One clean simple sentence summarizing the work clearly.

SCOPE:
Brief customer-friendly scope of work. One short paragraph, 3-8 sentences. No headings, numbered lists, or bullet points. No process-step detail. Describe what SFW will inspect and what SFW will repair or replace. Clear, confident, professional. No technical jargon. No exaggeration. Include investigation, demo, findings, and repair process. Include customer-specific concerns. Active leaks receive priority language.

EMAIL:
Warm reassuring thank you email. Reinforce customer decision. No signoff. Must end with exactly: Thank you for choosing SFW Construction.
Use active-leak version only when job involves active leak or emergency.

STYLE RULES — absolute, never violated:
No em dashes. Warm professional customer-facing language. No corporate clichés. Never mention AI, analysis, or speculation. Never make customer feel uncertain. Include reassurance when appropriate. Address cost concerns, skepticism, urgency, comfort. Reflect estimator notes accurately. Never output incorrect dates. Never guess at timeline or scheduling. Never make promises SFW cannot guarantee.

ACTIVE LEAK BEHAVIOR:
Triggers: water coming inside, staining, ceiling soft spots, dripping, tenant complaints, skylight leaks, window leaks, roof valley leaks. Automatically classify as active leak. Use active-leak email. Include priority language in scope.

MISSING INFO:
If customer name, location, or damage details are unclear, ask one short clarifying question then continue. Never stop or fail to produce output unless absolutely necessary.

SPECIAL INSTRUCTIONS:
If user says "waive the 4 hour minimum" adjust language accordingly.
If customer is nervous add warm reassurance in scope.
Always respect customer-specific instructions: budget caps, not-to-exceed language, realtor requirements.
If user says "modify," "revise," or "redo" output a complete clean replacement package.
Never invent details beyond what the user provides.
```

- [ ] **Step 3: Commit**

```bash
git add src/agreement_tool/__init__.py src/agreement_tool/prompts/service_agreement.md
git commit -m "feat(agreement): package skeleton + SA generator system prompt"
```

---

## Task 3: Pydantic models

**Files:**
- Create: `src/agreement_tool/models.py`

- [ ] **Step 1: Create the models**

Create `src/agreement_tool/models.py`:

```python
"""Request/response models for the agreement tool."""
from pydantic import BaseModel


class AgreementInput(BaseModel):
    customer_name: str
    issue_type: str | None = None
    active_leak: bool = False
    delivery_method: str = "email"  # "email" | "text"
    notes: str


class AgreementPackage(BaseModel):
    header: str | None = None
    deal_description: str | None = None
    scope: str | None = None
    email: str | None = None
    partial: bool = False
    raw: str | None = None  # set only when a section failed to parse


class CallRailReq(BaseModel):
    call_id: str | None = None  # None => list recent calls
```

- [ ] **Step 2: Verify import**

Run: `python -c "from src.agreement_tool.models import AgreementInput, AgreementPackage, CallRailReq; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/agreement_tool/models.py
git commit -m "feat(agreement): request/response models"
```

---

## Task 4: Section splitter (TDD)

**Files:**
- Create: `src/agreement_tool/generator.py` (parser portion)
- Test: `tests/test_agreement_generator.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agreement_generator.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_agreement_generator.py -v`
Expected: FAIL with `ImportError` / `cannot import name 'split_sections'`

- [ ] **Step 3: Implement the parser**

Create `src/agreement_tool/generator.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_agreement_generator.py -v`
Expected: all 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agreement_tool/generator.py tests/test_agreement_generator.py
git commit -m "feat(agreement): section splitter + user-message builder"
```

---

## Task 5: build_user_message coverage (TDD)

**Files:**
- Test: `tests/test_agreement_generator.py` (add)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_agreement_generator.py`:

```python
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
```

- [ ] **Step 2: Run**

Run: `pytest tests/test_agreement_generator.py -k build_user_message -v`
Expected: PASS (implementation from Task 4 already satisfies these)

- [ ] **Step 3: Commit**

```bash
git add tests/test_agreement_generator.py
git commit -m "test(agreement): user-message builder field coverage"
```

---

## Task 6: Anthropic call (TDD with respx)

**Files:**
- Modify: `src/agreement_tool/generator.py` (add `generate_package`)
- Test: `tests/test_agreement_generator.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agreement_generator.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_agreement_generator.py -k generate_package -v`
Expected: FAIL with `cannot import name 'generate_package'`

- [ ] **Step 3: Implement `generate_package`**

Append to `src/agreement_tool/generator.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_agreement_generator.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agreement_tool/generator.py tests/test_agreement_generator.py
git commit -m "feat(agreement): Anthropic Messages API call for SA package"
```

---

## Task 7: CallRail client (TDD with respx)

**Files:**
- Create: `src/agreement_tool/callrail.py`
- Test: `tests/test_callrail_client.py`

> The exact CallRail field names should be verified against a live call during the manual smoke (Task 15); the parsing is isolated in `_parse_call` so only that function changes if a field path differs. Tests mock the documented shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_callrail_client.py`:

```python
import pytest
import respx
import httpx

from src.agreement_tool.callrail import CallRailClient, CallRailError

ACCOUNT = "ACC123"
BASE = "https://api.callrail.com/v3"


def _client():
    return CallRailClient(api_key="cr-key", account_id=ACCOUNT)


@pytest.mark.asyncio
@respx.mock
async def test_get_call_transcript_maps_fields():
    url = f"{BASE}/a/{ACCOUNT}/calls/CAL1.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "id": "CAL1",
        "customer_name": "Jane Doe",
        "customer_phone_number": "+15035551212",
        "source": "Google Ads",
        "transcription": {"sentences": [
            {"speaker": "Agent", "text": "Thanks for calling SFW."},
            {"speaker": "Customer", "text": "My roof is leaking."},
        ]},
    }))
    out = await _client().get_call_transcript("CAL1")
    assert out["customer_name"] == "Jane Doe"
    assert out["customer_phone_number"] == "+15035551212"
    assert out["source"] == "Google Ads"
    assert "Agent: Thanks for calling SFW." in out["transcript"]
    assert "Customer: My roof is leaking." in out["transcript"]
    # auth header
    assert respx.calls.last.request.headers["authorization"] == "Token token=cr-key"


@pytest.mark.asyncio
@respx.mock
async def test_get_call_transcript_handles_plain_string_transcription():
    url = f"{BASE}/a/{ACCOUNT}/calls/CAL2.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "id": "CAL2", "customer_name": "Bob", "customer_phone_number": "+15035550000",
        "source": None, "transcription": "Plain transcript text.",
    }))
    out = await _client().get_call_transcript("CAL2")
    assert out["transcript"] == "Plain transcript text."


@pytest.mark.asyncio
@respx.mock
async def test_get_recent_calls_returns_summaries():
    url = f"{BASE}/a/{ACCOUNT}/calls.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "calls": [
            {"id": "CAL1", "customer_name": "Jane", "customer_phone_number": "+1503", "source": "Google", "start_time": "2026-06-03T10:00:00Z"},
        ]
    }))
    out = await _client().get_recent_calls(limit=5)
    assert out[0]["id"] == "CAL1"
    assert out[0]["customer_name"] == "Jane"


@pytest.mark.asyncio
@respx.mock
async def test_callrail_error_on_non_2xx():
    url = f"{BASE}/a/{ACCOUNT}/calls/BAD.json"
    respx.get(url).mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(CallRailError, match="404"):
        await _client().get_call_transcript("BAD")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_callrail_client.py -v`
Expected: FAIL with `cannot import name 'CallRailClient'`

- [ ] **Step 3: Implement the client**

Create `src/agreement_tool/callrail.py`:

```python
"""Thin async CallRail v3 client: recent calls + per-call transcript.

Auth: `Authorization: Token token=<api_key>`. Account-scoped by account_id.
Transcript field handling is isolated in `_parse_transcript` because CallRail
returns either a structured object (sentences) or a plain string depending on
account configuration.
"""
import httpx


class CallRailError(Exception):
    """Non-2xx from CallRail. Message includes status code + body snippet."""


def _parse_transcript(transcription) -> str:
    """Normalize CallRail's transcription field into a single text block."""
    if not transcription:
        return ""
    if isinstance(transcription, str):
        return transcription
    if isinstance(transcription, dict):
        sentences = transcription.get("sentences") or []
        lines = []
        for s in sentences:
            speaker = s.get("speaker")
            text = s.get("text", "")
            lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines)
    return str(transcription)


class CallRailClient:
    def __init__(self, api_key: str, account_id: str,
                 base_url: str = "https://api.callrail.com/v3", timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._account = account_id
        self._headers = {"Authorization": f"Token token={api_key}"}
        self._timeout = timeout

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._base}/a/{self._account}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(url, headers=self._headers, params=params)
        if r.status_code >= 400:
            raise CallRailError(f"CallRail {r.status_code}: {r.text[:300]}")
        return r.json()

    async def get_call_transcript(self, call_id: str) -> dict:
        data = await self._get(
            f"/calls/{call_id}.json",
            params={"fields": "transcription,customer_name,customer_phone_number,source"},
        )
        return {
            "id": data.get("id"),
            "customer_name": data.get("customer_name"),
            "customer_phone_number": data.get("customer_phone_number"),
            "source": data.get("source"),
            "transcript": _parse_transcript(data.get("transcription")),
        }

    async def get_recent_calls(self, limit: int = 20) -> list[dict]:
        data = await self._get(
            "/calls.json",
            params={"fields": "customer_name,customer_phone_number,source,start_time",
                    "per_page": limit, "sort": "start_time", "order": "desc"},
        )
        return data.get("calls", [])
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_callrail_client.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/agreement_tool/callrail.py tests/test_callrail_client.py
git commit -m "feat(agreement): CallRail v3 client (recent calls + transcript)"
```

---

## Task 8: Extend `create_deal` with scope (TDD)

**Files:**
- Modify: `src/hubspot_client.py`
- Test: `tests/test_hubspot_client.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hubspot_client.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_create_deal_writes_scope_property():
    create = respx.post("https://api.hubapi.com/crm/v3/objects/deals").mock(
        return_value=httpx.Response(201, json={"id": "D9"})
    )
    client = HubSpotClient("pat-test")
    result = await client.create_deal(
        contact_id="42", dealname="ACTION REQUIRED | ...",
        description="Repair roof leak.", scope="SFW will inspect...",
        scope_property="scope_of_work",
    )
    assert result["id"] == "D9"
    body = create.calls.last.request.content.decode()
    assert '"scope_of_work": "SFW will inspect..."' in body
    assert '"description": "Repair roof leak."' in body
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_hubspot_client.py -k scope -v`
Expected: FAIL — `create_deal() got an unexpected keyword argument 'scope'`

- [ ] **Step 3: Update `create_deal`**

In `src/hubspot_client.py`, replace the `create_deal` method signature and props-building block with:

```python
    async def create_deal(self, contact_id: str, dealname: str,
                           description: str, stage: str | None = None,
                           scope: str | None = None,
                           scope_property: str = "scope_of_work") -> dict:
        """Create a deal associated with the given contact id.

        When stage is None, HubSpot uses the default pipeline's default stage.
        When scope is provided, it is written to `scope_property` (the custom
        Deal property the PandaDoc template tokenizes).
        """
        props = {"dealname": dealname, "description": description}
        if stage is not None:
            props["dealstage"] = stage
        if scope:
            props[scope_property] = scope
        body = {
            "properties": props,
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 3,  # deal -> contact
                }],
            }],
        }
        return await self._request("POST", "/crm/v3/objects/deals", json=body)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_hubspot_client.py -v`
Expected: all tests PASS (existing deal tests still pass — new args are optional)

- [ ] **Step 5: Commit**

```bash
git add src/hubspot_client.py tests/test_hubspot_client.py
git commit -m "feat(hubspot): create_deal writes SCOPE to a configurable property"
```

---

## Task 9: Deal request model + route wiring (TDD)

**Files:**
- Modify: `src/api/models.py`
- Modify: `src/api/routes.py`
- Test: `tests/test_api.py` (add)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_api.py`:

```python
import respx
import httpx


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.HUBSPOT_PRIVATE_APP_TOKEN", "pat-test")
@patch("src.api.routes.Config.HUBSPOT_PORTAL_ID", "8210108")
@patch("src.api.routes.Config.HUBSPOT_SCOPE_PROPERTY", "scope_of_work")
@respx.mock
def test_create_deal_passes_scope_and_returns_url(client):
    respx.post("https://api.hubapi.com/crm/v3/objects/deals").mock(
        return_value=httpx.Response(201, json={"id": "D9"})
    )
    # clear the lru_cache so the patched token is used
    from src.api.routes import _hs_client
    _hs_client.cache_clear()
    resp = client.post("/api/hubspot/deals", headers=auth_header(), json={
        "contactId": "42", "dealname": "ACTION", "description": "Repair.",
        "scope": "SFW will inspect.",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["dealId"] == "D9"
    assert data["url"] == "https://app.hubspot.com/contacts/8210108/record/0-3/D9"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_api.py -k scope_and_returns_url -v`
Expected: FAIL (no `url` in response / scope ignored)

- [ ] **Step 3: Extend `DealReq`**

In `src/api/models.py`, update `DealReq`:

```python
class DealReq(BaseModel):
    contactId: str
    dealname: str
    description: str
    stage: str | None = None
    scope: str | None = None
```

- [ ] **Step 4: Add URL helpers + update endpoints**

In `src/api/routes.py`, add these helpers after `_hs_client`:

```python
def _contact_url(contact_id: str) -> str | None:
    pid = Config.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{pid}/record/0-1/{contact_id}" if pid else None


def _deal_url(deal_id: str) -> str | None:
    pid = Config.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{pid}/record/0-3/{deal_id}" if pid else None
```

Replace the body of `post_hubspot_contact` return with:

```python
    return {"contactId": result["id"], "url": _contact_url(result["id"])}
```

Replace `post_hubspot_deal` with:

```python
@hubspot_router.post("/deals")
async def post_hubspot_deal(body: DealReq):
    try:
        result = await _hs_client().create_deal(
            contact_id=body.contactId,
            dealname=body.dealname,
            description=body.description,
            stage=body.stage,
            scope=body.scope,
            scope_property=Config.HUBSPOT_SCOPE_PROPERTY,
        )
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"dealId": result["id"], "url": _deal_url(result["id"])}
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_api.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/api/models.py src/api/routes.py tests/test_api.py
git commit -m "feat(hubspot): deal endpoint forwards scope + returns record URLs"
```

---

## Task 10: Agreement + CallRail endpoints (TDD)

**Files:**
- Create: `src/agreement_tool/routes.py`
- Test: `tests/test_agreement_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agreement_routes.py`:

```python
import pytest
import respx
import httpx
from unittest.mock import patch
from fastapi.testclient import TestClient

from src.api.main import create_app

API_KEY = "test-key-123"


def auth():
    return {"x-api-key": API_KEY}


@pytest.fixture
def client():
    return TestClient(create_app(store=None))


WELL_FORMED = (
    "HEADER:\nACTION REQUIRED | SFW Construction Service Agreement | Jane Doe | 2026-Jun\n\n"
    "DEAL DESCRIPTION:\nRepair the active roof leak.\n\n"
    "SCOPE:\nSFW will inspect the roof and repair the leak.\n\n"
    "EMAIL:\nThank you. Thank you for choosing SFW Construction."
)


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test")
@patch("src.agreement_tool.generator.Config.ANTHROPIC_MODEL", "claude-sonnet-4-6")
@respx.mock
def test_generate_endpoint_returns_sections(client):
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": WELL_FORMED}]})
    )
    resp = client.post("/api/agreement/generate", headers=auth(), json={
        "customer_name": "Jane Doe", "notes": "roof leak", "active_leak": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"].startswith("SFW will inspect")
    assert data["partial"] is False


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test")
@respx.mock
def test_generate_endpoint_502_on_anthropic_error(client):
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(500, text="boom")
    )
    resp = client.post("/api/agreement/generate", headers=auth(), json={
        "customer_name": "X", "notes": "y",
    })
    assert resp.status_code == 502


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_generate_endpoint_requires_auth(client):
    resp = client.post("/api/agreement/generate", json={"customer_name": "X", "notes": "y"})
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.routes.Config.CALLRAIL_API_KEY", "cr-key")
@patch("src.agreement_tool.routes.Config.CALLRAIL_ACCOUNT_ID", "ACC123")
@respx.mock
def test_callrail_transcript_endpoint(client):
    respx.get("https://api.callrail.com/v3/a/ACC123/calls/CAL1.json").mock(
        return_value=httpx.Response(200, json={
            "id": "CAL1", "customer_name": "Jane", "customer_phone_number": "+1503",
            "source": "Google", "transcription": "leaky roof",
        })
    )
    resp = client.post("/api/callrail/transcript", headers=auth(), json={"call_id": "CAL1"})
    assert resp.status_code == 200
    assert resp.json()["transcript"] == "leaky roof"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_agreement_routes.py -v`
Expected: FAIL — `create_app` has no `/api/agreement/generate` route (404) / import error

- [ ] **Step 3: Implement the router**

Create `src/agreement_tool/routes.py`:

```python
"""HTTP endpoints for the agreement tool: generate + CallRail fetch.

Contact/Deal creation reuses the existing /api/hubspot/* endpoints.
"""
from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import verify_api_key
from src.config import Config
from src.agreement_tool.models import AgreementInput, CallRailReq
from src.agreement_tool.generator import generate_package, AgreementError
from src.agreement_tool.callrail import CallRailClient, CallRailError

agreement_router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_key)])


def _callrail_client() -> CallRailClient:
    if not Config.CALLRAIL_API_KEY or not Config.CALLRAIL_ACCOUNT_ID:
        raise HTTPException(status_code=503, detail="CallRail not configured")
    return CallRailClient(Config.CALLRAIL_API_KEY, Config.CALLRAIL_ACCOUNT_ID)


@agreement_router.post("/agreement/generate")
async def post_generate(body: AgreementInput) -> dict:
    try:
        return await generate_package(body)
    except AgreementError as e:
        code = 504 if "timed out" in str(e).lower() else 502
        raise HTTPException(status_code=code, detail=str(e))


@agreement_router.post("/callrail/transcript")
async def post_callrail_transcript(body: CallRailReq) -> dict:
    client = _callrail_client()
    try:
        if body.call_id:
            return await client.get_call_transcript(body.call_id)
        return {"calls": await client.get_recent_calls()}
    except CallRailError as e:
        raise HTTPException(status_code=502, detail=str(e))
```

- [ ] **Step 4: Mount the router (temporary, for the test to pass)**

In `src/api/main.py`, add the import and include the router. Add after the existing `app.include_router(hubspot_router)` line:

```python
    from src.agreement_tool.routes import agreement_router
    app.include_router(agreement_router)
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_agreement_routes.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/agreement_tool/routes.py src/api/main.py tests/test_agreement_routes.py
git commit -m "feat(agreement): /api/agreement/generate + /api/callrail/transcript"
```

---

## Task 11: Serve the page + clean mount

**Files:**
- Modify: `src/api/main.py`

- [ ] **Step 1: Finalize `main.py`**

Replace the entire contents of `src/api/main.py` with:

```python
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from src.redis_store import CallStore
from src.api.routes import router, hubspot_router, set_store
from src.agreement_tool.routes import agreement_router


_STATIC_DIR = Path(__file__).parent / "static"
_AGREEMENT_HTML = Path(__file__).parent.parent / "agreement_tool" / "static" / "agreement.html"


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    app.include_router(hubspot_router)
    app.include_router(agreement_router)

    @app.get("/dashboard")
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "dashboard.html")

    @app.get("/agreement")
    def agreement() -> FileResponse:
        return FileResponse(_AGREEMENT_HTML)

    return app
```

- [ ] **Step 2: Verify existing + new route tests still pass**

Run: `pytest tests/test_api.py tests/test_agreement_routes.py -v`
Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add src/api/main.py
git commit -m "feat(agreement): serve GET /agreement and mount router cleanly"
```

---

## Task 12: The page

**Files:**
- Create: `src/agreement_tool/static/agreement.html`

- [ ] **Step 1: Create the page**

Create `src/agreement_tool/static/agreement.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SFW — Scope to Service Agreement</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#272b34; --fg:#e6e8ec; --muted:#9aa3b2; --accent:#3b82f6; --ok:#22c55e; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line); font-weight:600; }
  main { display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:16px 20px; max-width:1400px; }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
  h2 { margin:0 0 10px; font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  label { display:block; margin:8px 0 3px; color:var(--muted); font-size:12px; }
  input, textarea, select { width:100%; background:#0d0f13; color:var(--fg); border:1px solid var(--line); border-radius:7px; padding:8px; font:inherit; }
  textarea { resize:vertical; }
  .row { display:flex; gap:10px; } .row > * { flex:1; }
  .toggle { display:flex; gap:8px; align-items:center; margin-top:6px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:7px; padding:9px 12px; font:inherit; cursor:pointer; margin-top:10px; }
  button.secondary { background:#2a2f3a; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .out { margin-top:12px; }
  .out label { font-weight:600; color:var(--fg); }
  .link { display:inline-block; margin-top:8px; color:var(--ok); text-decoration:none; }
  #toast { position:fixed; bottom:18px; right:18px; background:#1f2937; border:1px solid var(--line); padding:10px 14px; border-radius:8px; opacity:0; transition:opacity .2s; max-width:420px; }
  #toast.show { opacity:1; }
  .hint { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>SFW — Scope to Service Agreement</header>
<main>
  <!-- LEFT: transcript + CallRail -->
  <section>
    <h2>Transcript</h2>
    <div class="row">
      <input id="callId" placeholder="CallRail call ID" />
      <button class="secondary" id="pull" style="flex:0 0 auto">Pull from CallRail</button>
    </div>
    <label>Transcript / notes (primary input)</label>
    <textarea id="notes" rows="18" placeholder="Paste or pull the call transcript, or type estimator notes…"></textarea>
  </section>

  <!-- RIGHT: form + actions -->
  <section>
    <h2>Customer</h2>
    <label>Customer name</label>
    <input id="name" placeholder="Jane Doe" />
    <div class="row">
      <div><label>Email</label><input id="email" type="email" /></div>
      <div><label>Phone</label><input id="phone" /></div>
    </div>
    <label>Address</label>
    <input id="address" placeholder="123 Main St" />
    <div class="row">
      <div><label>City</label><input id="city" /></div>
      <div><label>State</label><input id="state" /></div>
      <div><label>ZIP</label><input id="zip" /></div>
    </div>
    <div class="row">
      <div><label>Issue type</label><input id="issue" placeholder="roof leak" /></div>
      <div><label>Delivery</label>
        <select id="delivery"><option value="email">Email</option><option value="text">Text</option></select>
      </div>
    </div>
    <div class="toggle"><input type="checkbox" id="leak" style="width:auto" /><label for="leak" style="margin:0">Active leak / emergency</label></div>

    <button id="generate">Generate Service Agreement</button>

    <div class="out">
      <label>HEADER</label><textarea id="o_header" rows="2"></textarea>
      <label>DEAL DESCRIPTION</label><textarea id="o_deal" rows="2"></textarea>
      <label>SCOPE (goes on the HubSpot deal → PandaDoc)</label><textarea id="o_scope" rows="6"></textarea>
      <label>EMAIL (send to customer)</label><textarea id="o_email" rows="6"></textarea>
      <p id="partialNote" class="hint" style="display:none">⚠ Some sections did not parse cleanly — review all boxes.</p>
    </div>

    <div class="row">
      <button id="createContact">1 · Create Contact</button>
      <button id="createDeal" disabled>2 · Create Deal</button>
    </div>
    <a class="link" id="contactLink" target="_blank" style="display:none"></a>
    <a class="link" id="dealLink" target="_blank" style="display:none"></a>
    <p class="hint">After the deal is created, open it in HubSpot and click your PandaDoc service-agreement template — it auto-fills from the deal (including SCOPE).</p>
  </section>
</main>
<div id="toast"></div>

<script>
const KEY_NAME = "sfw-bridge-key";
function apiKey() {
  let k = localStorage.getItem(KEY_NAME);
  if (!k) { k = prompt("Enter API key"); if (k) localStorage.setItem(KEY_NAME, k); }
  return k;
}
function toast(msg, ok=false) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.borderColor = ok ? "var(--ok)" : "var(--line)";
  t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 4000);
}
async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": apiKey() },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail || r.status);
  return data;
}
const $ = id => document.getElementById(id);

$("pull").onclick = async () => {
  const id = $("callId").value.trim();
  if (!id) return toast("Enter a CallRail call ID");
  try {
    const d = await api("/api/callrail/transcript", { call_id: id });
    $("notes").value = d.transcript || "";
    if (d.customer_name) $("name").value = d.customer_name;
    if (d.customer_phone_number) $("phone").value = d.customer_phone_number;
    toast("Pulled from CallRail", true);
  } catch (e) { toast("CallRail: " + e.message); }
};

$("generate").onclick = async () => {
  const btn = $("generate"); btn.disabled = true; btn.textContent = "Generating…";
  try {
    const d = await api("/api/agreement/generate", {
      customer_name: $("name").value.trim(),
      issue_type: $("issue").value.trim(),
      active_leak: $("leak").checked,
      delivery_method: $("delivery").value,
      notes: $("notes").value,
    });
    $("o_header").value = d.header || "";
    $("o_deal").value = d.deal_description || "";
    $("o_scope").value = d.scope || (d.partial ? d.raw : "") || "";
    $("o_email").value = d.email || "";
    $("partialNote").style.display = d.partial ? "block" : "none";
    toast("Agreement generated", true);
  } catch (e) { toast("Generate: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "Generate Service Agreement"; }
};

let contactId = null;
function nameParts() {
  const parts = $("name").value.trim().split(/\s+/);
  return { firstname: parts[0] || "", lastname: parts.slice(1).join(" ") };
}
$("createContact").onclick = async () => {
  const { firstname, lastname } = nameParts();
  try {
    const d = await api("/api/hubspot/contacts", {
      firstname, lastname,
      email: $("email").value.trim() || null,
      phone: $("phone").value.trim() || null,
      address: $("address").value.trim() || null,
      city: $("city").value.trim() || null,
      state: $("state").value.trim() || null,
      zip: $("zip").value.trim() || null,
    });
    contactId = d.contactId;
    $("createDeal").disabled = false;
    if (d.url) { const a = $("contactLink"); a.href = d.url; a.textContent = "↗ Open contact in HubSpot"; a.style.display = "inline-block"; }
    toast("Contact created/updated", true);
  } catch (e) { toast("Contact: " + e.message); }
};
$("createDeal").onclick = async () => {
  if (!contactId) return toast("Create the contact first");
  try {
    const d = await api("/api/hubspot/deals", {
      contactId,
      dealname: $("o_header").value.trim() || ("Service Agreement — " + $("name").value.trim()),
      description: $("o_deal").value.trim(),
      scope: $("o_scope").value.trim(),
    });
    if (d.url) { const a = $("dealLink"); a.href = d.url; a.textContent = "↗ Open deal in HubSpot"; a.style.display = "inline-block"; }
    toast("Deal created", true);
  } catch (e) { toast("Deal: " + e.message); }
};
</script>
</body>
</html>
```

- [ ] **Step 2: Manual visual check**

Run: `uvicorn api.index:app --reload` (or `python run_local.py` if that's the dev entry), open `http://localhost:8000/agreement`.
Expected: page renders with transcript panel, form, output boxes, and buttons; no console errors.

- [ ] **Step 3: Commit**

```bash
git add src/agreement_tool/static/agreement.html
git commit -m "feat(agreement): scope-to-agreement web page"
```

---

## Task 13: Make Vercel entrypoint Redis-optional

**Files:**
- Modify: `api/index.py`

- [ ] **Step 1: Replace the entrypoint**

Replace the entire contents of `api/index.py` with:

```python
"""Vercel serverless entrypoint — exposes the FastAPI app as a handler.

Redis is optional: the agreement tool needs no store. When KV/Upstash env
vars are present, the call-intelligence endpoints get a live store too.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import create_app
from src.config import Config

store = None
if Config.REDIS_URL and Config.REDIS_TOKEN:
    from upstash_redis import Redis
    from src.redis_store import CallStore
    store = CallStore(Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN))

app = create_app(store)
```

- [ ] **Step 2: Verify the app builds without Redis env**

Run: `python -c "import api.index; print([r.path for r in api.index.app.routes if 'agreement' in r.path])"`
Expected: prints `['/agreement', '/api/agreement/generate']` (order may vary; both present)

- [ ] **Step 3: Commit**

```bash
git add api/index.py
git commit -m "fix(vercel): make Redis store optional so the agreement tool deploys standalone"
```

---

## Task 14: Document env + HubSpot property setup

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Append the new env section**

Add to the end of `.env.example`:

```bash

# --- Scope → Agreement tool ---
# Anthropic (SFW Service Agreement Generator). Server-side only.
ANTHROPIC_API_KEY=
# Optional model override. Default in code: claude-sonnet-4-6
ANTHROPIC_MODEL=

# CallRail (transcript + caller info). Token auth.
CALLRAIL_API_KEY=
CALLRAIL_ACCOUNT_ID=

# HubSpot extras
# Portal ID is used only to build clickable record links (e.g. 8210108).
HUBSPOT_PORTAL_ID=
# Deal property the PandaDoc template reads the SCOPE from. Default: scope_of_work.
# A HubSpot admin must create this custom Deal property (multi-line text), OR set
# this to an existing property the PandaDoc template already tokenizes.
HUBSPOT_SCOPE_PROPERTY=scope_of_work
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: document agreement-tool env vars + scope_of_work deal property"
```

---

## Task 15: Full test run + manual smoke

**Files:** none (verification)

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all tests pass (no regressions in the existing call-monitor / dashboard tests).

- [ ] **Step 2: Populate `.env`**

Copy the required tokens from the user's `causal-ai-lab` environment into the project `.env`: `ANTHROPIC_API_KEY`, `CALLRAIL_API_KEY`, `CALLRAIL_ACCOUNT_ID`, `HUBSPOT_PRIVATE_APP_TOKEN`, `HUBSPOT_PORTAL_ID`, `CALL_BRIDGE_API_KEY`. Confirm `scope_of_work` exists as a Deal property in HubSpot (or set `HUBSPOT_SCOPE_PROPERTY`).

- [ ] **Step 3: Manual smoke**

1. Start the app; open `/agreement`, enter the API key when prompted.
2. Type a sample transcript in the notes box ("ceiling leaking over the kitchen, customer is nervous about cost, address 123 Main St"). Fill name/email/phone/address, toggle Active leak.
3. Click **Generate** → confirm four populated boxes; active-leak language appears in SCOPE/EMAIL; HEADER shows the current month in `YYYY-Mmm`; no em dashes.
4. Click **Create Contact** → contact link appears in HubSpot.
5. Click **Create Deal** → deal link appears; open the deal in HubSpot and confirm the SCOPE shows on the `scope_of_work` property.
6. (If a real CallRail call ID is available) verify **Pull from CallRail** fills the notes + name/phone. Adjust `_parse_transcript` field paths in `callrail.py` if the live shape differs.

- [ ] **Step 4: Final commit (if any smoke fixes were needed)**

```bash
git add -A
git commit -m "fix(agreement): smoke-test adjustments"
```

---

## Self-review notes

- **Spec coverage:** page (T12), CallRail fetch (T7/T10), Claude generation + 4-section split (T4/T6), contact upsert (reused) + deal with SCOPE (T8/T9), PandaDoc-via-HubSpot (documented manual step, T12/T14), config (T1/T14), Vercel-standalone (T13), tests (T4–T10). All spec sections map to a task.
- **No new runtime deps:** Anthropic and CallRail both go through the existing `httpx` dependency; `respx` is already a dev dep.
- **Type consistency:** `AgreementInput`/`AgreementPackage`/`CallRailReq` defined in T3 and used unchanged in T6/T10; `split_sections` returns the dict shape the page reads (`header`/`deal_description`/`scope`/`email`/`partial`/`raw`); `create_deal(scope=, scope_property=)` signature in T8 matches the call site in T9.
- **Shared-endpoint safety:** the `/api/hubspot/*` changes are additive (optional `scope`, extra `url` field) so the existing dashboard keeps working.
