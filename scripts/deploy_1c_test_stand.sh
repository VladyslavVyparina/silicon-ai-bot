#!/usr/bin/env bash
# Деплой тестового стенду 1С (порт 8001, окремо від бойового :8000)
#
# З Mac (заливає на VPS і стартує):
#   bash scripts/deploy_1c_test_stand.sh
#
# Прямо на VPS (без rsync/ssh):
#   bash /root/ai_silicon/scripts/deploy_1c_test_stand.sh
set -euo pipefail

HOST="${DEPLOY_HOST:-root@173.242.60.134}"
REMOTE="${DEPLOY_PATH:-/root/ai_silicon}"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_URL="${PUBLIC_STAND_URL:-http://173.242.60.134:8001}"

start_stand_on_this_host() {
  local root="$1"
  cd "$root"
  mkdir -p handover_1c data

  if [[ ! -f handover_1c/silicon_bot_1c_test.db ]]; then
    echo "Немає handover_1c/silicon_bot_1c_test.db — спочатку prepare"
    exit 1
  fi

  cat > handover_1c/.env.server << EOF
MOCK_EXTERNAL_APIS=true
DEBUG=true
DATABASE_URL=sqlite+aiosqlite:////${root}/handover_1c/silicon_bot_1c_test.db
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5
TELEGRAM_BOT_TOKEN=
ADMIN_PASSWORD=CHANGE_ME
ADMIN_OWNER_EMAIL=1c-dev@silicon.local
ADMIN_SECRET_KEY=silicon-1c-test-secret
ADMIN_PUBLIC_URL=${PUBLIC_URL}
PUBLIC_BASE_URL=${PUBLIC_URL}
LIQPAY_SANDBOX=true
ONEC_API_URL=http://185.161.208.178/FOPAfonin/odata/standard.odata
ONEC_API_USER=
ONEC_API_PASSWORD=
EOF

  # оновити лист/доступи під публічний URL
  cat > handover_1c/ACCESS.txt << EOF
Silicon 1C test stand — доступи
================================
Веб-сервер (API):     ${PUBLIC_URL}
Тестовий бот (web):   ${PUBLIC_URL}/test-bot
Swagger:              ${PUBLIC_URL}/docs
Адмін-панель:         ${PUBLIC_URL}/admin
Health:               ${PUBLIC_URL}/health

Адмін:
  login:    1c_dev
  email:    1c-dev@silicon.local
  password: CHANGE_ME

БД: silicon_bot_1c_test.db (SQLite, анонімізована)
Тестові ЄДРПОУ в mock 1С: 12345678, 87654321
MOCK_EXTERNAL_APIS=true
EOF

  cat > handover_1c/LETTER_TO_SEND.txt << EOF
Тема: Доступи для робіт по ТЗ (1С ↔ Silicon bot)

Добрий день!

Надаю все необхідне для робіт згідно ТЗ.

1) Копія бази — файл silicon_bot_1c_test.db (у вкладенні / архіві).
2) Тестовий бот: ${PUBLIC_URL}/test-bot
3) Веб-сервер: ${PUBLIC_URL}
   Swagger: ${PUBLIC_URL}/docs
   Адмінка: ${PUBLIC_URL}/admin
   Логін: 1c_dev
   Пароль: CHANGE_ME

Контракт API 1С (що має віддавати 1С):
GET  /counterparties/search
POST /counterparties
POST /invoices
(Basic Auth; деталі в API_1C_CONTRACT.md)

Прошу у відповідь URL тестової 1С, логін/пароль і тестовий ЄДРПОУ.

З повагою,
Silicon
EOF

  local py="${root}/venv/bin/uvicorn"
  if [[ ! -x "$py" ]]; then
    echo "Немає $py — активуйте venv на сервері"
    exit 1
  fi

  pkill -f 'uvicorn app.main:app --host 0.0.0.0 --port 8001' 2>/dev/null || true
  sleep 1
  set -a
  # shellcheck disable=SC1091
  source handover_1c/.env.server
  set +a
  nohup "$py" app.main:app --host 0.0.0.0 --port 8001 \
    >> /var/log/silicon_1c_test.log 2>&1 &

  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
      curl -fsS http://127.0.0.1:8001/health
      echo
      break
    fi
    sleep 1
  done

  echo
  echo "Тестовий стенд: ${PUBLIC_URL}/test-bot"
  echo "Адмінка:        ${PUBLIC_URL}/admin  (1c_dev / CHANGE_ME)"
  echo "Лист:           ${root}/handover_1c/LETTER_TO_SEND.txt"
  echo "Архів:          ${root}/handover_1c/Silicon_1C_handover.zip"
}

# Якщо вже на VPS у /root/ai_silicon — не робимо rsync на себе
ON_SERVER=0
if [[ "$LOCAL" == "/root/ai_silicon" ]] || [[ "$(hostname 2>/dev/null || true)" == *vps* ]] || [[ -f /root/ai_silicon/run_telegram.py && "$LOCAL" == /root/ai_silicon* ]]; then
  if [[ -d /root/ai_silicon ]] && [[ "$LOCAL" == /root/ai_silicon* ]]; then
    ON_SERVER=1
  fi
fi

echo "→ Готую handover"
PUBLIC_STAND_URL="$PUBLIC_URL" FETCH_PROD_DB=0 START_STAND=0 bash "$LOCAL/scripts/prepare_1c_handover.sh"

if [[ "$ON_SERVER" == "1" ]]; then
  echo "→ Запуск стенду на цьому сервері (:8001)"
  start_stand_on_this_host "$LOCAL"
else
  echo "→ rsync на $HOST"
  rsync -avz --delete \
    --exclude '.env' \
    --exclude '.git' \
    --exclude 'venv' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'data/silicon_bot.db' \
    --exclude 'silicon_bot.db' \
    --exclude '.DS_Store' \
    --exclude 'uploads/avatars/*' \
    "$LOCAL/" "$HOST:$REMOTE/"

  echo "→ Запуск на віддаленому сервері"
  ssh "$HOST" "PUBLIC_STAND_URL='$PUBLIC_URL' bash $REMOTE/scripts/deploy_1c_test_stand.sh"
fi

echo "Готово."
