from zoneinfo import ZoneInfo
from src.config import Config


def test_metrics_timezone_default_is_valid_iana():
    assert Config.METRICS_TIMEZONE == "America/Los_Angeles"
    ZoneInfo(Config.METRICS_TIMEZONE)  # must not raise
