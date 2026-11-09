"""Job workers: long polling Camunda, вызов обработчика, complete / BPMN error / fail с retry."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .camunda_client import CamundaClient, NotFound

log = logging.getLogger("worker")


@dataclass(frozen=True)
class Job:
    key: str
    type: str
    element_id: str
    process_instance_key: str
    retries: int
    variables: dict[str, Any]
    headers: dict[str, Any]

    @property
    def payment_id(self) -> str:
        return self.variables["paymentId"]

    @property
    def scenario(self) -> str:
        return self.variables.get("scenario", "success")


@dataclass
class JobResult:
    """Результат обработчика. after_complete выполняется только если Camunda приняла завершение job."""

    variables: dict[str, Any] = field(default_factory=dict)
    after_complete: Callable[[], None] | None = None
    on_rejected: Callable[[], None] | None = None


class BusinessError(Exception):
    """Бизнес-отказ шага. Превращается в BPMN error и ведёт в компенсацию без ретраев."""

    def __init__(self, code: str, message: str, variables: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.variables = variables or {}


Handler = Callable[[Job], JobResult | dict[str, Any] | None]


class JobWorker(threading.Thread):
    def __init__(self, job_type: str, handler: Handler, base_url: str, worker_name: str,
                 job_timeout_ms: int, concurrency: int = 4) -> None:
        super().__init__(name=f"poller:{job_type}", daemon=True)
        self.job_type = job_type
        self._handler = handler
        self._base_url = base_url
        self._worker_name = worker_name
        self._job_timeout_ms = job_timeout_ms
        self._concurrency = concurrency
        self._slots = threading.Semaphore(concurrency)
        self._stop_event = threading.Event()
        self._attempts: dict[str, int] = {}
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=job_type)

    def stop(self) -> None:
        self._stop_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def run(self) -> None:
        client = CamundaClient(self._base_url)
        log.info("Worker '%s' запущен (до %s jobs параллельно)", self.job_type, self._concurrency)
        while not self._stop_event.is_set():
            self._slots.acquire()
            free = 1
            while free < self._concurrency and self._slots.acquire(blocking=False):
                free += 1
            try:
                jobs = client.activate_jobs(self.job_type, self._worker_name, self._job_timeout_ms, free, 5000)
            except Exception as exc:
                log.warning("Не удалось получить jobs '%s': %s", self.job_type, exc)
                jobs = []
                self._stop_event.wait(2)
            for _ in range(free - len(jobs)):
                self._slots.release()
            for raw in jobs:
                self._executor.submit(self._process, raw)

    def _process(self, raw: dict[str, Any]) -> None:
        client = CamundaClient(self._base_url)
        job = Job(
            key=raw["jobKey"],
            type=raw["type"],
            element_id=raw["elementId"],
            process_instance_key=raw["processInstanceKey"],
            retries=raw["retries"],
            variables=raw.get("variables") or {},
            headers=raw.get("customHeaders") or {},
        )
        try:
            self._execute(client, job)
        finally:
            client.close()
            self._slots.release()

    def _execute(self, client: CamundaClient, job: Job) -> None:
        try:
            outcome = self._handler(job)
        except BusinessError as error:
            log.warning("job=%s %s: бизнес-отказ %s (%s) → BPMN error, запуск компенсации",
                        job.key, job.element_id, error.code, error.message)
            self._safe(lambda: client.throw_error(job.key, error.code, error.message, error.variables), job)
            return
        except Exception as error:
            self._fail(client, job, error)
            return

        result = outcome if isinstance(outcome, JobResult) else JobResult(variables=outcome or {})
        try:
            client.complete_job(job.key, result.variables)
        except NotFound:
            log.warning("job=%s %s: job уже не активен (сработало граничное событие), результат отброшен",
                        job.key, job.element_id)
            if result.on_rejected:
                result.on_rejected()
            return
        self._attempts.pop(job.key, None)
        if result.after_complete:
            result.after_complete()

    def _fail(self, client: CamundaClient, job: Job, error: Exception) -> None:
        attempt = self._attempts.get(job.key, 0) + 1
        self._attempts[job.key] = attempt
        retries_left = max(job.retries - 1, 0)
        backoff_ms = min(1000 * 2 ** (attempt - 1), 30000)
        if retries_left:
            log.warning("job=%s %s: технический сбой '%s', повтор через %s мс (осталось попыток: %s)",
                        job.key, job.element_id, error, backoff_ms, retries_left)
        else:
            log.error("job=%s %s: попытки исчерпаны ('%s') → инцидент в Camunda Operate",
                      job.key, job.element_id, error)
        self._safe(lambda: client.fail_job(job.key, retries_left, str(error), backoff_ms), job)

    @staticmethod
    def _safe(call: Callable[[], None], job: Job) -> None:
        try:
            call()
        except NotFound:
            log.warning("job=%s уже не активен", job.key)
        except Exception as exc:
            log.error("job=%s: не удалось отправить результат в Camunda: %s (job вернётся после таймаута)", job.key, exc)
