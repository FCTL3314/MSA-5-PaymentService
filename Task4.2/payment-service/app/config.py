import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    camunda_rest_url: str = os.getenv("CAMUNDA_REST_URL", "http://localhost:8080/v2")
    database_url: str = os.getenv("DATABASE_URL", "postgresql://payments:payments@localhost:5432/payments")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    worker_name: str = os.getenv("WORKER_NAME", "payment-service")
    fraud_check_cut_off: str = os.getenv("FRAUD_CHECK_CUT_OFF", "PT3S")
    manual_review_timeout: str = os.getenv("MANUAL_REVIEW_TIMEOUT", "PT20M")
    job_timeout_ms: int = int(os.getenv("JOB_TIMEOUT_MS", "30000"))
    bpmn_dir: Path = Path(os.getenv("BPMN_DIR", Path(__file__).resolve().parent.parent / "bpmn"))
    process_id: str = "payment-saga"


settings = Settings()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
