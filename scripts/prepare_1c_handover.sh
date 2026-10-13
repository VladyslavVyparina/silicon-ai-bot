#!/usr/bin/env bash
# Одна команда: готує все для програміста 1С
#   bash scripts/prepare_1c_handover.sh
# Опційно підтягнути БД з прода і підняти стенд:
#   FETCH_PROD_DB=1 START_STAND=1 bash scripts/prepare_1c_handover.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/handover_1c"
# На Mac — також на Desktop; на VPS Desktop немає → тільки в проєкті
PKG_ZIP="$OUT/Silicon_1C_handover.zip"
DESKTOP_ZIP="${HOME}/Desktop/Silicon_1C_handover.zip"
HOST="${DEPLOY_HOST:-root@173.242.60.134}"
REMOTE="${DEPLOY_PATH:-/root/ai_silicon}"
PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

mkdir -p "$OUT"
cd "$ROOT"

echo "→ 1/6 Джерело БД"
SRC_DB=""
if [[ "${FETCH_PROD_DB:-0}" == "1" ]]; then
  echo "  тягну з $HOST …"
  mkdir -p "$OUT/_tmp"
  if scp -o BatchMode=yes -o ConnectTimeout=12 \
      "$HOST:$REMOTE/data/silicon_bot.db" "$OUT/_tmp/prod.db" 2>/dev/null; then
    SRC_DB="$OUT/_tmp/prod.db"
    echo "  OK prod DB"
  else
    echo "  ⚠ SSH недоступний — беру локальну БД"
  fi
fi
if [[ -z "$SRC_DB" ]]; then
  if [[ -f "$ROOT/data/silicon_bot.db" ]]; then
    SRC_DB="$ROOT/data/silicon_bot.db"
  else
    echo "Немає silicon_bot.db. Задайте FETCH_PROD_DB=1 або покладіть файл у data/"
    exit 1
  fi
fi

echo "→ 2/6 Анонімізація БД"
"$PY" "$ROOT/scripts/anonymize_db_for_1c.py" --src "$SRC_DB" --dst "$OUT/silicon_bot_1c_test.db"

echo "→ 3/6 .env.1c-test"
cat > "$ROOT/.env.1c-test" << 'EOF'
MOCK_EXTERNAL_APIS=true
DEBUG=true
DATABASE_URL=sqlite+aiosqlite:////app/data/silicon_bot.db
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5
TELEGRAM_BOT_TOKEN=
ADMIN_PASSWORD=CHANGE_ME
ADMIN_OWNER_EMAIL=1c-dev@silicon.local
ADMIN_SECRET_KEY=silicon-1c-test-secret-change-on-prod
ADMIN_PUBLIC_URL=http://127.0.0.1:8001
PUBLIC_BASE_URL=http://127.0.0.1:8001
LIQPAY_SANDBOX=true
ONEC_API_URL=http://185.161.208.178/FOPAfonin/odata/standard.odata
ONEC_API_USER=
ONEC_API_PASSWORD=
INVOICE_SELLER_NAME=ФОП Афонін Дмитро Вікторович
INVOICE_SELLER_ADDRESS=вулиця Київська, буд. 22, с. Бриків, Шумський район, Тернопільська обл., 47140
INVOICE_SELLER_PHONE=0958814263
INVOICE_SELLER_IPN=3052600213
INVOICE_SELLER_BANK=КБ "ПРИВАТБАНК" М.ДНІПРО
INVOICE_SELLER_IBAN=UA393052990000026006003304573
EOF
# локальний запуск без Docker — абсолютний шлях до БД пакету
cp "$ROOT/.env.1c-test" "$OUT/.env.1c-test"
# для uvicorn з Mac (не Docker)
cat > "$OUT/.env.local" << EOF
MOCK_EXTERNAL_APIS=true
DEBUG=true
DATABASE_URL=sqlite+aiosqlite:///${OUT}/silicon_bot_1c_test.db
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5
TELEGRAM_BOT_TOKEN=
ADMIN_PASSWORD=CHANGE_ME
ADMIN_OWNER_EMAIL=1c-dev@silicon.local
ADMIN_SECRET_KEY=silicon-1c-test-secret-change-on-prod
ADMIN_PUBLIC_URL=http://127.0.0.1:8001
PUBLIC_BASE_URL=http://127.0.0.1:8001
LIQPAY_SANDBOX=true
ONEC_API_URL=http://185.161.208.178/FOPAfonin/odata/standard.odata
ONEC_API_USER=
ONEC_API_PASSWORD=
EOF

