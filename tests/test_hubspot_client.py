import pytest
import respx
import httpx

from src.hubspot_client import HubSpotClient, HubSpotError


@pytest.mark.asyncio
@respx.mock
async def test_lookup_by_email_returns_match():
    respx.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
        return_value=httpx.Response(200, json={
            "total": 1,
            "results": [{"id": "42", "properties": {"firstname": "Sebastian"}}],
        })
    )
    client = HubSpotClient("pat-test")
    match = await client.lookup_contact(email="seb@core.com")
    assert match == {"id": "42", "properties": {"firstname": "Sebastian"}}


@pytest.mark.asyncio
@respx.mock
async def test_lookup_no_match_returns_none():
    respx.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
        return_value=httpx.Response(200, json={"total": 0, "results": []})
    )
    client = HubSpotClient("pat-test")
    assert await client.lookup_contact(email="nope@nope.com") is None


@pytest.mark.asyncio
@respx.mock
async def test_lookup_requires_email_or_phone():
    client = HubSpotClient("pat-test")
    with pytest.raises(ValueError, match="email or phone"):
        await client.lookup_contact()


@pytest.mark.asyncio
@respx.mock
async def test_upsert_creates_when_not_found():
    # Lookup returns no match
    respx.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
        return_value=httpx.Response(200, json={"total": 0, "results": []})
    )
    # Then create
    create_route = respx.post("https://api.hubapi.com/crm/v3/objects/contacts").mock(
        return_value=httpx.Response(201, json={"id": "101", "properties": {"email": "a@b.com"}})
    )
    client = HubSpotClient("pat-test")
    result = await client.upsert_contact({"email": "a@b.com", "firstname": "A"})
    assert result["id"] == "101"
    assert create_route.called
    sent_body = create_route.calls.last.request.content.decode()
    assert '"email": "a@b.com"' in sent_body


@pytest.mark.asyncio
@respx.mock
async def test_upsert_patches_when_found():
    respx.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
        return_value=httpx.Response(200, json={
            "total": 1, "results": [{"id": "99", "properties": {"email": "a@b.com"}}],
        })
    )
    patch_route = respx.patch("https://api.hubapi.com/crm/v3/objects/contacts/99").mock(
        return_value=httpx.Response(200, json={"id": "99", "properties": {"email": "a@b.com", "firstname": "A"}})
    )
    client = HubSpotClient("pat-test")
    result = await client.upsert_contact({"email": "a@b.com", "firstname": "A"})
    assert result["id"] == "99"
    assert patch_route.called


@pytest.mark.asyncio
@respx.mock
async def test_create_deal_with_association():
    deal_route = respx.post("https://api.hubapi.com/crm/v3/objects/deals").mock(
        return_value=httpx.Response(201, json={"id": "d-7", "properties": {"dealname": "Test"}})
    )
    client = HubSpotClient("pat-test")
    result = await client.create_deal(
        contact_id="42", dealname="Test", description="scope here",
    )
    assert result["id"] == "d-7"
    sent = deal_route.calls.last.request.content.decode()
    assert '"dealname": "Test"' in sent
    assert '"description": "scope here"' in sent
    assert '"associations"' in sent
    assert '"id": "42"' in sent  # contact association


@pytest.mark.asyncio
@respx.mock
async def test_create_deal_writes_scope_property():
    create = respx.post("https://api.hubapi.com/crm/v3/objects/deals").mock(
        return_value=httpx.Response(201, json={"id": "D9"})
    )
    client = HubSpotClient("pat-test")
    result = await client.create_deal(
        contact_id="42", dealname="ACTION REQUIRED | ...",
        description="Repair roof leak.", scope="SFW will inspect...",
        scope_property="scope_of_work",
    )
    assert result["id"] == "D9"
    body = create.calls.last.request.content.decode()
    assert '"scope_of_work": "SFW will inspect..."' in body
    assert '"description": "Repair roof leak."' in body


@pytest.mark.asyncio
@respx.mock
async def test_auth_error_raises_hubspoterror():
    respx.post("https://api.hubapi.com/crm/v3/objects/contacts/search").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )
    client = HubSpotClient("pat-test")
    with pytest.raises(HubSpotError, match="401"):
        await client.lookup_contact(email="x@y.com")
