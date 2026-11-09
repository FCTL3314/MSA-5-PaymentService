"""Хранилище Payment Service в PostgreSQL: платежи, журнал переходов, проводки."""

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .state_machine import Status, ensure_allowed

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id           UUID PRIMARY KEY,
    order_id             TEXT NOT NULL UNIQUE,
    customer_id          TEXT NOT NULL,
    merchant_id          TEXT NOT NULL,
    amount               NUMERIC(14, 2) NOT NULL,
    currency             TEXT NOT NULL,
    status               TEXT NOT NULL,
    failure_reason       TEXT,
    process_instance_key TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS payment_events (
    id          BIGSERIAL PRIMARY KEY,
    payment_id  UUID NOT NULL REFERENCES payments (payment_id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    event       TEXT NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              BIGSERIAL PRIMARY KEY,
    payment_id      UUID NOT NULL REFERENCES payments (payment_id),
    operation       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    debit_account   TEXT NOT NULL,
    credit_account  TEXT NOT NULL,
    amount          NUMERIC(14, 2) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_events_payment_idx ON payment_events (payment_id, id);
CREATE INDEX IF NOT EXISTS payments_status_updated_idx ON payments (status, updated_at);
"""

TRANSIT_ACCOUNT = "transit:orchestrpay"


@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    customer_id: str
    merchant_id: str
    amount: Decimal
    currency: str
    status: Status
    failure_reason: str | None
    process_instance_key: str | None

    @property
    def short_id(self) -> str:
        return self.payment_id[:8]


class PaymentRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def init_schema(self, attempts: int = 30) -> None:
        for attempt in range(attempts):
            try:
                with self._connect() as conn:
                    conn.execute(SCHEMA)
                return
            except psycopg.OperationalError:
                if attempt == attempts - 1:
                    raise
                time.sleep(2)

    def create(self, payment_id: str, order_id: str, customer_id: str, merchant_id: str, amount: Decimal,
               currency: str, process_instance_key: str) -> bool:
        """CREATE_PAYMENT. Идемпотентно: повторный вызов для того же платежа ничего не меняет."""
        with self._connect() as conn, conn.transaction():
            inserted = conn.execute(
                """INSERT INTO payments (payment_id, order_id, customer_id, merchant_id, amount, currency, status,
                                         process_instance_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (payment_id) DO NOTHING""",
                (payment_id, order_id, customer_id, merchant_id, amount, currency, Status.CREATED,
                 process_instance_key),
            ).rowcount == 1
            if inserted:
                ensure_allowed(None, Status.CREATED, "PaymentInitiated")
                self._log_event(conn, payment_id, None, Status.CREATED, "PaymentInitiated",
                                {"processInstanceKey": process_instance_key})
            return inserted

    def get(self, payment_id: str) -> Payment | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE payment_id = %s", (payment_id,)).fetchone()
        return self._to_payment(row) if row else None

    def find_by_order(self, order_id: str) -> Payment | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE order_id = %s", (order_id,)).fetchone()
        return self._to_payment(row) if row else None

    def transition(self, payment_id: str, target: Status, event: str, details: dict[str, Any] | None = None,
                   failure_reason: str | None = None) -> Status:
        """Переход по таблице State Machine под блокировкой строки. Возвращает исходное состояние."""
        with self._connect() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM payments WHERE payment_id = %s FOR UPDATE",
                               (payment_id,)).fetchone()
            current = Status(row["status"])
            ensure_allowed(current, target, event)
            conn.execute(
                """UPDATE payments SET status = %s, failure_reason = COALESCE(%s, failure_reason), updated_at = now()
                   WHERE payment_id = %s""",
                (target, failure_reason, payment_id),
            )
            self._log_event(conn, payment_id, current, target, event, details)
            return current

    def transition_if(self, payment_id: str, expected: Status, target: Status, event: str,
                      details: dict[str, Any] | None = None, failure_reason: str | None = None) -> bool:
        """Переход только если платёж всё ещё в состоянии expected (защита от гонки параллельных обработчиков)."""
        with self._connect() as conn, conn.transaction():
            row = conn.execute("SELECT status FROM payments WHERE payment_id = %s FOR UPDATE",
                               (payment_id,)).fetchone()
            if Status(row["status"]) != expected:
                return False
            ensure_allowed(expected, target, event)
            conn.execute(
                """UPDATE payments SET status = %s, failure_reason = COALESCE(%s, failure_reason), updated_at = now()
                   WHERE payment_id = %s""",
                (target, failure_reason, payment_id),
            )
            self._log_event(conn, payment_id, expected, target, event, details)
            return True

    def post_ledger(self, payment_id: str, operation: str, idempotency_key: str, debit_account: str,
                    credit_account: str, amount: Decimal) -> bool:
        """Проводка с ключом идемпотентности. False — проводка с таким ключом уже была."""
        with self._connect() as conn:
            return conn.execute(
                """INSERT INTO ledger_entries (payment_id, operation, idempotency_key, debit_account, credit_account,
                                               amount)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
                (payment_id, operation, idempotency_key, debit_account, credit_account, amount),
            ).rowcount == 1

    def ledger_exists(self, idempotency_key: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM ledger_entries WHERE idempotency_key = %s",
                                (idempotency_key,)).fetchone() is not None

    def history(self, payment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            events = conn.execute(
                """SELECT from_status, to_status, event, details, created_at FROM payment_events
                   WHERE payment_id = %s ORDER BY id""", (payment_id,)).fetchall()
            ledger = conn.execute(
                """SELECT operation, debit_account, credit_account, amount, idempotency_key, created_at
                   FROM ledger_entries WHERE payment_id = %s ORDER BY id""", (payment_id,)).fetchall()
            transit = conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN credit_account = %(t)s THEN amount ELSE 0 END), 0)
                        - COALESCE(SUM(CASE WHEN debit_account = %(t)s THEN amount ELSE 0 END), 0) AS balance
                   FROM ledger_entries WHERE payment_id = %(p)s""", {"t": TRANSIT_ACCOUNT, "p": payment_id}).fetchone()
        return {"events": events, "ledger": ledger, "transitBalance": transit["balance"]}

    @staticmethod
    def _log_event(conn: psycopg.Connection, payment_id: str, current: Status | None, target: Status, event: str,
                   details: dict[str, Any] | None) -> None:
        conn.execute(
            "INSERT INTO payment_events (payment_id, from_status, to_status, event, details) VALUES (%s, %s, %s, %s, %s)",
            (payment_id, current, target, event, json.dumps(details or {}, ensure_ascii=False, default=str)),
        )

    @staticmethod
    def _to_payment(row: dict[str, Any]) -> Payment:
        return Payment(
            payment_id=str(row["payment_id"]),
            order_id=row["order_id"],
            customer_id=row["customer_id"],
            merchant_id=row["merchant_id"],
            amount=row["amount"],
            currency=row["currency"],
            status=Status(row["status"]),
            failure_reason=row["failure_reason"],
            process_instance_key=row["process_instance_key"],
        )
