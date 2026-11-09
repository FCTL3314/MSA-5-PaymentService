"""Обработчики шагов Saga (job types из payment-saga.bpmn).

Каждый обработчик идемпотентен: при повторной доставке job он смотрит текущее состояние платежа
и не повторяет уже сделанную работу. Переходы состояний идут строго по таблице из Task2.
"""

import logging
import time
from decimal import Decimal
from typing import Any

from .external import Bank, ExternalTimeout, FaultInjector, FraudCheckService, LimitsService, NotificationService
from .repository import Payment, PaymentRepository
from .state_machine import FINAL_STATUS_BY_REASON, FINAL_STATUSES, Status
from .worker import BusinessError, Job, JobResult

log = logging.getLogger("saga")

S = Status
AFTER_DEBIT = {S.DEBITED, S.FRAUD_CHECK_PENDING, S.MANUAL_REVIEW, S.APPROVED, S.TRANSFER_PENDING, S.TRANSFERRED,
               S.COMPLETED}
AFTER_TRANSFER = {S.TRANSFERRED, S.COMPLETED}


class PaymentSagaHandlers:
    def __init__(self, repo: PaymentRepository, bank: Bank, limits: LimitsService, fraud: FraudCheckService,
                 notifications: NotificationService, faults: FaultInjector, fraud_check_cut_off_seconds: float) -> None:
        self._repo = repo
        self._bank = bank
        self._limits = limits
        self._fraud = fraud
        self._notifications = notifications
        self._faults = faults
        self._fraud_cut_off_seconds = fraud_check_cut_off_seconds

    def registry(self) -> dict[str, Any]:
        return {
            "create-payment": self.create_payment,
            "reserve-limits": self.reserve_limits,
            "debit-funds": self.debit_funds,
            "fraud-check": self.fraud_check,
            "transfer-to-merchant": self.transfer_to_merchant,
            "complete-payment": self.complete_payment,
            "notify-customer": self.notify_customer,
            "refund-funds": self.refund_funds,
            "release-limits": self.release_limits,
            "cancel-payment": self.cancel_payment,
            "notify-security": self.notify_security,
        }

    # ------------------------------------------------------------------ основной поток

    def create_payment(self, job: Job) -> dict[str, Any]:
        v = job.variables
        created = self._repo.create(v["paymentId"], v["orderId"], v["customerId"], v["merchantId"],
                                    Decimal(str(v["amount"])), v["currency"], job.process_instance_key)
        self._log(job, "CREATE_PAYMENT", f"платёж на {v['amount']} {v['currency']} по заказу {v['orderId']} "
                  f"{'создан, статус CREATED' if created else 'уже существует (повтор)'}")
        return {}

    def reserve_limits(self, job: Job) -> dict[str, Any]:
        payment = self._payment(job)
        if payment.status == S.COMPENSATING and payment.failure_reason == "LimitExceeded":
            raise BusinessError("LIMIT_EXCEEDED", "лимит превышен (повторная доставка job)")
        if payment.status != S.CREATED:
            return {}
        ok, details = self._limits.reserve(payment, job.scenario)
        if not ok:
            self._repo.transition(payment.payment_id, S.COMPENSATING, "LimitExceeded", {"reason": details},
                                  failure_reason="LimitExceeded")
            self._log(job, "RESERVE_LIMITS", f"отказ: {details}. CREATED → COMPENSATING")
            raise BusinessError("LIMIT_EXCEEDED", details)
        self._repo.transition(payment.payment_id, S.LIMITS_RESERVED, "LimitsReserved", {"details": details})
        self._log(job, "RESERVE_LIMITS", f"{details}. CREATED → LIMITS_RESERVED")
        return {}

    def debit_funds(self, job: Job) -> dict[str, Any]:
        payment = self._payment(job)
        if payment.status == S.COMPENSATING and payment.failure_reason == "DebitDeclined":
            raise BusinessError("DEBIT_DECLINED", "банк отказал (повторная доставка job)")
        if payment.status in AFTER_DEBIT:
            return {}
        key = f"debit:{payment.payment_id}"
        if payment.status == S.LIMITS_RESERVED:
            self._repo.transition(payment.payment_id, S.DEBIT_PENDING, "DebitRequested", {"idempotencyKey": key})
        elif self._bank.operation_done(key):
            self._log(job, "DEBIT_FUNDS", f"статус операции {key} в банке: проведена ранее, повторно не списываем")
            self._repo.transition(payment.payment_id, S.DEBITED, "FundsDebited", {"idempotencyKey": key})
            return {}

        if job.scenario == "debit-declined":
            self._repo.transition(payment.payment_id, S.COMPENSATING, "DebitDeclined",
                                  {"reason": "недостаточно средств"}, failure_reason="DebitDeclined")
            self._log(job, "DEBIT_FUNDS", "банк отказал: недостаточно средств. DEBIT_PENDING → COMPENSATING")
            raise BusinessError("DEBIT_DECLINED", "недостаточно средств на счёте клиента")
        if self._faults.first_attempt_fails(job.scenario, "debit", payment.payment_id):
            self._repo.transition(payment.payment_id, S.DEBIT_PENDING, "DebitTimeout", {"idempotencyKey": key})
            raise ExternalTimeout("банк не ответил за 3 с")

        self._bank.debit(payment, key)
        self._repo.transition(payment.payment_id, S.DEBITED, "FundsDebited", {"idempotencyKey": key})
        self._log(job, "DEBIT_FUNDS", f"списано {payment.amount} {payment.currency}: customer:{payment.customer_id} → "
                  f"транзитный счёт (ключ {key}). DEBIT_PENDING → DEBITED")
        return {}

    def fraud_check(self, job: Job) -> JobResult:
        payment = self._payment(job)
        if payment.status == S.DEBITED:
            self._repo.transition(payment.payment_id, S.FRAUD_CHECK_PENDING, "FraudCheckRequested")
            self._log(job, "FRAUD_CHECK", "запрос в FraudCheck Service. DEBITED → FRAUD_CHECK_PENDING")

        if job.scenario == "fraud-timeout":
            delay = self._fraud_cut_off_seconds + 3
            self._log(job, "FRAUD_CHECK", f"внешний провайдер не отвечает, ответ придёт через {delay:.0f} с "
                      f"(cut-off {self._fraud_cut_off_seconds:.0f} с)")
            time.sleep(delay)

        verdict = self._fraud.check(payment, job.scenario)
        variables = {"fraudDecision": verdict.decision, "fraudDecisionSource": "AUTO",
                     "riskScore": verdict.risk_score, "fraudReason": verdict.reason}
        source = "кэш Redis" if verdict.cached else "правила + провайдеры"
        self._log(job, "FRAUD_CHECK", f"решение {verdict.decision} (risk score {verdict.risk_score}, {source}): "
                  f"{verdict.reason}")

        transition = {
            "ALLOW": (S.APPROVED, "FraudCheckAllowed", None),
            "DENY": (S.COMPENSATING, "FraudCheckDenied", "FraudCheckDenied"),
            "MANUAL_REVIEW": (S.MANUAL_REVIEW, "ManualReviewRequired", None),
        }[verdict.decision]

        def apply_decision() -> None:
            target, event, reason = transition
            if self._repo.transition_if(payment.payment_id, S.FRAUD_CHECK_PENDING, target, event, variables,
                                        failure_reason=reason):
                self._log(job, "FRAUD_CHECK", f"FRAUD_CHECK_PENDING → {target}")

        def late_answer() -> None:
            self._log(job, "FRAUD_CHECK", f"поздний ответ {verdict.decision} пришёл после cut-off: платёж уже "
                      "разрешён по умолчанию; при «Запрете» стартует пост-фактум возврат (вне этой Saga)")

        return JobResult(variables=variables, after_complete=apply_decision, on_rejected=late_answer)

    def transfer_to_merchant(self, job: Job) -> dict[str, Any]:
        payment = self._approve_if_needed(job, self._payment(job))
        if payment.status == S.COMPENSATING and payment.failure_reason == "TransferRejected":
            raise BusinessError("TRANSFER_REJECTED", "перевод отклонён (повторная доставка job)")
        if payment.status in AFTER_TRANSFER:
            return {}
        key = f"transfer:{payment.payment_id}"
        if payment.status == S.APPROVED:
            self._repo.transition(payment.payment_id, S.TRANSFER_PENDING, "TransferRequested", {"idempotencyKey": key})
        elif self._bank.operation_done(key):
            self._repo.transition(payment.payment_id, S.TRANSFERRED, "TransferConfirmed", {"idempotencyKey": key})
            return {}

        if job.scenario == "transfer-rejected":
            self._repo.transition(payment.payment_id, S.COMPENSATING, "TransferRejected",
                                  {"reason": "счёт контрагента закрыт"}, failure_reason="TransferRejected")
            self._log(job, "TRANSFER_TO_MERCHANT", "банк отклонил перевод: счёт контрагента закрыт. "
                      "TRANSFER_PENDING → COMPENSATING (pivot не пройден)")
            raise BusinessError("TRANSFER_REJECTED", "счёт контрагента закрыт")
        if self._faults.first_attempt_fails(job.scenario, "transfer", payment.payment_id):
            self._repo.transition(payment.payment_id, S.TRANSFER_PENDING, "TransferTimeout", {"idempotencyKey": key})
            raise ExternalTimeout("банк не подтвердил перевод за 3 с")

        self._bank.transfer(payment, key)
        self._repo.transition(payment.payment_id, S.TRANSFERRED, "TransferConfirmed", {"idempotencyKey": key})
        self._log(job, "TRANSFER_TO_MERCHANT", f"переведено {payment.amount} {payment.currency} контрагенту "
                  f"merchant:{payment.merchant_id} (ключ {key}). PIVOT пройден: TRANSFER_PENDING → TRANSFERRED")
        return {}

    def complete_payment(self, job: Job) -> dict[str, Any]:
        payment = self._payment(job)
        if self._repo.transition_if(payment.payment_id, S.TRANSFERRED, S.COMPLETED, "PaymentCompleted"):
            self._log(job, "COMPLETE_PAYMENT", "платёж проведён, событие PaymentCompleted отправлено в «Финансы». "
                      "TRANSFERRED → COMPLETED")
        return {}

    def notify_customer(self, job: Job) -> dict[str, Any]:
        payment = self._payment(job)
        template = job.headers.get("template", "PAYMENT_STATUS")
        if self._faults.first_attempt_fails(job.scenario, f"notify-{template}", payment.payment_id):
            raise ExternalTimeout("push-шлюз вернул 503")
        if self._notifications.send_once(f"{payment.payment_id}:{template}"):
            self._log(job, "NOTIFY_CUSTOMER", f"клиенту {payment.customer_id} отправлено уведомление {template} "
                      f"(статус платежа {payment.status})")
        else:
            self._log(job, "NOTIFY_CUSTOMER", f"уведомление {template} уже отправлялось, повтор подавлен")
        return {}

    # ------------------------------------------------------------------ компенсации

    def refund_funds(self, job: Job) -> dict[str, Any]:
        payment = self._enter_compensation(job)
        key = f"refund:{payment.payment_id}"
        if not self._bank.operation_done(f"debit:{payment.payment_id}"):
            self._log(job, "REFUND_FUNDS", "списания не было, возвращать нечего")
            return {}
        if self._bank.refund(payment, key):
            self._log(job, "REFUND_FUNDS", f"возвращено {payment.amount} {payment.currency} клиенту "
                      f"customer:{payment.customer_id} с транзитного счёта (ключ {key})")
        else:
            self._log(job, "REFUND_FUNDS", f"возврат {key} уже проведён, повтор не нужен")
        return {}

    def release_limits(self, job: Job) -> dict[str, Any]:
        payment = self._enter_compensation(job)
        released = self._limits.release(payment)
        self._log(job, "RELEASE_LIMITS", "резерв лимита снят" if released else "резерва нет или уже снят")
        return {}

    def cancel_payment(self, job: Job) -> dict[str, Any]:
        payment = self._enter_compensation(job)
        if payment.status in FINAL_STATUSES:
            return {}
        final = FINAL_STATUS_BY_REASON[payment.failure_reason]
        self._repo.transition(payment.payment_id, final, "CompensationCompleted", {"reason": payment.failure_reason})
        self._log(job, "CANCEL_PAYMENT", f"компенсация завершена, причина {payment.failure_reason}. "
                  f"COMPENSATING → {final}")
        return {"paymentStatus": str(final)}

    def notify_security(self, job: Job) -> dict[str, Any]:
        payment = self._payment(job)
        v = job.variables
        if self._notifications.send_once(f"{payment.payment_id}:SECURITY"):
            self._log(job, "NOTIFY_SECURITY", f"служба безопасности уведомлена: клиент {payment.customer_id}, "
                      f"сумма {payment.amount}, источник решения {v.get('fraudDecisionSource')}, "
                      f"risk score {v.get('riskScore')}, причина «{v.get('fraudReason')}»")
        return {}

    # ------------------------------------------------------------------ вспомогательное

    def _payment(self, job: Job) -> Payment:
        payment = self._repo.get(job.payment_id)
        if payment is None:
            raise RuntimeError(f"платёж {job.payment_id} не найден")
        return payment

    def _approve_if_needed(self, job: Job, payment: Payment) -> Payment:
        """Решение по антифроду, принятое в Camunda (оператор, cut-off), фиксируем перед переводом."""
        source = job.variables.get("fraudDecisionSource")
        event = None
        if payment.status == S.FRAUD_CHECK_PENDING and source == "CUT_OFF":
            event = "FraudCheckCutOffExpired"
        elif payment.status == S.FRAUD_CHECK_PENDING and source == "AUTO":
            event = "FraudCheckAllowed"
        elif payment.status == S.MANUAL_REVIEW:
            event = "OperatorApproved" if source == "OPERATOR" else "ManualReviewCutOffExpired"
        details = {"fraudDecisionSource": source, "operatorComment": job.variables.get("operatorComment")}
        if event and self._repo.transition_if(payment.payment_id, payment.status, S.APPROVED, event, details):
            self._log(job, "TRANSFER_TO_MERCHANT", f"решение по антифроду: {event}. {payment.status} → APPROVED")
        return self._payment(job)

    def _enter_compensation(self, job: Job) -> Payment:
        """Первый компенсирующий шаг фиксирует причину, если отказ пришёл из Camunda (оператор, отмена клиентом)."""
        payment = self._payment(job)
        if payment.status == S.COMPENSATING or payment.status in FINAL_STATUSES:
            return payment
        v = job.variables
        if v.get("failureReason") == "CUSTOMER_CANCELLED":
            event = "CustomerCancelled"
        elif v.get("fraudDecision") == "DENY" and v.get("fraudDecisionSource") == "OPERATOR":
            event = "OperatorDenied"
        else:
            event = "FraudCheckDenied"
        if self._repo.transition_if(payment.payment_id, payment.status, S.COMPENSATING, event,
                                    {"operatorComment": v.get("operatorComment")}, failure_reason=event):
            self._log(job, job.element_id, f"старт компенсации, причина {event}. {payment.status} → COMPENSATING")
        return self._payment(job)

    @staticmethod
    def _log(job: Job, step: str, message: str) -> None:
        log.info("payment=%s job=%s %s: %s", job.payment_id[:8], job.key, step, message)
