"""State machine платежа: таблица переходов из Task2. Любой переход вне таблицы отклоняется."""

from enum import StrEnum


class Status(StrEnum):
    CREATED = "CREATED"
    LIMITS_RESERVED = "LIMITS_RESERVED"
    DEBIT_PENDING = "DEBIT_PENDING"
    DEBITED = "DEBITED"
    FRAUD_CHECK_PENDING = "FRAUD_CHECK_PENDING"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    APPROVED = "APPROVED"
    TRANSFER_PENDING = "TRANSFER_PENDING"
    TRANSFERRED = "TRANSFERRED"
    COMPLETED = "COMPLETED"
    COMPENSATING = "COMPENSATING"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


S = Status

# (исходное состояние, переходное состояние, событие) — один в один с таблицей переходов Task2.
TRANSITIONS: frozenset[tuple[Status | None, Status, str]] = frozenset({
    (None, S.CREATED, "PaymentInitiated"),
    (S.CREATED, S.LIMITS_RESERVED, "LimitsReserved"),
    (S.CREATED, S.COMPENSATING, "LimitExceeded"),
    (S.LIMITS_RESERVED, S.DEBIT_PENDING, "DebitRequested"),
    (S.DEBIT_PENDING, S.DEBIT_PENDING, "DebitTimeout"),
    (S.DEBIT_PENDING, S.DEBITED, "FundsDebited"),
    (S.DEBIT_PENDING, S.COMPENSATING, "DebitDeclined"),
    (S.DEBITED, S.FRAUD_CHECK_PENDING, "FraudCheckRequested"),
    (S.FRAUD_CHECK_PENDING, S.APPROVED, "FraudCheckAllowed"),
    (S.FRAUD_CHECK_PENDING, S.COMPENSATING, "FraudCheckDenied"),
    (S.FRAUD_CHECK_PENDING, S.MANUAL_REVIEW, "ManualReviewRequired"),
    (S.FRAUD_CHECK_PENDING, S.APPROVED, "FraudCheckCutOffExpired"),
    (S.MANUAL_REVIEW, S.APPROVED, "OperatorApproved"),
    (S.MANUAL_REVIEW, S.COMPENSATING, "OperatorDenied"),
    (S.MANUAL_REVIEW, S.APPROVED, "ManualReviewCutOffExpired"),
    (S.MANUAL_REVIEW, S.COMPENSATING, "CustomerCancelled"),
    (S.APPROVED, S.TRANSFER_PENDING, "TransferRequested"),
    (S.TRANSFER_PENDING, S.TRANSFER_PENDING, "TransferTimeout"),
    (S.TRANSFER_PENDING, S.TRANSFERRED, "TransferConfirmed"),
    (S.TRANSFER_PENDING, S.COMPENSATING, "TransferRejected"),
    (S.TRANSFERRED, S.TRANSFERRED, "CompletionFailed"),
    (S.TRANSFERRED, S.COMPLETED, "PaymentCompleted"),
    (S.COMPENSATING, S.COMPENSATING, "RefundFailed"),
    (S.COMPENSATING, S.REJECTED, "CompensationCompleted"),
    (S.COMPENSATING, S.BLOCKED, "CompensationCompleted"),
    (S.COMPENSATING, S.CANCELLED, "CompensationCompleted"),
})

FINAL_STATUSES = frozenset({S.COMPLETED, S.REJECTED, S.BLOCKED, S.CANCELLED})

# Причина компенсации (событие входа в COMPENSATING) → финальное состояние.
FINAL_STATUS_BY_REASON: dict[str, Status] = {
    "LimitExceeded": S.REJECTED,
    "DebitDeclined": S.REJECTED,
    "FraudCheckDenied": S.BLOCKED,
    "OperatorDenied": S.BLOCKED,
    "CustomerCancelled": S.CANCELLED,
    "TransferRejected": S.CANCELLED,
}


class IllegalTransition(Exception):
    pass


def ensure_allowed(current: Status | None, target: Status, event: str) -> None:
    if (current, target, event) not in TRANSITIONS:
        raise IllegalTransition(f"переход {current} → {target} по событию {event} запрещён таблицей переходов")
