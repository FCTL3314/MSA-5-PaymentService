# Задание 3. Где расположить оркестратор

| Артефакт | Файл |
|---|---|
| ADR | [ADR.md](ADR.md) |
| C2-диаграмма выбранного решения | [orchestrator-c2.puml](orchestrator-c2.puml) → [orchestrator-c2.png](orchestrator-c2.png) |

Решение: оркестрация выносится в **отдельный сервис Payment Orchestrator** (кластер Camunda и stateless job workers). Доменные сервисы (Payment, Limits, FraudCheck, Notification) предоставляют идемпотентные команды и ничего не знают о Saga. Альтернатива «оркестратор внутри Payment Service», плюсы и минусы обоих вариантов и риски описаны в [ADR](ADR.md).
