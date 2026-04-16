"""Entrypoint for the persistent call monitor process."""
import asyncio
import logging

from ringcentral import SDK
from upstash_redis import Redis

from src.call_monitor import run_monitor
from src.config import Config
from src.recording_transcriber import transcribe_recording_url
from src.redis_store import CallStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def poll_for_recordings(store: CallStore, interval: int = 30):
    """Periodically check completed calls for recordings and transcribe them."""
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)

    while True:
        await asyncio.sleep(interval)
        try:
            resp = platform.get("/restapi/v1.0/account/~/call-log", {
                "perPage": 10,
                "view": "Detailed",
            })
            records = resp.json_dict().get("records", [])
            for call in records:
                recording = call.get("recording")
                if not recording:
                    continue
                session_id = call.get("sessionId")
                if not session_id:
                    continue
                if store.get_transcript(session_id):
                    continue
                content_uri = recording.get("contentUri")
                if not content_uri:
                    continue

                logger.info("Transcribing recording for session %s", session_id)
                transcript = await transcribe_recording_url(content_uri)
                if transcript:
                    store.store_transcript(session_id, transcript)
                    logger.info("Stored transcript for session %s (%d chars)",
                                session_id, len(transcript))
        except Exception:
            logger.exception("Error in recording poll")


async def main():
    redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
    store = CallStore(redis)

    tasks = [run_monitor(store)]
    if Config.DEEPGRAM_API_KEY:
        tasks.append(poll_for_recordings(store))
    else:
        logger.warning("DEEPGRAM_API_KEY not set — post-call transcription disabled")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
