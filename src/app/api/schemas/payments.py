from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field

from app.domain.payment import PaymentStatus


class PaymentWebhookRequest(BaseModel):
    external_user_id: UUID = Field(
        validation_alias=AliasChoices("external_user_id", "user_id")
    )
    amount: Annotated[int, Field(gt=0, le=1_000_000_000)]
    external_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class PaymentWebhookResponse(BaseModel):
    status: PaymentStatus
    balance: int
