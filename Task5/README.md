# Задание 5. Интеграционные и end-to-end тесты

Тест-кейсы проверяют основной flow и corner cases процесса из [Task4.1](../Task4.1) и State Machine из [Task2](../Task2).

## Окружение

| Что | Чем |
|---|---|
| Инфраструктура | Testcontainers: `camunda/camunda:8.9`, PostgreSQL, Redis |
| E2E-тесты | Весь [docker-compose](../Task4.2/docker-compose.yaml) через `ComposeContainer` |
| Внешние системы (банк, антифрод-провайдеры, push-шлюз) | WireMock-контейнер: задержки, 5xx, отказы |
| Тесты процесса | Camunda Process Test: управление временем для таймеров cut-off, проверки пройденных элементов и переменных |

Все 11 E2E-сценариев уже прогнаны вручную на стенде [Task4.2](../Task4.2), результаты — в [logs/payments-final-state.log](../Task4.2/logs/payments-final-state.log).

## Тест-кейсы

| № | Название | Тип | Компоненты | Предусловия | Ожидаемый результат |
|---|---|---|---|---|---|
| 1 | Деплой BPMN-процесса и формы | интеграционный | Camunda, `payment-saga.bpmn`, `manual-review.form` | Пустой кластер Camunda в Testcontainers | Деплой успешен: процесс `payment-saga` v1 и форма `manual-review-form` доступны. Повторный деплой без изменений не создаёт новую версию |
| 2 | Успешный путь процесса | интеграционный | Camunda, mock job workers | Workers отвечают успехом, `fraud-check` → `ALLOW` | Пройдены элементы `CREATE_PAYMENT` … `NOTIFY_CUSTOMER`, экземпляр завершён в «Платёж проведён», компенсации не активировались |
| 3 | Запрет антифрода: компенсация в обратном порядке | интеграционный | Camunda, mock job workers | `fraud-check` → `DENY` | Обработчики вызваны строго в порядке `refund-funds` → `release-limits` → `cancel-payment`. Параллельно один раз вызван `notify-security`. `notify-customer` получил шаблон `PAYMENT_NOT_COMPLETED` |
| 4 | Компенсация только завершённых шагов | интеграционный | Camunda, mock job workers | `debit-funds` бросает BPMN error `DEBIT_DECLINED` | `refund-funds` не активирован (списания не было). `release-limits` и `cancel-payment` выполнены |
| 5 | Cut-off автоматической антифрод-проверки | интеграционный | Camunda (управление временем), mock workers | Worker `fraud-check` не завершает job, `fraudCheckCutOff = PT3S` | Через 3 с job отменён, пройдено «Разрешено по cut-off», `fraudDecisionSource = CUT_OFF`, выполнен `transfer-to-merchant`. Поздний complete от worker получает 404 |
| 6 | Cut-off ручной проверки через 20 минут | интеграционный | Camunda (управление временем), mock workers | `fraud-check` → `MANUAL_REVIEW`, оператор не отвечает | Через 20 минут user task отменён, `fraudDecision = ALLOW`, платёж уходит в перевод. Раньше 20 минут таймер не срабатывает |
| 7 | Отмена клиентом во время ручной проверки | интеграционный | Camunda, Payment Service API | Экземпляр ждёт `MANUAL_FRAUD_REVIEW` | `POST /payments/{id}/cancel` → 202, user task прерван, компенсация выполнена, статус `CANCELLED` |
| 8 | Отмена клиентом после pivot невозможна | интеграционный | Camunda, Payment Service API | Платёж в `TRANSFERRED` / `COMPLETED` | API отвечает 409, сообщение `CustomerCancelled` не коррелируется (нет подписки), возврата нет |
| 9 | Технический сбой шага: ретраи и инцидент без компенсации | интеграционный | Camunda, worker с ошибкой | `complete-payment` всегда падает с исключением, `retries = 10` | Попытки идут с растущим `retryBackOff`, после последней в Operate появляется инцидент. Экземпляр остаётся активным, в компенсацию не уходит. После resolve incident процесс продолжается |
| 10 | Запрещённый переход State Machine | интеграционный | Payment Service, PostgreSQL | Платёж в `COMPLETED` | Попытка перевести в `COMPENSATING` → `IllegalTransition`. Статус не изменился, событие в `payment_events` не записано |
| 11 | Повторная доставка job списания не списывает дважды | интеграционный | Payment Service, PostgreSQL, WireMock (банк) | Проводка `debit:{paymentId}` уже записана, но job не завершён (сбой до complete) | Повторный `debit-funds` находит операцию по ключу, повторный вызов банка не делается, проводка `DEBIT` одна, статус `DEBITED` |
| 12 | Таймаут банка при списании | интеграционный | Payment Service, WireMock (банк) | Банк отвечает дольше таймаута, при следующем запросе статуса — «проведено» | Событие `DebitTimeout` (self-transition), затем запрос статуса и `FundsDebited`. Списание одно |
| 13 | Кэширование результата антифрода | интеграционный | FraudCheck-адаптер, Redis, WireMock (провайдер) | Вердикт для `paymentId` уже в Redis | Повторная проверка (ретрай job) берёт вердикт из кэша, WireMock фиксирует ровно 1 вызов провайдера |
| 14 | Деградация антифрод-провайдера | интеграционный | FraudCheck-адаптер, WireMock (провайдер) | Провайдер отвечает 503, затем 200 | Повтор с backoff, итоговый вердикт получен. При постоянном 503 срабатывает cut-off процесса |
| 15 | Идемпотентность создания платежа | интеграционный | Payment Service API, Redis, Camunda | Платёж с `orderId` уже создан | Повторный `POST /payments` возвращает тот же `paymentId` с `duplicate: true`, второй экземпляр процесса не создаётся |
| 16 | Параллельные платежи не превышают суточный лимит | интеграционный | Limits-адаптер, Redis | У клиента лимит 300 000, 5 параллельных платежей по 100 000 | Зарезервировано не больше 3 платежей, остальные получают `LIMIT_EXCEEDED`. Счётчик лимита согласован |
| 17 | Контракт BPMN ↔ workers | интеграционный (контрактный) | `payment-saga.bpmn`, реестр обработчиков | — | Каждому job type из BPMN соответствует обработчик. Каждый `errorCode`, который бросают обработчики, объявлен в BPMN и перехватывается boundary event. У шагов до pivot есть compensation handler, после pivot — нет |
| 18 | E2E: успешный платёж | end-to-end | Весь стенд: Camunda, Payment Service, PostgreSQL, Redis, WireMock | Стенд поднят, процесс задеплоен | За < 5 с статус `COMPLETED`. Проводки `DEBIT` и `TRANSFER`, остаток транзитного счёта 0. Клиенту одно уведомление. В Operate экземпляр завершён без инцидентов |
| 19 | E2E: оператор разрешил | end-to-end | Весь стенд + Tasklist API | Сценарий с `MANUAL_REVIEW`, задача в Tasklist | Оператор назначает задачу и завершает с `ALLOW` → `OperatorApproved`, `COMPLETED`. В Tasklist задача в «Completed» с комментарием |
| 20 | E2E: оператор запретил | end-to-end | Весь стенд + Tasklist API | Сценарий с `MANUAL_REVIEW` | Завершение с `DENY` → `OperatorDenied`, проводка `REFUND`, служба безопасности уведомлена, статус `BLOCKED`, остаток транзитного счёта 0 |
| 21 | E2E: мошенничество, мгновенный возврат | end-to-end | Весь стенд | Провайдер возвращает AML-запрет | Статус `BLOCKED`. От решения до `REFUND` не больше 5 с. Уведомление службе безопасности. Перевода контрагенту нет |
| 22 | E2E: антифрод не ответил (cut-off) | end-to-end | Весь стенд, WireMock с задержкой 6 с | `fraudCheckCutOff = PT3S` | `FraudCheckCutOffExpired` → `COMPLETED`. Поздний ответ провайдера залогирован и не меняет процесс |
| 23 | E2E: отказ в переводе контрагенту | end-to-end | Весь стенд | Банк отклоняет перевод (счёт закрыт) | `TransferRejected` → `REFUND` → `CANCELLED`. Деньги контрагенту не ушли, остаток транзитного счёта 0 |
| 24 | E2E: рестарт сервиса во время ожидания оператора | end-to-end | Весь стенд | Платёж в `MANUAL_REVIEW`, контейнер `payment-service` перезапущен | Задача осталась в Tasklist. После решения оператора процесс доходит до `COMPLETED`, ни один шаг не выполнен дважды |
| 25 | E2E: падение worker посреди списания | end-to-end | Весь стенд | Контейнер `payment-service` убит после проводки `DEBIT`, но до complete job | После таймаута job повторно активирован, проводка одна, платёж продолжает Saga |
| 26 | E2E: перезапуск Camunda | end-to-end | Весь стенд | Активные экземпляры на разных шагах, контейнер `orchestration` перезапущен | Все экземпляры продолжаются с того же шага. Новые платежи во время простоя получают 503, ни один не создан наполовину |
| 27 | E2E: SLA под нагрузкой | end-to-end (нагрузочный) | Весь стенд, Locust | 50 платежей/с, 10 минут, сценарий `success` | p90 времени `PaymentInitiated → PaymentCompleted` < 5 с, ошибок нет, инцидентов в Operate нет |
