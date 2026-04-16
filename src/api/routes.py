from fastapi import APIRouter, Depends, HTTPException
from src.redis_store import CallStore
from src.api.auth import verify_api_key

router = APIRouter(prefix="/api/calls", dependencies=[Depends(verify_api_key)])

_store: CallStore | None = None


def set_store(store: CallStore):
    global _store
    _store = store


def get_store() -> CallStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


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
