from zoneinfo import ZoneInfo
from src.config import Config


def test_metrics_timezone_default_is_valid_iana():
    assert Config.METRICS_TIMEZONE == "America/Los_Angeles"
    ZoneInfo(Config.METRICS_TIMEZONE)  # must not raise


def test_claude_tools_are_configured():
    tools = Config.CLAUDE_TOOLS
    assert len(tools) >= 7
    assert all("label" in t and "url" in t for t in tools)
    assert all(t["url"].startswith("https://claude.ai/project/") for t in tools)
