"""Тонкий клиент Orchestration Cluster REST API (Camunda 8, /v2)."""

import logging
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("camunda")


class CamundaError(Exception):
    pass


class NotFound(CamundaError):
    """404: job уже не активен (отменён граничным событием cut-off / отмены клиента) или нет подписки на сообщение."""


class CamundaClient:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, connect=5.0))

    def close(self) -> None:
        self._http.close()

    def wait_until_ready(self, attempts: int = 90, delay: float = 2.0) -> dict[str, Any]:
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.get("/topology")
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            log.info("Camunda ещё не готова, попытка %s/%s", attempt, attempts)
            time.sleep(delay)
        raise CamundaError("Camunda недоступна")

    def deploy(self, paths: list[Path]) -> dict[str, Any]:
        files = [("resources", (path.name, path.read_bytes(), "application/octet-stream")) for path in paths]
        return self._request("POST", "/deployments", files=files)

    def create_process_instance(self, process_id: str, variables: dict[str, Any], business_id: str) -> dict[str, Any]:
        body = {"processDefinitionId": process_id, "variables": variables, "businessId": business_id}
        return self._request("POST", "/process-instances", json=body)

    def correlate_message(self, name: str, correlation_key: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        body = {"name": name, "correlationKey": correlation_key, "variables": variables}
        try:
            return self._request("POST", "/messages/correlation", json=body)
        except NotFound:
            return None

    def activate_jobs(self, job_type: str, worker: str, timeout_ms: int, max_jobs: int, request_timeout_ms: int) -> list[dict[str, Any]]:
        body = {
            "type": job_type,
            "worker": worker,
            "timeout": timeout_ms,
            "maxJobsToActivate": max_jobs,
            "requestTimeout": request_timeout_ms,
        }
        return self._request("POST", "/jobs/activation", json=body)["jobs"]

    def complete_job(self, job_key: str, variables: dict[str, Any]) -> None:
        self._request("POST", f"/jobs/{job_key}/completion", json={"variables": variables})

    def fail_job(self, job_key: str, retries: int, error_message: str, retry_backoff_ms: int) -> None:
        body = {"retries": retries, "errorMessage": error_message, "retryBackOff": retry_backoff_ms}
        self._request("POST", f"/jobs/{job_key}/failure", json=body)

    def throw_error(self, job_key: str, error_code: str, error_message: str, variables: dict[str, Any]) -> None:
        body = {"errorCode": error_code, "errorMessage": error_message, "variables": variables}
        self._request("POST", f"/jobs/{job_key}/error", json=body)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self._http.request(method, url, **kwargs)
        if response.status_code == 404:
            raise NotFound(response.text)
        if response.status_code >= 400:
            raise CamundaError(f"{method} {url} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None
