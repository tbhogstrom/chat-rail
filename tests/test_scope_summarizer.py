import asyncio
import json
import pytest
from unittest.mock import patch, AsyncMock

from src.scope_summarizer import summarize_scope, ScopeSummarizerError


@pytest.mark.asyncio
async def test_summarize_happy_path():
    fake_stdout = json.dumps({"result": "Customer wants electrical contractor."}).encode()
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (fake_stdout, b"")
    mock_proc.returncode = 0
    with patch("src.scope_summarizer.asyncio.create_subprocess_exec",
               AsyncMock(return_value=mock_proc)):
        result = await summarize_scope("caller asked about electrical work")
    assert result == "Customer wants electrical contractor."


@pytest.mark.asyncio
async def test_summarize_nonzero_exit_raises():
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"claude: auth failed")
    mock_proc.returncode = 1
    with patch("src.scope_summarizer.asyncio.create_subprocess_exec",
               AsyncMock(return_value=mock_proc)):
        with pytest.raises(ScopeSummarizerError, match="auth failed"):
            await summarize_scope("transcript")


@pytest.mark.asyncio
async def test_summarize_timeout_raises():
    async def never_returns():
        await asyncio.sleep(10)
        return b"", b""

    mock_proc = AsyncMock()
    mock_proc.communicate.side_effect = never_returns
    mock_proc.returncode = 0
    with patch("src.scope_summarizer.asyncio.create_subprocess_exec",
               AsyncMock(return_value=mock_proc)):
        with pytest.raises(ScopeSummarizerError, match="timed out"):
            await summarize_scope("transcript", timeout=0.05)


@pytest.mark.asyncio
async def test_summarize_handles_braces_in_transcript():
    """Transcripts may legitimately contain braces; must not crash format parser."""
    fake_stdout = json.dumps({"result": "fine"}).encode()
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (fake_stdout, b"")
    mock_proc.returncode = 0
    tricky = "customer quoted price at {TBD}, timeline {early Q3}"
    with patch("src.scope_summarizer.asyncio.create_subprocess_exec",
               AsyncMock(return_value=mock_proc)):
        result = await summarize_scope(tricky)
    assert result == "fine"
