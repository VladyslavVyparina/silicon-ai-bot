#!/usr/bin/env python3
"""Отримати Ref відправника Нової пошти і вивести рядки для .env"""
import json
import urllib.request

API = "https://api.novaposhta.ua/v2.0/json/"


def call(api_key, model, method, props):
    payload = json.dumps({
        "apiKey": api_key,
        "modelName": model,
        "calledMethod": method,
        "methodProperties": props,
    }).encode()
    req = urllib.request.Request(API, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    with open("/root/ai_silicon/.env") as f:
        env = dict(
            line.split("=", 1) for line in f if "=" in line and not line.strip().startswith("#")
        )
    key = env.get("NOVA_POSHTA_API_KEY", "").strip()
    if not key:
        raise SystemExit("NOVA_POSHTA_API_KEY порожній")

    senders = call(key, "Counterparty", "getCounterparties", {"CounterpartyProperty": "Sender"})
    if not senders.get("success") or not senders.get("data"):
        print("ERROR senders:", senders)
        return
    sender = senders["data"][0]
    sender_ref = sender["Ref"]
    print("Sender:", sender.get("Description"), sender_ref)

    contacts = call(key, "Counterparty", "getCounterpartyContactPersons", {"Ref": sender_ref, "Page": "1"})
    contact_ref = contacts["data"][0]["Ref"] if contacts.get("data") else ""
    print("Contact:", contacts["data"][0].get("Description") if contacts.get("data") else None, contact_ref)

    cities = call(key, "Address", "getCities", {"FindByString": "Київ", "Limit": 1})
    city_ref = cities["data"][0]["Ref"] if cities.get("data") else ""
    print("City Київ:", city_ref)

    # відділення №148 Київ (з ваших скрінів) — або перше
    wh = call(key, "Address", "getWarehouses", {"CityRef": city_ref, "FindByString": "148", "Limit": 5})
    if not wh.get("data"):
        wh = call(key, "Address", "getWarehouses", {"CityRef": city_ref, "Limit": 1})
    addr_ref = wh["data"][0]["Ref"] if wh.get("data") else ""
    print("Warehouse:", wh["data"][0].get("Description") if wh.get("data") else None, addr_ref)

    print("\n# Додайте в .env:")
    print(f"NOVA_POSHTA_SENDER_REF={sender_ref}")
    print(f"NOVA_POSHTA_CONTACT_SENDER_REF={contact_ref}")
    print(f"NOVA_POSHTA_CITY_SENDER_REF={city_ref}")
    print(f"NOVA_POSHTA_SENDER_ADDRESS_REF={addr_ref}")


if __name__ == "__main__":
    main()
