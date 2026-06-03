"""Thin async wrapper around HubSpot CRM v3 for Contact + Deal upsert.

Uses a Private App access token (Bearer auth). No SDK dependency.
"""
import json as _json
import re

import httpx


class HubSpotError(Exception):
    """Non-2xx from HubSpot. Message includes status code + body snippet."""


_EXT_RE = re.compile(r"(?:ext\.?|x|extension)\s*(\d+)\s*$", re.IGNORECASE)


def normalize_phone(raw) -> str | None:
    """Coerce a phone number into HubSpot's required E.164 format.

    HubSpot's `phone` property validation requires `+15032874764` (optionally
    `... ext 123`). CallRail numbers usually arrive that way already, but typed
    or oddly-formatted values do not. Returns None for input with no digits.
    US 10-digit and 1+10-digit numbers get a `+1` prefix; values already
    starting with `+` keep their digits; anything else is a best-effort `+digits`.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    ext = ""
    m = _EXT_RE.search(s)
    if m:
        ext = f" ext {m.group(1)}"
        s = s[: m.start()]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if s.lstrip().startswith("+"):
        e164 = "+" + digits
    elif len(digits) == 10:
        e164 = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        e164 = "+" + digits
    else:
        e164 = "+" + digits
    return e164 + ext


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

    async def search_contacts(self, query: str, limit: int = 5) -> list[dict]:
        """Free-text search across name, email, phone, and company.

        HubSpot's `query` field matches a single search string against the
        searchable contact properties, so one call covers name + phone + email.
        Returns up to `limit` matches (each a dict with `id` + `properties`).
        """
        body = {
            "query": query,
            "limit": limit,
            "properties": ["firstname", "lastname", "email", "phone", "company",
                           "address", "city", "state", "zip"],
        }
        data = await self._request("POST", "/crm/v3/objects/contacts/search", json=body)
        return data.get("results", [])

    async def upsert_contact(self, props: dict) -> dict:
        """Create if no match on email, else PATCH the existing contact.

        `props` is a dict like {"email": ..., "firstname": ..., ...}. Email is
        the idempotency key. A `phone` value is normalized to E.164 (HubSpot
        rejects other formats); if it has no digits it is dropped.
        """
        if props.get("phone"):
            normalized = normalize_phone(props["phone"])
            props = {**props, "phone": normalized}
            if normalized is None:
                del props["phone"]

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
