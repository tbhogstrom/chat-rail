import asyncio
import pytest
from unittest.mock import MagicMock

from src.extraction_worker import run_extraction_cycle


def test_run_cycle_writes_extracted_for_each_active_session():
    store = MagicMock()
    store.list_active_sessions.return_value = ["s-1", "s-2"]
    store.get_call.return_value = None  # no rep_first_name hint
    store.get_transcript.side_effect = lambda sid: {
        "s-1": "my name is Sebastian, call 503-444-1123",
        "s-2": "",  # empty transcript
    }[sid]

    run_extraction_cycle(store)

    # s-1: non-empty extraction
    assert store.set_extracted.call_count == 2
    args_1 = store.set_extracted.call_args_list[0].args
    assert args_1[0] == "s-1"
    assert args_1[1]["firstname"] == "Sebastian"
    assert args_1[1]["phone"] == "5034441123"

    # s-2: every field null, but highlights is an empty list (not None)
    args_2 = store.set_extracted.call_args_list[1].args
    assert args_2[0] == "s-2"
    fields = {k: v for k, v in args_2[1].items() if k != "highlights"}
    assert all(v is None for v in fields.values())
    assert args_2[1]["highlights"] == []


def test_run_cycle_no_active_sessions_is_noop():
    store = MagicMock()
    store.list_active_sessions.return_value = []
    run_extraction_cycle(store)
    store.set_extracted.assert_not_called()


def test_run_cycle_includes_highlights_with_rep_split():
    """Highlights are computed per-cycle and tagged caller-name vs rep-name
    using the rep_first_name stored on call state."""
    store = MagicMock()
    store.list_active_sessions.return_value = ["s-jim"]
    store.get_call.return_value = {"rep_first_name": "Doug"}
    store.get_transcript.return_value = "Hi, Jim. This is Doug with SFW."

    run_extraction_cycle(store)

    args = store.set_extracted.call_args.args
    hl = args[1]["highlights"]
    by_rule = {(h["ruleId"], h["text"]) for h in hl}
    assert ("caller-name", "Jim") in by_rule
    assert ("rep-name", "Doug") in by_rule


def test_run_cycle_firstname_prefers_caller_over_rep_intro():
    """extract_firstname alone would return 'Doug' from 'This is Doug', but
    the worker promotes the caller-name match from highlights when present."""
    store = MagicMock()
    store.list_active_sessions.return_value = ["s-jim"]
    store.get_call.return_value = {"rep_first_name": "Doug"}
    store.get_transcript.return_value = "Hi, Jim. This is Doug with SFW."

    run_extraction_cycle(store)

    assert store.set_extracted.call_args.args[1]["firstname"] == "Jim"


@pytest.mark.asyncio
async def test_worker_loop_swallows_per_iteration_errors():
    """If one iteration raises, the loop logs and continues."""
    from src.extraction_worker import run_extraction_worker

    store = MagicMock()
    calls = []

    def flaky_list():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []  # second iteration clean

    store.list_active_sessions.side_effect = flaky_list

    task = asyncio.create_task(run_extraction_worker(store, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) >= 2  # loop survived the first exception
