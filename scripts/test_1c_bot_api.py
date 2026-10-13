#!/usr/bin/env python3
"""Жива перевірка HTTP-сервісу 1С bot-api (пошук / контрагент / рахунок / PDF).

  ONEC_API_URL=http://localhost/silikon \\
  ONEC_API_USER=... ONEC_API_PASSWORD=... \\
  python scripts/test_1c_bot_api.py --code 12345678
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.onec import _api_base  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("ONEC_API_URL", "http://localhost/silikon"))
    parser.add_argument("--user", default=os.environ.get("ONEC_API_USER", ""))
    parser.add_argument("--password", default=os.environ.get("ONEC_API_PASSWORD", ""))
    parser.add_argument("--code", default="12345678", help="ЄДРПОУ для пошуку")
    parser.add_argument("--create", action="store_true", help="створити тестового контрагента і рахунок")
    args = parser.parse_args()

    base = _api_base(args.url)
    auth = (args.user, args.password) if args.user else None
    print(f"BASE {base}")
    print(f"AUTH {'yes' if auth else 'no'}")

    search_url = f"{base}/counterparty"
    with httpx.Client(timeout=20) as client:
        print(f"\nGET {search_url}?code={args.code}")
        r = client.get(search_url, params={"code": args.code}, auth=auth)
        print(f"  HTTP {r.status_code}")
        print(f"  {r.text[:1500]}")
        if r.status_code == 401:
            print("\nПотрібен логін/пароль користувача 1С (Basic Auth).")
            return 1

        if not args.create:
            return 0 if r.status_code < 500 else 1

        payload = {
            "edrpou": "11112222",
            "code": "11112222",
            "name": "ТОВ Тест Бот Silicon",
            "ipn": "1111222233",
            "address": "м. Київ, вул. Тестова, 1",
            "phone": "+380501112233",
        }
        print(f"\nPOST {base}/counterparty")
        print(json.dumps(payload, ensure_ascii=False))
        r2 = client.post(f"{base}/counterparty", json=payload, auth=auth)
        print(f"  HTTP {r2.status_code}")
        print(f"  {r2.text[:1500]}")
        cp_id = None
        try:
            body = r2.json()
            cp_id = body.get("id") or (body.get("data") or {}).get("id")
        except Exception:
            pass
        if not cp_id:
            print("Немає id контрагента — рахунок не створюю.")
            return 1

        inv = {
            "counterparty_id": cp_id,
            "amount": 20280,
            "description": "Тест Silicon bot",
            "items": [
                {
                    "product": "Шнурок для бейджа з логотипом і двома карабінами, 20мм",
                    "qty": 600,
                    "unit_price": 33.8,
                    "amount": 20280,
                }
            ],
        }
        print(f"\nPOST {base}/invoice")
        print(json.dumps(inv, ensure_ascii=False))
        r3 = client.post(f"{base}/invoice", json=inv, auth=auth)
        print(f"  HTTP {r3.status_code}")
        print(f"  {r3.text[:1500]}")
        inv_id = None
        try:
            body = r3.json()
            inv_id = body.get("id") or (body.get("data") or {}).get("id")
        except Exception:
            pass
        if inv_id:
            pdf_url = f"{base}/invoice/{inv_id}/pdf"
            print(f"\nGET {pdf_url}")
            r4 = client.get(pdf_url, auth=auth)
            print(f"  HTTP {r4.status_code} type={r4.headers.get('content-type')} bytes={len(r4.content)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
