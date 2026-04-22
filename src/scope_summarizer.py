"""Scope-of-work summary via the local `claude` CLI (OAuth-authenticated
Claude Code subscription). Returns a short paragraph suitable for a HubSpot
Deal description field.
"""
import asyncio
import json

SCOPE_PROMPT = """You're summarizing a construction sales call for HubSpot.

Produce a 2-3 sentence scope of work summarizing what the customer wants done,
where the work is, and any timeline or budget constraints they mentioned.
Write it as if it will go directly into a HubSpot Deal description — factual,
no salesy language, no preamble like "Based on the transcript...".

TRANSCRIPT:
{transcript}

SCOPE OF WORK:"""


class ScopeSummarizerError(Exception):
    """The claude CLI failed, timed out, or returned unparseable output."""


async def summarize_scope(transcript: str, timeout: float = 30.0) -> str:
    """Call `claude -p <prompt> --output-format=json` and return the result text."""
    prompt = SCOPE_PROMPT.format(transcript=transcript)
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--output-format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise ScopeSummarizerError(f"claude CLI timed out after {timeout}s") from e

    if proc.returncode != 0:
        raise ScopeSummarizerError(f"claude CLI exit {proc.returncode}: {stderr.decode()[:500]}")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScopeSummarizerError(f"claude CLI returned non-JSON: {stdout[:200]!r}") from e

    text = data.get("result")
    if not isinstance(text, str):
        raise ScopeSummarizerError(f"claude CLI JSON missing 'result': {data}")
    return text.strip()
