"""Имитации внешних систем: банк / платёжный шлюз, контроль лимитов, FraudCheck, Notification.

Поведение задаётся переменной процесса `scenario`, чтобы на стенде воспроизводить все ветки BPMN.
Идемпотентность и кэширование — в Redis и через уникальные ключи проводок в PostgreSQL.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import redis

from .repository import TRANSIT_ACCOUNT, Payment, PaymentRepository

log = logging.getLogger("external")

PER_OPERATION_LIMIT = Decimal("150000")
DAILY_LIMIT = Decimal("300000")


class ExternalTimeout(Exception):
    pass


class FaultInjector:
    """Сценарий `flaky`: первая попытка операции падает по таймауту, повтор проходит."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def first_attempt_fails(self, scenario: str, operation: str, payment_id: str) -> bool:
        if scenario != "flaky":
            return False
        return self._redis.incr(f"fault:{operation}:{payment_id}") == 1


class Bank:
    """Банк / платёжный шлюз. Каждая операция идемпотентна по ключу и записывается проводкой."""

    def __init__(self, repo: PaymentRepository) -> None:
        self._repo = repo

    def operation_done(self, idempotency_key: str) -> bool:
        return self._repo.ledger_exists(idempotency_key)

    def debit(self, payment: Payment, idempotency_key: str) -> bool:
        return self._repo.post_ledger(payment.payment_id, "DEBIT", idempotency_key,
                                      f"customer:{payment.customer_id}", TRANSIT_ACCOUNT, payment.amount)

    def transfer(self, payment: Payment, idempotency_key: str) -> bool:
        return self._repo.post_ledger(payment.payment_id, "TRANSFER", idempotency_key,
                                      TRANSIT_ACCOUNT, f"merchant:{payment.merchant_id}", payment.amount)

    def refund(self, payment: Payment, idempotency_key: str) -> bool:
        return self._repo.post_ledger(payment.payment_id, "REFUND", idempotency_key,
                                      TRANSIT_ACCOUNT, f"customer:{payment.customer_id}", payment.amount)


class LimitsService:
    """Контроль ограничений: лимит на операцию и суточный лимит клиента, резерв в Redis."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def reserve(self, payment: Payment, scenario: str) -> tuple[bool, str]:
        reservation_key = f"limits:reservation:{payment.payment_id}"
        if self._redis.exists(reservation_key):
            return True, "резерв уже создан"
        if payment.amount > PER_OPERATION_LIMIT:
            return False, f"сумма {payment.amount} превышает лимит на операцию {PER_OPERATION_LIMIT}"
        if scenario == "limit-exceeded":
            return False, f"превышен суточный лимит клиента {DAILY_LIMIT}"
        daily_key = self._daily_key(payment.customer_id)
        used = Decimal(self._redis.incrbyfloat(daily_key, float(payment.amount)))
        self._redis.expire(daily_key, 2 * 24 * 3600)
        if used > DAILY_LIMIT:
            self._redis.incrbyfloat(daily_key, -float(payment.amount))
            return False, f"превышен суточный лимит {DAILY_LIMIT}"
        self._redis.set(reservation_key, str(payment.amount))
        return True, f"зарезервировано {payment.amount}, использовано за сутки {used}"

    def release(self, payment: Payment) -> bool:
        reservation_key = f"limits:reservation:{payment.payment_id}"
        amount = self._redis.getdel(reservation_key)
        if amount is None:
            return False
        self._redis.incrbyfloat(self._daily_key(payment.customer_id), -float(amount))
        return True

    @staticmethod
    def _daily_key(customer_id: str) -> str:
        return f"limits:daily:{customer_id}:{date.today().isoformat()}"


@dataclass(frozen=True)
class FraudVerdict:
    decision: str
    risk_score: float
    reason: str
    cached: bool = False


class FraudCheckService:
    """FraudCheck: внутренние правила + внешние провайдеры (AML, GeoRisk). Результат кэшируется в Redis."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def check(self, payment: Payment, scenario: str) -> FraudVerdict:
        cache_key = f"fraud:verdict:{payment.payment_id}"
        cached = self._redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            return FraudVerdict(**data, cached=True)
        verdict = self._evaluate(payment, scenario)
        self._redis.set(cache_key, json.dumps({"decision": verdict.decision, "risk_score": verdict.risk_score,
                                               "reason": verdict.reason}), ex=3600)
        return verdict

    @staticmethod
    def _evaluate(payment: Payment, scenario: str) -> FraudVerdict:
        if scenario == "fraud-deny":
            return FraudVerdict("DENY", 0.97, "AML: контрагент в санкционном списке")
        if scenario in ("manual-review", "manual-cut-off", "customer-cancel"):
            return FraudVerdict("MANUAL_REVIEW", 0.64, "GeoRisk: вход из новой страны и сумма выше обычной в 6 раз")
        return FraudVerdict("ALLOW", 0.05, "правила и провайдеры не нашли рисков")


class NotificationService:
    """Notification Service. Повторная отправка того же уведомления подавляется по ключу."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def send_once(self, key: str) -> bool:
        return bool(self._redis.set(f"notify:{key}", "1", nx=True, ex=7 * 24 * 3600))
