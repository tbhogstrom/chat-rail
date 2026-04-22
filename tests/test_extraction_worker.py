import asyncio
import pytest
from unittest.mock import MagicMock

from src.extraction_worker import run_extraction_cycle


def test_run_cycle_writes_extracted_for_each_active_session():
    store = MagicMock()
    store.list_active_sessions.return_value = ["s-1", "s-2"]
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

    # s-2: all None
    args_2 = store.set_extracted.call_args_list[1].args
    assert args_2[0] == "s-2"
    assert all(v is None for v in args_2[1].values())


def test_run_cycle_no_active_sessions_is_noop():
    store = MagicMock()
    store.list_active_sessions.return_value = []
    run_extraction_cycle(store)
    store.set_extracted.assert_not_called()


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
