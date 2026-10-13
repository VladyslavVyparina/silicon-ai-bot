#!/bin/bash
# Перевірка інтеграцій Silicon AI Bot
# Запуск: bash /root/ai_silicon/check_integrations.sh

set -e
BASE="http://127.0.0.1:8000"
echo "=== 0. HEALTH ==="
curl -s "$BASE/health"; echo; echo

echo "=== 1. REGISTER CLIENT ==="
REG=$(curl -s -X POST "$BASE/api/orders/register-client" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"chk-'"$(date +%s)"'","first_name":"Тест","phone":"+380501111111"}')
echo "$REG"
CHAT=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin)['client_id'] and sys.stdin)" 2>/dev/null || true)
CHAT_ID=$(echo "$REG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('onebox_deal_id',''))" )
CLIENT_CHAT=$(echo "$REG" | python3 -c "import sys,json; print('chk')" )

# беремо chat_id з запиту
CHAT_ID_REQ=$(echo "$REG" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('success') else 'fail')")
DEAL_ID=$(echo "$REG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('onebox_deal_id') or '')")
# re-parse from REG string
CHAT_USED=$(python3 - <<'PY'
import json,os,time
print(os.environ.get('CHAT',''))
PY
)

# Фіксований chat_id для цього прогону
TS=$(date +%s)
CHAT="chk-$TS"

echo
echo "=== REGISTER з chat_id=$CHAT ==="
REG=$(curl -s -X POST "$BASE/api/orders/register-client" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\":\"$CHAT\",\"first_name\":\"Тест\",\"phone\":\"+380501111111\"}")
echo "$REG"
DEAL_ID=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('onebox_deal_id') or '')")

echo
echo "=== 2. CREATE ORDER ==="
ORD=$(curl -s -X POST "$BASE/api/orders/create" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\":\"$CHAT\",\"product_type\":\"силіконовий\",\"quantity\":100,\"total_amount\":1150}")
echo "$ORD"
ORDER_ID=$(echo "$ORD" | python3 -c "import sys,json; print(json.load(sys.stdin).get('order_id') or '')")
echo "ORDER_ID=$ORDER_ID DEAL_ID=$DEAL_ID"

echo
echo "=== 3. LIQPAY PAYMENT ==="
PAY=$(curl -s -X POST "$BASE/api/orders/$ORDER_ID/payment")
echo "$PAY"
LIQ_ID=$(echo "$PAY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('liqpay_order_id') or '')")
DATA=$(echo "$PAY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data') or '')")
SIG=$(echo "$PAY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('signature') or '')")

echo
echo "=== 4. LIQPAY WEBHOOK ==="
if [ -n "$DATA" ] && [ -n "$SIG" ]; then
  # Перестворюємо payload зі статусом success
  cd /root/ai_silicon && source venv/bin/activate
  python3 << PY
import base64, json, urllib.parse, urllib.request
from app.services.liqpay import LiqPayService
s = LiqPayService()
payload = {"order_id": "$LIQ_ID", "status": "success", "amount": 1150}
data = base64.b64encode(json.dumps(payload).encode()).decode()
sig = s._sign(data)
body = f"data={urllib.parse.quote(data)}&signature={urllib.parse.quote(sig)}"
req = urllib.request.Request(
    "$BASE/api/webhooks/liqpay",
    data=body.encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
try:
    print(urllib.request.urlopen(req).read().decode())
except Exception as e:
    print("WEBHOOK ERROR:", e)
    if hasattr(e, 'read'):
        print(e.read().decode())
PY
else
  echo "SKIP: немає data/signature"
fi

echo
echo "=== 5. NOVA POSHTA KEY ==="
curl -s -X POST https://api.novaposhta.ua/v2.0/json/ \
  -H "Content-Type: application/json" \
  -d "{\"apiKey\":\"$(grep NOVA_POSHTA_API_KEY /root/ai_silicon/.env | cut -d= -f2)\",\"modelName\":\"Address\",\"calledMethod\":\"getCities\",\"methodProperties\":{\"FindByString\":\"Київ\",\"Limit\":1}}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('NP_KEY_OK' if d.get('success') else d)"

echo
echo "=== 6. DELIVERY + TTN ==="
curl -s -X POST "$BASE/api/orders/delivery" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\":\"$CHAT\",\"recipient_name\":\"Тест Клієнт\",\"recipient_phone\":\"380501111111\",\"city\":\"Київ\",\"weight_kg\":1}"
echo
TTN=$(curl -s -X POST "$BASE/api/orders/$ORDER_ID/ttn")
echo "$TTN"

echo
echo "=== 7. ONEBOX WEBHOOK ==="
# оновити DEAL_ID якщо create повернув інший
DEAL_ID=$(echo "$ORD" | python3 -c "import sys,json; print(json.load(sys.stdin).get('onebox_deal_id') or '')" 2>/dev/null || echo "$DEAL_ID")
if [ -z "$DEAL_ID" ]; then
  DEAL_ID=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('onebox_deal_id') or '')")
fi
echo "Using DEAL_ID=$DEAL_ID"
curl -s -X POST "$BASE/api/webhooks/onebox/order-completed" \
  -H "Content-Type: application/json" \
  -d "{\"deal_id\":\"$DEAL_ID\",\"status\":\"completed\"}"
echo
echo
echo "=== 8. ONEBOX API TOKEN ==="
curl -s -X POST "https://box.vals.net.ua/api/v2/token/get/" \
  -H "Content-Type: application/json" \
  -d '{"login":"Dima","restapipassword":"!Q2w3e4r5t"}'
echo
echo
echo "DONE"
