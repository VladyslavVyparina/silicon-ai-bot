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
