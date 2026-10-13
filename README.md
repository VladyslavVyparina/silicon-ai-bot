# Silicon — продажі в Telegram

Бот магазину silicon.com.ua веде консультацію за каталогом, рахує ціну, збирає доставку Новою поштою, виставляє рахунок і приймає оплату. Рахунки створюються в 1С, оплата йде через LiqPay, угоди — в CRM.

**Стек.** Python, FastAPI, Telegram, 1С, LiqPay, Нова пошта.

У публічній копії немає паролів, бази клієнтів і файлів рахунків.

Публічний кейс. Робоча копія з паролями і базами лишається локально і сюди не входить.

---

# Silicon AI Bot Integrator

Сервер-інтегратор для AI-бота компанії Silicon (silicon.com.ua).

## Можливості

- **AI-консультант** — відповіді по товарах на основі каталогу та інструкцій
- **Калькулятор цін** — розрахунок вартості та термінів виготовлення
- **CRM Onebox** — автоматичне заведення клієнтів та угод
- **1С** — перевірка контрагентів за ЄДРПОУ/назвою (без дублікатів), рахунки
- **LiqPay** — створення платежів та обробка webhook
- **Нова пошта** — збір даних доставки, створення ТТН
- **SendPulse** — проактивні сповіщення (ТТН, оплата)
- **Webhook CRM** — при статусі «Виконано» надсилає ТТН з подякою

## Швидкий старт

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

## API Endpoints

| Метод | URL | Опис |
|-------|-----|------|
| POST | `/api/chat/message` | Головний endpoint для SendPulse |
| POST | `/api/ai/consult` | AI-консультація |
| POST | `/api/pricing/calculate` | Розрахунок вартості |
| POST | `/api/orders/register-client` | Заведення клієнта в CRM |
| POST | `/api/orders/create` | Створення замовлення |
| POST | `/api/orders/delivery` | Дані доставки Нова пошта |
| POST | `/api/orders/{id}/ttn` | Створення ТТН |
| POST | `/api/orders/{id}/payment` | Посилання LiqPay |
| POST | `/api/orders/invoice` | Рахунок на реквізити |
| POST | `/api/webhooks/liqpay` | Webhook оплати |
| POST | `/api/webhooks/onebox/order-completed` | Статус «Виконано» |

## Тести

```bash
pytest -v
```

## Production

Встановіть `MOCK_EXTERNAL_APIS=false` та заповніть ключі API в `.env`.

### Посилання на оплату без IP

У Telegram клієнт бачить короткий URL `/pay/{id}`. Щоб **не світити IP сервера**:

1. Привʼяжіть домен (напр. `bot.silicon.com.ua`) до VPS (DNS A-запис).
2. Пробросьте HTTPS (nginx/caddy) на порт додатку.
3. У `.env` задайте:

```bash
PUBLIC_BASE_URL=https://bot.silicon.com.ua
ADMIN_PUBLIC_URL=https://bot.silicon.com.ua
```

Якщо `PUBLIC_BASE_URL` порожній — використовується `ADMIN_PUBLIC_URL` (на проді з IP це погано).
