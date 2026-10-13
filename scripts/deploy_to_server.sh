#!/usr/bin/env bash
# Безпечне оновлення коду на сервері БЕЗ затирання БД і .env
# Запускати з Mac:
#   bash scripts/deploy_to_server.sh
set -euo pipefail

HOST="${DEPLOY_HOST:-root@173.242.60.134}"
REMOTE="${DEPLOY_PATH:-/root/ai_silicon}"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ rsync $LOCAL → $HOST:$REMOTE (без .env, *.db, venv, data/*.db)"
rsync -avz --delete \
  --exclude '.env' \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.db' \
  --exclude 'data/*.db' \
  --exclude 'data/consultant_prompt_override.txt' \
  --exclude 'data/bot_runtime.json' \
  --exclude 'silicon_bot.db' \
  --exclude '.DS_Store' \
  --exclude 'uploads/avatars/*' \
  "$LOCAL/" "$HOST:$REMOTE/"

echo "→ migrate DB path + restart на сервері"
ssh "$HOST" bash -s << 'REMOTE_EOF'
set -euo pipefail
cd /root/ai_silicon
mkdir -p data

# перенести стару БД з кореня в data/, якщо ще лежить там
if [ -f silicon_bot.db ] && [ ! -f data/silicon_bot.db ]; then
  mv silicon_bot.db data/silicon_bot.db
  echo "Перенесено silicon_bot.db → data/silicon_bot.db"
fi

# у .env прописати абсолютний шлях до БД (не затираємо інші ключі)
if [ -f .env ]; then
  if grep -q '^DATABASE_URL=' .env; then
    sed -i 's|^DATABASE_URL=.*|DATABASE_URL=sqlite+aiosqlite:////root/ai_silicon/data/silicon_bot.db|' .env
  else
    echo 'DATABASE_URL=sqlite+aiosqlite:////root/ai_silicon/data/silicon_bot.db' >> .env
  fi
fi

# модель агента за замовчуванням (промпт з адмінки НЕ чіпаємо)
mkdir -p data
if [ -f data/bot_runtime.json ]; then
  python3 - << 'PY'
import json
from pathlib import Path
p = Path("data/bot_runtime.json")
try:
    data = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}
data["openai_model"] = "gpt-5"
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
PY
else
  echo '{"openai_model":"gpt-5"}' > data/bot_runtime.json
fi
if [ -f data/consultant_prompt_override.txt ]; then
  echo "Залишено промпт з адмінки: data/consultant_prompt_override.txt"
fi
if [ -f .env ]; then
  if grep -q '^OPENAI_MODEL=' .env; then
    sed -i 's|^OPENAI_MODEL=.*|OPENAI_MODEL=gpt-5|' .env
  else
    echo 'OPENAI_MODEL=gpt-5' >> .env
  fi
fi

# залежності для PDF-рахунку (на проді часто немає reportlab)
/root/ai_silicon/venv/bin/pip install -q -r /root/ai_silicon/requirements.txt

# перезапуск процесів (повний шлях до venv — на Ubuntu часто немає команди `python`)
pkill -f 'uvicorn app.main:app' 2>/dev/null || true
pkill -f 'run_telegram.py' 2>/dev/null || true
sleep 1
nohup /root/ai_silicon/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> /var/log/silicon_api.log 2>&1 &
nohup /root/ai_silicon/venv/bin/python /root/ai_silicon/run_telegram.py >> /var/log/silicon_tg.log 2>&1 &
sleep 2
ps aux | grep -E 'uvicorn|run_telegram' | grep -v grep || true
curl -s http://127.0.0.1:8000/health || true
echo
echo "API/Telegram перезапущено. Модель: gpt-5. DB: $(ls -la data/silicon_bot.db 2>/dev/null || echo 'ще не створена')"
REMOTE_EOF

echo "Готово."
