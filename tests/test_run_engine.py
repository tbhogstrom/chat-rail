import pytest
from unittest.mock import patch

import run_engine
from src.redis_store import CallStore
from src.sidecar_client import SidecarClient


@patch("run_engine.Config")
def test_build_store_requires_upstash(MockConfig):
    MockConfig.REDIS_URL = ""
    MockConfig.REDIS_TOKEN = ""
    with pytest.raises(RuntimeError):
        run_engine.build_store()


@patch("run_engine.Config")
def test_build_store_with_creds_returns_callstore(MockConfig):
    MockConfig.REDIS_URL = "https://example.upstash.io"
    MockConfig.REDIS_TOKEN = "tok"
    assert isinstance(run_engine.build_store(), CallStore)


@patch("run_engine.Config")
def test_build_sidecar_none_when_unconfigured(MockConfig):
    MockConfig.SOFTPHONE_BRIDGE_URL = ""
    MockConfig.SOFTPHONE_BRIDGE_API_KEY = ""
    assert run_engine.build_sidecar() is None


@patch("run_engine.Config")
def test_build_sidecar_configured(MockConfig):
    MockConfig.SOFTPHONE_BRIDGE_URL = "http://sfw-softphone-bridge.internal:8787"
    MockConfig.SOFTPHONE_BRIDGE_API_KEY = "k"
    assert isinstance(run_engine.build_sidecar(), SidecarClient)
