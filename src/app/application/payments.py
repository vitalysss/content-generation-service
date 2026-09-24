from typing import Protocol
from uuid import UUID

from app.domain.payment import PaymentResult


class PaymentRepository(Protocol):
    async def apply_credit(
        self, *, external_id: str, user_id: UUID, amount: int
    ) -> PaymentResult: ...


async def apply_payment(
    repository: PaymentRepository, *, external_id: str, user_id: UUID, amount: int
) -> PaymentResult:
    return await repository.apply_credit(
        external_id=external_id,
        user_id=user_id,
        amount=amount,
    )

