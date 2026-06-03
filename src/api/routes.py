from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from src.redis_store import CallStore
from src.api.auth import verify_api_key
from src.api.models import ContactLookupReq, ContactProps, DealReq
from src.config import Config
from src.hubspot_client import HubSpotClient, HubSpotError
from src.scope_summarizer import summarize_scope, ScopeSummarizerError

router = APIRouter(prefix="/api/calls", dependencies=[Depends(verify_api_key)])
hubspot_router = APIRouter(prefix="/api/hubspot", dependencies=[Depends(verify_api_key)])

_store: CallStore | None = None


def set_store(store: CallStore):
    global _store
    _store = store


def get_store() -> CallStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


@lru_cache(maxsize=1)
def _hs_client() -> HubSpotClient:
    if not Config.HUBSPOT_PRIVATE_APP_TOKEN:
        raise HTTPException(status_code=503,
                            detail="HUBSPOT_PRIVATE_APP_TOKEN not configured")
    return HubSpotClient(Config.HUBSPOT_PRIVATE_APP_TOKEN)


def _contact_url(contact_id: str) -> str | None:
    pid = Config.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{pid}/record/0-1/{contact_id}" if pid else None


def _deal_url(deal_id: str) -> str | None:
    pid = Config.HUBSPOT_PORTAL_ID
    return f"https://app.hubspot.com/contacts/{pid}/record/0-3/{deal_id}" if pid else None


@router.get("/active")
def get_active_calls():
    store = get_store()
    calls = store.list_active_calls()
    return {"calls": calls, "count": len(calls)}


@router.get("/latest")
def get_latest_call(rep: str | None = None):
    store = get_store()
    if rep:
        call = store.get_rep_current_call(rep)
        if call is None:
            raise HTTPException(status_code=404, detail=f"No calls found for rep {rep}")
        transcript = store.get_transcript(call["sessionId"])
        return {"call": call, "transcript": transcript}
    calls = store.list_active_calls()
    if not calls:
        raise HTTPException(status_code=404, detail="No active calls")
    return {"call": calls[0], "transcript": store.get_transcript(calls[0]["sessionId"])}


@router.get("/{session_id}/context")
def get_call_context(session_id: str):
    store = get_store()
    call = store.get_call(session_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    transcript = store.get_transcript(session_id)
    return {
        "call": call,
        "transcript": transcript,
    }


@router.get("/{session_id}/transcript")
def get_call_transcript(session_id: str):
    store = get_store()
    transcript = store.get_transcript(session_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return {"sessionId": session_id, "transcript": transcript}


@router.get("/{session_id}/extracted")
def get_extracted(session_id: str):
    store = get_store()
    data = store.get_extracted(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="No extraction data for session")
    return data


@router.post("/{session_id}/scope-summary")
async def post_scope_summary(session_id: str):
    store = get_store()
    transcript = store.get_transcript(session_id)
    if not transcript:
        raise HTTPException(status_code=404, detail="No transcript for session")
    try:
        summary = await summarize_scope(transcript)
    except ScopeSummarizerError as e:
        # 504 for timeouts, 500 otherwise
        code = 504 if "timed out" in str(e) else 500
        raise HTTPException(status_code=code, detail=str(e))
    return {"summary": summary}


@hubspot_router.post("/contacts/lookup")
async def post_hubspot_lookup(body: ContactLookupReq):
    if not body.email and not body.phone:
        raise HTTPException(status_code=400,
                            detail="email or phone required")
    try:
        match = await _hs_client().lookup_contact(email=body.email, phone=body.phone)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if match is None:
        raise HTTPException(status_code=404, detail="No matching contact")
    return match


@hubspot_router.post("/contacts")
async def post_hubspot_contact(body: ContactProps):
    props = body.to_hubspot_props()
    if not props:
        raise HTTPException(status_code=400, detail="At least one property required")
    try:
        result = await _hs_client().upsert_contact(props)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"contactId": result["id"], "url": _contact_url(result["id"])}


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
