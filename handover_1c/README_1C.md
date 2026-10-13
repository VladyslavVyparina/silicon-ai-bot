# Silicon — package for the 1C integration

## What is in the folder

| File | Role |
|---|---|
| `API_1C_CONTRACT.md` | The HTTP API 1C is expected to expose |
| `LETTER_TO_SEND.txt` | A cover note for the 1C specialist. Passwords are not in this copy. |
| `README_1C.md` | This page |

The anonymized database, `.env` files, and `ACCESS.txt` are not in this repository. Put your own test credentials on the machine that runs the stand.

## Docker

From the repository root:

```bash
docker compose -f docker-compose.1c-test.yml up -d --build
```

Then open:

- Test bot (web, no Telegram): `http://127.0.0.1:8001/test-bot`
- Swagger: `http://127.0.0.1:8001/docs`
- Admin: `http://127.0.0.1:8001/admin`
- Health: `http://127.0.0.1:8001/health`

The admin login is whatever you set in the local env. It is not stored here.

## Without Docker

```bash
cp handover_1c/.env.local .env
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Create `.env.local` yourself. Do not commit it.

## 1C mode

- `MOCK_EXTERNAL_APIS=true` is the default. The bot pretends to be 1C and accepts the sample counterparties `12345678` and `87654321`.
- For a real or test 1C base, set `MOCK_EXTERNAL_APIS=false`, fill `ONEC_API_USER` and `ONEC_API_PASSWORD`, and publish the endpoints in `API_1C_CONTRACT.md`.
