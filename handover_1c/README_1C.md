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
