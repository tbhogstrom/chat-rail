from datetime import datetime, timedelta, timezone

from src.config import Config
from src.sellometer import (
    advance_timeline,
    compute_score,
    known_event_ids,
    load_config,
)


def _config():
    return {
        "version": 1,
        "checkpoints": [
            {"id": "caller-name", "label": "Name", "points": 10,
             "detect": {"type": "extracted_field", "field": "firstname"}},
            {"id": "email", "label": "Email", "points": 15,
             "detect": {"type": "event", "event": "email-entered"}},
        ],
    }


def test_load_config_real_file_totals_100():
    config = load_config()
    assert config["version"] == 1
    assert sum(cp["points"] for cp in config["checkpoints"]) == 100
    ids = [cp["id"] for cp in config["checkpoints"]]
    assert ids == ["caller-name", "phone", "email", "sales-script", "agreement"]


def test_known_event_ids_only_event_checkpoints():
    assert known_event_ids(_config()) == {"email-entered"}


def test_claude_tools_events_are_known_sellometer_events():
    tool_events = {t["event"] for t in Config.CLAUDE_TOOLS if "event" in t}
    assert tool_events, "expected at least one checkpoint-wired Claude tool"
    assert tool_events <= known_event_ids(load_config())


def test_compute_score_empty_inputs():
    result = compute_score(_config(), {}, {})
    assert result["score"] == 0
    assert result["max"] == 25
    assert [cp["hit"] for cp in result["checkpoints"]] == [False, False]
    assert all(cp["ts"] is None for cp in result["checkpoints"])


def test_compute_score_extracted_field_hit():
    result = compute_score(_config(), {"firstname": "Jim"}, {})
    assert result["score"] == 10
    by_id = {cp["id"]: cp for cp in result["checkpoints"]}
    assert by_id["caller-name"]["hit"] is True
    assert by_id["caller-name"]["ts"] is None  # extracted fields carry no ts


def test_compute_score_event_hit_carries_timestamp():
    result = compute_score(_config(), {}, {"email-entered": "2026-07-03T14:11:00+00:00"})
    by_id = {cp["id"]: cp for cp in result["checkpoints"]}
    assert by_id["email"]["hit"] is True
    assert by_id["email"]["ts"] == "2026-07-03T14:11:00+00:00"
    assert result["score"] == 15


def test_compute_score_all_hit():
    result = compute_score(_config(), {"firstname": "Jim"},
                           {"email-entered": "2026-07-03T14:11:00+00:00"})
    assert result["score"] == result["max"] == 25


def test_compute_score_empty_string_field_is_not_hit():
    result = compute_score(_config(), {"firstname": ""}, {})
    assert result["score"] == 0


def test_compute_score_none_inputs_treated_as_empty():
    result = compute_score(_config(), None, None)
    assert result["score"] == 0


def test_compute_score_unknown_detect_type_skipped():
    config = {"checkpoints": [
        {"id": "phrase", "label": "Phrase", "points": 50,
         "detect": {"type": "phrase", "patterns": ["guarantee"]}},
        {"id": "caller-name", "label": "Name", "points": 10,
         "detect": {"type": "extracted_field", "field": "firstname"}},
    ]}
    result = compute_score(config, {"firstname": "Jim"}, {})
    # Unknown type contributes to neither score nor max, and emits no checkpoint.
    assert result["score"] == 10
    assert result["max"] == 10
    assert [cp["id"] for cp in result["checkpoints"]] == ["caller-name"]


def _t(minutes: float) -> datetime:
    return datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_advance_timeline_first_minute_in_progress_appends_nothing():
    assert advance_timeline([], _t(0), _t(0.5), score=10) == []


def test_advance_timeline_appends_on_minute_boundary():
    assert advance_timeline([], _t(0), _t(1.05), score=10) == [10]


def test_advance_timeline_no_duplicate_within_same_minute():
    tl = advance_timeline([], _t(0), _t(1.05), score=10)
    assert advance_timeline(tl, _t(0), _t(1.9), score=25) == [10]


def test_advance_timeline_backfills_gap_with_current_score():
    # Worker stalled for 3 minutes: missing minutes get the catch-up score.
    assert advance_timeline([10], _t(0), _t(4.2), score=40) == [10, 40, 40, 40]
