"""Pydantic request bodies for the Phase 3 dashboard endpoints."""
from pydantic import BaseModel


class ContactLookupReq(BaseModel):
    email: str | None = None
    phone: str | None = None


class ContactProps(BaseModel):
    firstname: str | None = None
    lastname: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None

    def to_hubspot_props(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class DealReq(BaseModel):
    contactId: str
    dealname: str
    description: str
    stage: str | None = None
    scope: str | None = None
