#!/usr/bin/env bash
# Прогон сценариев Saga на запущенном стенде (docker compose up -d --build).
#   ./demo/run-demo.sh auto            — все автоматические сценарии, ждёт финального статуса
#   ./demo/run-demo.sh manual          — платежи, требующие ручной проверки (решение — в Tasklist)
#   ./demo/run-demo.sh cancel <id>     — отмена платежа клиентом во время ручной проверки
#   ./demo/run-demo.sh status <id>     — статус, журнал переходов и проводки платежа
set -euo pipefail

API=${API:-http://localhost:8000}
RUN=${RUN:-$(date +%H%M%S)}

pay() { # scenario amount customer
  curl -sf -X POST "$API/payments" -H 'Content-Type: application/json' \
    -d "{\"orderId\":\"order-$RUN-$1-$3\",\"customerId\":\"$3\",\"merchantId\":\"merchant-7\",\"amount\":\"$2\",\"scenario\":\"$1\"}" \
    | sed -E 's/.*"paymentId":"([^"]+)".*/\1/'
}

status_of() { curl -sf "$API/payments/$1" | sed -E 's/.*"status":"([A-Z_]+)".*/\1/'; }

wait_final() { # payment_id
  for _ in $(seq 1 60); do
    s=$(status_of "$1" 2>/dev/null || true)
    case "$s" in COMPLETED|REJECTED|BLOCKED|CANCELLED) echo "$s"; return;; esac
    sleep 1
  done
  echo "TIMEOUT($s)"
}

case "${1:-auto}" in
  auto)
    printf "%-18s %-38s %s\n" "SCENARIO" "PAYMENT ID" "RESULT"
    for spec in "success:1500.00:customer-1" "flaky:2400.00:customer-2" "limit-exceeded:180000.00:customer-3" \
                "debit-declined:3100.00:customer-4" "fraud-deny:9900.00:customer-5" "fraud-timeout:1200.00:customer-6" \
                "transfer-rejected:5600.00:customer-7"; do
      IFS=: read -r scenario amount customer <<<"$spec"
      id=$(pay "$scenario" "$amount" "$customer")
      printf '%-18s %-38s %s\n' "$scenario" "$id" "$(wait_final "$id")"
    done
    ;;
  manual)
    for spec in "manual-review:84000.00:customer-11" "manual-review:120000.00:customer-12" \
                "customer-cancel:47000.00:customer-13" "manual-cut-off:66000.00:customer-14"; do
      IFS=: read -r scenario amount customer <<<"$spec"
      printf '%-18s %s\n' "$scenario" "$(pay "$scenario" "$amount" "$customer")"
    done
    echo "Решения по manual-review принимаются в Tasklist: http://localhost:8080/tasklist (demo / demo)"
    echo "manual-cut-off разрешится сам через 30 с; customer-cancel: ./demo/run-demo.sh cancel <id>"
    ;;
  cancel) curl -sf -X POST "$API/payments/$2/cancel"; echo ;;
  status) curl -sf "$API/payments/$2"; echo ;;
  *) echo "usage: $0 auto|manual|cancel <id>|status <id>"; exit 1 ;;
esac
