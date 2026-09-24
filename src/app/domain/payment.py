from dataclasses import dataclass
from enum import StrEnum


class PaymentStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class PaymentResult:
    status: PaymentStatus
    balance: int


class PaymentUserNotFoundError(Exception):
    pass


class PaymentConflictError(Exception):
    pass

