FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /service

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# BPMN-процесс и форма ручной проверки вшиваются в образ: при старте сервис сам деплоит их в Camunda.
COPY bpmn ./bpmn

RUN useradd --system --uid 1001 service
USER service

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
