import hashlib
import hmac
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from app.api.dependencies import PaymentRepositoryDep, SettingsDep
from app.api.schemas.payments import PaymentWebhookRequest, PaymentWebhookResponse
from app.application.payments import apply_payment
from app.domain.payment import PaymentConflictError, PaymentUserNotFoundError

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook", response_model=PaymentWebhookResponse)
async def payment_webhook(
    payload: PaymentWebhookRequest,
    repository: PaymentRepositoryDep,
    settings: SettingsDep,
    webhook_secret: Annotated[str | None, Header(alias="X-Payment-Secret")] = None,
    payment_event_id: Annotated[
        str | None, Header(alias="X-Payment-Event-ID", min_length=1, max_length=128)
    ] = None,
) -> PaymentWebhookResponse:
    expected_secret = settings.payment_webhook_secret.get_secret_value()
    if webhook_secret is None or not hmac.compare_digest(webhook_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing payment webhook secret",
        )

    if payment_event_id and payload.external_id and payment_event_id != payload.external_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment event IDs in the header and body do not match",
        )
    external_id = payment_event_id or payload.external_id
    if external_id is None:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Payment-Event-ID is required in production",
            )
        fingerprint = f"{payload.external_user_id}:{payload.amount}".encode()
        external_id = f"legacy-{hashlib.sha256(fingerprint).hexdigest()}"

    try:
        result = await apply_payment(
            repository,
            external_id=external_id,
            user_id=payload.external_user_id,
            amount=payload.amount,
        )
    except PaymentUserNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from error
    except PaymentConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Payment external_id was already used with different data",
        ) from error

    return PaymentWebhookResponse(status=result.status, balance=result.balance)