echo "→ 4/6 Документи для програміста"
cat > "$OUT/README_1C.md" << 'EOF'
# Silicon — пакет для інтеграції з 1С

## Що всередині
| Файл | Опис |
|------|------|
| `silicon_bot_1c_test.db` | Анонімізована копія БД бота |
| `.env.1c-test` / `.env.local` | Налаштування тестового стенду |
| `API_1C_CONTRACT.md` | Контракт HTTP API, який має віддавати 1С |
| `LETTER_TO_SEND.txt` | Готовий лист (можна переслати як є) |
| `ACCESS.txt` | Логіни / URL стенду |

## Швидкий старт (Docker)
З кореня репозиторію `ai_silicon`:

```bash
docker compose -f docker-compose.1c-test.yml up -d --build
```

Відкрити:
- Тестовий бот (web): http://127.0.0.1:8001/test-bot
- Swagger: http://127.0.0.1:8001/docs
- Адмінка: http://127.0.0.1:8001/admin
- Health: http://127.0.0.1:8001/health

Логін адмінки: див. `ACCESS.txt`

## Без Docker
```bash
cd ai_silicon
cp handover_1c/.env.local .env
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Режим 1С
- За замовчуванням `MOCK_EXTERNAL_APIS=true` — бот емулює 1С (тестові контрагенти 12345678 / 87654321).
- Для бойової/тестової 1С: виставте `MOCK_EXTERNAL_APIS=false` і заповніть `ONEC_API_USER` / `ONEC_API_PASSWORD`, підніміть endpoints з `API_1C_CONTRACT.md`.
EOF

cat > "$OUT/API_1C_CONTRACT.md" << 'EOF'
# Контракт API 1С для Silicon Bot

Base URL задається в `ONEC_API_URL` (без слеша в кінці).
Auth: HTTP Basic (`ONEC_API_USER` / `ONEC_API_PASSWORD`).

## 1. Пошук контрагента
`GET {BASE}/counterparties/search?edrpou=12345678`  
або `?name=ТОВ%20Сілікон`

Відповідь: JSON-масив (може бути порожній):
```json
[{"id": "1c-001", "name": "ТОВ Сілікон", "edrpou": "12345678", "ipn": "…", "address": "…"}]
```

## 2. Створення контрагента
`POST {BASE}/counterparties`
```json
{
  "edrpou": "55667788",
  "name": "ТОВ Новий Клієнт",
  "ipn": "9988776655",
  "address": "м. Львів, …",
  "phone": "+38050…",
  "email": "a@b.c"
}
```
Відповідь:
```json
{"id": "1c-xxx", "name": "…", "edrpou": "…"}
```

## 3. Створення рахунку
`POST {BASE}/invoices`
```json
{
  "counterparty_id": "1c-001",
  "amount": 1150,
  "description": "Замовлення Silicon",
  "items": []
}
```
Відповідь:
```json
{"id": "…", "number": "INV-…", "amount": 1150}
```

Якщо публікуєте лише OData — надайте mapping сутностей або окремий HTTP-шар під ці 3 методи.
EOF

STAND_URL="http://127.0.0.1:8001"
if [[ "${PUBLIC_STAND_URL:-}" != "" ]]; then
  STAND_URL="$PUBLIC_STAND_URL"
fi

cat > "$OUT/ACCESS.txt" << EOF
Silicon 1C test stand — доступи
================================
Веб-сервер (API):     ${STAND_URL}
Тестовий бот (web):   ${STAND_URL}/test-bot
Swagger:              ${STAND_URL}/docs
Адмін-панель:         ${STAND_URL}/admin
Health:               ${STAND_URL}/health

Адмін:
  login:    1c_dev
  email:    1c-dev@silicon.local
  password: CHANGE_ME

БД: silicon_bot_1c_test.db (SQLite, анонімізована)
Тестові ЄДРПОУ в mock 1С: 12345678, 87654321

MOCK_EXTERNAL_APIS=true (емуляція 1С у боті)
Для реальної 1С — вимкнути mock і дати логін/пароль HTTP API.
EOF

cat > "$OUT/LETTER_TO_SEND.txt" << EOF
Тема: Доступи для робіт по ТЗ (1С ↔ Silicon bot)

Добрий день!

Надаю все необхідне для робіт згідно ТЗ — копію бази, тестовий бот і веб-сервер.

1) Копія бази
Файл: silicon_bot_1c_test.db (SQLite, анонімізована).
Лежить у архіві Silicon_1C_handover.zip / папці handover_1c.

2) Тестовий бот
Веб-чат (без Telegram): ${STAND_URL}/test-bot
Це той самий сценарій Олега (прайс → замовлення → рахунок / 1С).

3) Веб-сервер
API: ${STAND_URL}
Документація: ${STAND_URL}/docs
Адмінка: ${STAND_URL}/admin
Логін: 1c_dev
Пароль: CHANGE_ME

Запуск у себе (якщо стенд ще не на сервері):
docker compose -f docker-compose.1c-test.yml up -d --build
Деталі — README_1C.md та API_1C_CONTRACT.md у архіві.

Що очікуємо від 1С (коротко):
GET  /counterparties/search
POST /counterparties
POST /invoices
(Basic Auth; повний контракт у API_1C_CONTRACT.md)

Прошу у відповідь:
— URL тестової 1С + логін/пароль для інтеграції;
— підтвердження, що три endpoints готові або OData-mapping;
— тестовий ЄДРПОУ для перевірки.

З повагою,
Silicon
EOF

# копії compose + dockerfile поруч для зручності
cp "$ROOT/docker-compose.1c-test.yml" "$OUT/"
cp "$ROOT/Dockerfile" "$OUT/"
cp "$ROOT/requirements.txt" "$OUT/"

echo "→ 5/6 ZIP-пакет"
rm -f "$PKG_ZIP"
(
  cd "$ROOT"
  zip -r "$PKG_ZIP" handover_1c \
    docker-compose.1c-test.yml Dockerfile requirements.txt \
    app data/knowledge data/branding run_telegram.py \
    scripts/anonymize_db_for_1c.py scripts/prepare_1c_handover.sh \
    -x "handover_1c/Silicon_1C_handover.zip" \
    -x "handover_1c/uvicorn_1c_test.log" \
    -x '**/__pycache__/*' '**/*.pyc' 'data/*.db' 'uploads/*' \
    || zip -r "$PKG_ZIP" handover_1c/silicon_bot_1c_test.db \
      handover_1c/README_1C.md handover_1c/ACCESS.txt \
      handover_1c/LETTER_TO_SEND.txt handover_1c/API_1C_CONTRACT.md \
      docker-compose.1c-test.yml Dockerfile
)
echo "  → $PKG_ZIP"
if [[ -d "${HOME}/Desktop" ]]; then
  cp -f "$PKG_ZIP" "$DESKTOP_ZIP"
  echo "  → $DESKTOP_ZIP"
fi

echo "→ 6/6 Стенд"
if [[ "${START_STAND:-0}" == "1" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker compose -f "$ROOT/docker-compose.1c-test.yml" up -d --build
    sleep 2
    curl -fsS "http://127.0.0.1:8001/health" && echo || true
  else
    echo "  Docker немає — стартую uvicorn на :8001"
    # зупинити старий якщо був
    pkill -f 'uvicorn app.main:app --host 0.0.0.0 --port 8001' 2>/dev/null || true
    set -a
    # shellcheck disable=SC1091
    source "$OUT/.env.local"
    set +a
    nohup "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8001 \
      >> "$OUT/uvicorn_1c_test.log" 2>&1 &
    for i in 1 2 3 4 5 6 7 8 9 10; do
      if curl -fsS "http://127.0.0.1:8001/health" >/dev/null 2>&1; then
        curl -fsS "http://127.0.0.1:8001/health"; echo
        break
      fi
      sleep 1
    done
  fi
else
  echo "  (пропущено; для старту: START_STAND=1 bash scripts/prepare_1c_handover.sh)"
fi

rm -rf "$OUT/_tmp"
echo
echo "ГОТОВО."
echo "  Папка:  $OUT"
echo "  Архів:  $PKG_ZIP"
[[ -f "$DESKTOP_ZIP" ]] && echo "  Desktop: $DESKTOP_ZIP"
echo "  Лист:   $OUT/LETTER_TO_SEND.txt"
echo "  Доступ: $OUT/ACCESS.txt"
