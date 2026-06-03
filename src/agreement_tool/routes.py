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
