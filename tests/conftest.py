import pytest
import fakeredis


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance that mimics Upstash Redis interface."""
    return fakeredis.FakeRedis(decode_responses=True)
