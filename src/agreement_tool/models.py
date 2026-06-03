"""Request/response models for the agreement tool."""
from pydantic import BaseModel


class AgreementInput(BaseModel):
    customer_name: str
    issue_type: str | None = None
    active_leak: bool = False
    delivery_method: str = "email"  # "email" | "text"
    notes: str


class AgreementPackage(BaseModel):
    header: str | None = None
    deal_description: str | None = None
    scope: str | None = None
    email: str | None = None
    partial: bool = False
    raw: str | None = None  # set only when a section failed to parse


class CallRailReq(BaseModel):
    call_id: str | None = None  # None => list recent calls


class SowSummaryReq(BaseModel):
    notes: str  # transcript or estimator notes to distill into a scope of work
