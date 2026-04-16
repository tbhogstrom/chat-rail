import pytest
import respx
import httpx
from src.recording_transcriber import transcribe_recording_url, DEEPGRAM_API_URL


@pytest.fixture
def deepgram_response():
    return {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "Hi this is Doug from SFW Construction. How can I help you?",
                        }
                    ]
                }
            ]
        }
    }


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_recording_url_returns_transcript(deepgram_response):
    respx.post(DEEPGRAM_API_URL).mock(
        return_value=httpx.Response(200, json=deepgram_response)
    )

    result = await transcribe_recording_url("https://media.ringcentral.com/recording/123.wav")

    assert result is not None
    assert "Doug from SFW Construction" in result


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_recording_url_handles_error():
    respx.post(DEEPGRAM_API_URL).mock(
        return_value=httpx.Response(400, text="Bad Request")
    )

    result = await transcribe_recording_url("https://media.ringcentral.com/recording/123.wav")

    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_recording_url_empty_channels_returns_none():
    respx.post(DEEPGRAM_API_URL).mock(
        return_value=httpx.Response(200, json={"results": {"channels": []}})
    )

    result = await transcribe_recording_url("https://media.ringcentral.com/recording/123.wav")

    assert result is None
