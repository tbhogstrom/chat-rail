"""Thin async wrapper around HubSpot CRM v3 for Contact + Deal upsert.

Uses a Private App access token (Bearer auth). No SDK dependency.
"""
import json as _json

import httpx


class HubSpotError(Exception):
    """Non-2xx from HubSpot. Message includes status code + body snippet."""


class HubSpotClient:
    def __init__(self, token: str, base_url: str = "https://api.hubapi.com",
                 timeout: float = 10.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}",
                         "content-type": "application/json"}
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base}{path}"
        # Serialize JSON ourselves with default separators (", ", ": ") so that
        # wire bodies have spaces after colons — matches what json.dumps emits
        # and keeps debug logs readable. httpx's json= kwarg uses compact
        # separators, which strips those spaces.
        if "json" in kwargs:
            body = kwargs.pop("json")
            kwargs["content"] = _json.dumps(body).encode("utf-8")
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.request(method, url, headers=self._headers, **kwargs)
        if r.status_code >= 400:
            raise HubSpotError(f"HubSpot {r.status_code}: {r.text[:400]}")
        return r.json()

    async def lookup_contact(self, email: str | None = None,
                              phone: str | None = None) -> dict | None:
        """Search by email first, then phone. Return the first match or None."""
        if not email and not phone:
            raise ValueError("lookup_contact requires email or phone")

        def _search(prop: str, value: str) -> list[dict]:
            return [{
                "filterGroups": [{
                    "filters": [{"propertyName": prop, "operator": "EQ", "value": value}]
                }],
                "properties": ["firstname", "lastname", "email", "phone", "company"],
                "limit": 1,
            }]

        for prop, val in (("email", email), ("phone", phone)):
            if not val:
                continue
            body = _search(prop, val)[0]
            data = await self._request("POST", "/crm/v3/objects/contacts/search", json=body)
            results = data.get("results", [])
            if results:
                return results[0]
        return None

    async def upsert_contact(self, props: dict) -> dict:
        """Create if no match on email, else PATCH the existing contact.

        `props` is a dict like {"email": ..., "firstname": ..., ...}. Email is
        the idempotency key.
        """
        existing = None
        if "email" in props and props["email"]:
            existing = await self.lookup_contact(email=props["email"])
        elif "phone" in props and props["phone"]:
            existing = await self.lookup_contact(phone=props["phone"])

        if existing:
            return await self._request(
                "PATCH",
                f"/crm/v3/objects/contacts/{existing['id']}",
                json={"properties": props},
            )
        return await self._request(
            "POST",
            "/crm/v3/objects/contacts",
            json={"properties": props},
        )

    async def create_deal(self, contact_id: str, dealname: str,
                           description: str, stage: str | None = None,
                           scope: str | None = None,
                           scope_property: str = "scope_of_work") -> dict:
        """Create a deal associated with the given contact id.

        When stage is None, HubSpot uses the default pipeline's default stage.
        When scope is provided, it is written to `scope_property` (the custom
        Deal property the PandaDoc template tokenizes).
        """
        props = {"dealname": dealname, "description": description}
        if stage is not None:
            props["dealstage"] = stage
        if scope:
            props[scope_property] = scope
        body = {
            "properties": props,
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 3,  # deal -> contact
                }],
            }],
        }
        return await self._request("POST", "/crm/v3/objects/deals", json=body)
