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
