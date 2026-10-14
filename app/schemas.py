"""Pydantic structural schemas for Phase 1 skeleton.

Structural shape validation only (required fields, types, enums taken
directly from ARCHITECTURE.md section 5). No business-policy enforcement
here: no refund eligibility, age/amount thresholds, pending-transaction
behavior, partial-refund rules, verification, escalation, or duplicate
logic.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CustomerStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"


class TransactionStatus(str, Enum):
    SETTLED = "settled"
    PENDING = "pending"
    REFUNDED = "refunded"


class CustomerRecord(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    card_last4: str = Field(pattern=r"^\d{4}$")
    status: CustomerStatus
    verified: bool


class TransactionRecord(StrictModel):
    txn_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    amount: float = Field(ge=0)
    date: datetime
    merchant: str = Field(min_length=1)
    status: TransactionStatus


class RefundSuccess(StrictModel):
    ok: Literal[True]
    refund_id: str = Field(min_length=1)


class RefundFailure(StrictModel):
    ok: Literal[False]
    reason: str = Field(min_length=1)


RefundResult = Union[RefundSuccess, RefundFailure]


class EscalationResult(StrictModel):
    ticket_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)


class GetCustomerArgs(StrictModel):
    """Structural arguments for get_customer. No policy logic."""

    email: str = Field(min_length=1)


class GetTransactionArgs(StrictModel):
    """Structural arguments for get_transaction. No policy logic."""

    customer_id: str = Field(min_length=1)
    txn_id: str = Field(min_length=1)


class IssueRefundArgs(StrictModel):
    """Structural arguments for issue_refund.

    Finite-number enforcement is input-shape validation. Whether a given
    amount is eligible is a policy decision made in app/policy.py, not here.
    """

    txn_id: str = Field(min_length=1)
    amount: float = Field(allow_inf_nan=False)


class EscalateArgs(StrictModel):
    """Structural arguments for escalate_to_human. No policy logic."""

    customer_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=2000)
