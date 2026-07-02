from zoneinfo import ZoneInfo
from src.config import Config


def test_metrics_timezone_default_is_valid_iana():
    assert Config.METRICS_TIMEZONE == "America/Los_Angeles"
    ZoneInfo(Config.METRICS_TIMEZONE)  # must not raise


def test_sales_script_claude_url_default():
    from src.config import Config
    assert Config.SALES_SCRIPT_CLAUDE_URL == \
        "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"
