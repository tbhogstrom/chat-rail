import logging

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

DEEPGRAM_API_URL = "https://api.deepgram.com/v1/listen"


async def transcribe_recording_url(recording_url: str) -> str | None:
    """Send a recording URL to Deepgram batch API and return the transcript text.

    Returns None if transcription fails.
    """
    headers = {
        "Authorization": f"Token {Config.DEEPGRAM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"url": recording_url}
    params = {
        "model": "nova-3",
        "smart_format": "true",
        "diarize": "true",
        "paragraphs": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                DEEPGRAM_API_URL,
                headers=headers,
                json=payload,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        channels = data.get("results", {}).get("channels", [])
        if not channels:
            logger.warning("No channels in Deepgram response")
            return None

        transcript = channels[0]["alternatives"][0]["transcript"]
        logger.info("Transcribed %d characters", len(transcript))
        return transcript

    except Exception:
        logger.exception("Deepgram transcription failed for %s", recording_url)
        return None
