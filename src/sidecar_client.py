import logging
import httpx

logger = logging.getLogger(__name__)


class SidecarClient:
    """Thin async client for the softphone-bridge sidecar."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._headers = {"x-bridge-key": api_key, "content-type": "application/json"}
        self._timeout = timeout

    async def start_supervision(self, session_id: str, agent_ext_number: str) -> None:
        url = f"{self._base}/sessions"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(url, headers=self._headers,
                             json={"sessionId": session_id,
                                   "agentExtNumber": agent_ext_number})
            if r.status_code not in (202, 409):
                logger.warning("bridge start_supervision %s -> %s %s",
                               session_id, r.status_code, r.text)

    async def stop_supervision(self, session_id: str) -> None:
        url = f"{self._base}/sessions/{session_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.delete(url, headers=self._headers)
            if r.status_code not in (204, 404):
                logger.warning("bridge stop_supervision %s -> %s %s",
                               session_id, r.status_code, r.text)
