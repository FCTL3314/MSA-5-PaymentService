"""Payment orchestration service: REST API платежей + job workers Saga на Camunda 8."""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .camunda_client import CamundaClient
from .config import settings, setup_logging
from .external import Bank, FaultInjector, FraudCheckService, LimitsService, NotificationService
from .handlers import PaymentSagaHandlers
from .repository import PaymentRepository
from .state_machine import Status
from .worker import JobWorker

setup_logging()
log = logging.getLogger("payment-service")

Scenario = Literal["success", "flaky", "limit-exceeded", "debit-declined", "fraud-deny", "fraud-timeout",
                   "manual-review", "manual-cut-off", "customer-cancel", "transfer-rejected"]

repo = PaymentRepository(settings.database_url)
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
camunda = CamundaClient(settings.camunda_rest_url)
workers: list[JobWorker] = []


def iso_seconds(duration: str) -> float:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", duration)
    if not match:
        raise ValueError(f"неподдерживаемая длительность {duration}")
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def start_workers() -> None:
    handlers = PaymentSagaHandlers(
        repo=repo,
        bank=Bank(repo),
        limits=LimitsService(redis_client),
        fraud=FraudCheckService(redis_client),
        notifications=NotificationService(redis_client),
        faults=FaultInjector(redis_client),
        fraud_check_cut_off_seconds=iso_seconds(settings.fraud_check_cut_off),
    )
    for job_type, handler in handlers.registry().items():
        worker = JobWorker(job_type, handler, settings.camunda_rest_url, settings.worker_name, settings.job_timeout_ms)
        worker.start()
        workers.append(worker)


@asynccontextmanager
async def lifespan(_: FastAPI):
    repo.init_schema()
    topology = camunda.wait_until_ready()
    log.info("Camunda готова: брокеров %s, версия %s", len(topology.get("brokers", [])),
             topology.get("gatewayVersion"))
    resources = [settings.bpmn_dir / "payment-saga.bpmn", settings.bpmn_dir / "manual-review.form"]
    deployment = camunda.deploy(resources)
    for item in deployment["deployments"]:
        if item.get("processDefinition"):
            pd = item["processDefinition"]
            log.info("Задеплоен процесс %s v%s (key %s)", pd["processDefinitionId"], pd["processDefinitionVersion"],
                     pd["processDefinitionKey"])
        if item.get("form"):
            log.info("Задеплоена форма %s v%s", item["form"]["formId"], item["form"]["version"])
    start_workers()
    yield
    for worker in workers:
        worker.stop()


app = FastAPI(title="OrchestrPay Payment Service", version="1.0", lifespan=lifespan)


class PaymentRequest(BaseModel):
    orderId: str = Field(examples=["order-1001"])
    customerId: str = Field(examples=["customer-42"])
    merchantId: str = Field(examples=["merchant-7"])
    amount: Decimal = Field(gt=0, examples=["1500.00"])
    currency: str = "RUB"
    scenario: Scenario = "success"
    manualReviewTimeout: str | None = Field(default=None, examples=["PT20M"],
                                            description="cut-off ручной проверки (ISO 8601), по умолчанию PT20M")


@app.post("/payments", status_code=202)
def create_payment(request: PaymentRequest) -> dict:
    """Инициирует платёж: запускает экземпляр Saga в Camunda. Идемпотентно по orderId."""
    payment_id = str(uuid.uuid4())
    existing = redis_client.set(f"order:{request.orderId}", payment_id, nx=True, get=True)
    if existing:
        return {"paymentId": existing, "duplicate": True}
    manual_timeout = request.manualReviewTimeout or (
        "PT30S" if request.scenario == "manual-cut-off" else settings.manual_review_timeout)
    variables = {
        "paymentId": payment_id,
        "orderId": request.orderId,
        "customerId": request.customerId,
        "merchantId": request.merchantId,
        "amount": float(request.amount),
        "currency": request.currency,
        "scenario": request.scenario,
        "fraudDecision": "NONE",
        "fraudCheckCutOff": settings.fraud_check_cut_off,
        "manualReviewTimeout": manual_timeout,
    }
    instance = camunda.create_process_instance(settings.process_id, variables, business_id=request.orderId)
    log.info("payment=%s запущена Saga: processInstanceKey=%s, заказ %s, сумма %s %s, сценарий %s",
             payment_id[:8], instance["processInstanceKey"], request.orderId, request.amount, request.currency,
             request.scenario)
    return {"paymentId": payment_id, "processInstanceKey": instance["processInstanceKey"]}


@app.post("/payments/{payment_id}/cancel", status_code=202)
def cancel_payment(payment_id: str) -> dict:
    """Отмена клиентом. Возможна, пока платёж ждёт ручной антифрод-проверки (до pivot)."""
    payment = repo.get(payment_id)
    if payment is None:
        raise HTTPException(404, "платёж не найден")
    if payment.status != Status.MANUAL_REVIEW:
        raise HTTPException(409, f"отмена невозможна в статусе {payment.status}")
    correlated = camunda.correlate_message("CustomerCancelled", payment_id, {"failureReason": "CUSTOMER_CANCELLED"})
    if correlated is None:
        raise HTTPException(409, "процесс уже вышел из ожидания ручной проверки")
    log.info("payment=%s клиент отменил платёж, сообщение CustomerCancelled доставлено в процесс %s",
             payment_id[:8], correlated["processInstanceKey"])
    return {"paymentId": payment_id, "processInstanceKey": correlated["processInstanceKey"]}


@app.get("/payments/{payment_id}")
def get_payment(payment_id: str) -> dict:
    """Статус платежа, журнал переходов State Machine и проводки (остаток на транзитном счёте)."""
    payment = repo.get(payment_id)
    if payment is None:
        raise HTTPException(404, "платёж не найден")
    return {**payment.__dict__, **repo.history(payment_id)}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "workers": [w.job_type for w in workers if w.is_alive()]}
