import uuid
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()


class NovaPoshtaService:
    API_URL = "https://api.novaposhta.ua/v2.0/json/"

    def __init__(self):
        self.api_key = get_settings().nova_poshta_api_key

    async def _request(self, model: str, method: str, props: dict[str, Any]) -> dict:
        s = get_settings()
        if s.mock_external_apis:
            if method == "save" and model == "InternetDocument":
                ttn = "2045" + uuid.uuid4().hex[:10].upper()
                return {"success": True, "data": [{"IntDocNumber": ttn, "Ref": "doc"}]}
            if method == "save" and model == "Counterparty":
                return {
                    "success": True,
                    "data": [{"Ref": "mock-r", "ContactPerson": [{"Ref": "mock-c"}]}],
                }
            if method == "getCities":
                return {"success": True, "data": [{"Ref": "mock-city", "Description": "Kyiv"}]}
            if method == "getWarehouses":
                return {"success": True, "data": [{"Ref": "mock-wh", "Description": "Відділення №12"}]}
            if method == "save" and model == "Address":
                return {"success": True, "data": [{"Ref": "mock-street-addr"}]}
            return {"success": True, "data": []}
        payload = {
            "apiKey": s.nova_poshta_api_key,
            "modelName": model,
            "calledMethod": method,
            "methodProperties": props,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self.API_URL, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def search_city(self, city_name: str) -> list[dict]:
        result = await self._request("Address", "getCities", {"FindByString": city_name, "Limit": 5})
        return [{"ref": i["Ref"], "name": i.get("Description")} for i in result.get("data", []) if i.get("Ref")]

    async def get_warehouses(self, city_ref: str) -> list[dict]:
        result = await self._request("Address", "getWarehouses", {"CityRef": city_ref, "Limit": 50})
        return [{"ref": w["Ref"], "name": w.get("Description")} for w in result.get("data", [])]

    async def create_recipient(
        self,
        name: str,
        phone: str,
        last_name: str | None = None,
        first_name: str | None = None,
        patronymic: str | None = None,
    ) -> dict:
        if last_name or first_name:
            first = (first_name or "Klient").strip()
            last = (last_name or "Klient").strip()
            middle = (patronymic or "").strip()
        else:
            parts = name.strip().split()
            if len(parts) >= 3:
                last, first, middle = parts[0], parts[1], " ".join(parts[2:])
            elif len(parts) == 2:
                last, first, middle = parts[0], parts[1], ""
            else:
                first = parts[0] if parts else "Klient"
                last, middle = first, ""

        phone = phone.replace("+", "").replace(" ", "")
        result = await self._request(
            "Counterparty",
            "save",
            {
                "FirstName": first,
                "LastName": last,
                "MiddleName": middle,
                "Phone": phone,
                "Email": "",
                "CounterpartyType": "PrivatePerson",
                "CounterpartyProperty": "Recipient",
            },
        )
        if not result.get("success") or not result.get("data"):
            return {"success": False, "error": result.get("errors") or result}
        data = result["data"][0]
        contact_ref = None
        cp = data.get("ContactPerson") or []
        if isinstance(cp, list) and cp:
            contact_ref = cp[0].get("Ref")
        elif isinstance(cp, dict):
            arr = cp.get("data") or []
            contact_ref = arr[0].get("Ref") if arr else cp.get("Ref")
        return {
            "success": True,
            "recipient_ref": data.get("Ref"),
            "contact_ref": contact_ref or data.get("Ref"),
        }

    async def create_ttn(self, delivery_data: dict[str, Any]) -> dict:
        s = get_settings()
        needed = [
            s.nova_poshta_sender_ref,
            s.nova_poshta_contact_sender_ref,
            s.nova_poshta_city_sender_ref,
            s.nova_poshta_sender_address_ref,
        ]
        if not s.mock_external_apis and not all(needed):
            return {"success": False, "error": "Missing NOVA_POSHTA_*_REF in .env"}

        city_ref = delivery_data.get("city_ref")
        warehouse_ref = delivery_data.get("warehouse_ref")
        delivery_type = delivery_data.get("delivery_type") or "warehouse"
        is_address = delivery_type == "address"

        if not city_ref:
            return {"success": False, "error": f"Missing city_ref={city_ref}"}
        if not is_address and not warehouse_ref:
            return {"success": False, "error": f"Missing warehouse_ref={warehouse_ref}"}

        recipient = await self.create_recipient(
            delivery_data.get("recipient_name", "Klient"),
            delivery_data.get("recipient_phone", "380501111111"),
            last_name=delivery_data.get("recipient_last_name"),
            first_name=delivery_data.get("recipient_first_name"),
            patronymic=delivery_data.get("recipient_patronymic"),
        )
        if not recipient.get("success"):
            return recipient

        recipient_address = warehouse_ref
        service_type = "WarehouseWarehouse"
        if is_address:
            service_type = "WarehouseDoors"
            street = delivery_data.get("street_address") or "вул. Невідома, 1"
            addr = await self._request(
                "Address",
                "save",
                {
                    "CounterpartyRef": recipient["recipient_ref"],
                    "StreetRef": "",
                    "BuildingNumber": street,
                    "Flat": "",
                    "Note": street,
                },
            )
            if addr.get("success") and addr.get("data"):
                recipient_address = addr["data"][0].get("Ref") or recipient_address
            if not recipient_address:
                # Mock / fallback: still allow TTN creation path in tests
                recipient_address = "mock-street-addr" if s.mock_external_apis else None
            if not recipient_address:
                return {"success": False, "error": "Не вдалося створити адресу отримувача"}

        phone = str(delivery_data.get("recipient_phone", "")).replace("+", "").replace(" ", "")
        props = {
            "PayerType": "Recipient",
            "PaymentMethod": "Cash",
            "CargoType": "Parcel",
            "Weight": str(delivery_data.get("weight_kg", 1)),
            "ServiceType": service_type,
            "SeatsAmount": str(delivery_data.get("seats_amount", 1)),
            "Description": delivery_data.get("description") or "Silicon order",
            "Cost": str(int(float(delivery_data.get("cost", 100)))),
            "CitySender": s.nova_poshta_city_sender_ref or "mock-city-sender",
            "Sender": s.nova_poshta_sender_ref or "mock-sender",
            "SenderAddress": s.nova_poshta_sender_address_ref or "mock-sender-addr",
            "ContactSender": s.nova_poshta_contact_sender_ref or "mock-contact-sender",
            "SendersPhone": "380501111111",
            "CityRecipient": city_ref,
            "Recipient": recipient["recipient_ref"],
            "RecipientAddress": recipient_address,
            "ContactRecipient": recipient["contact_ref"],
            "RecipientsPhone": phone,
        }
        result = await self._request("InternetDocument", "save", props)
        if not result.get("success") or not result.get("data"):
            return {"success": False, "error": result.get("errors") or result}
        doc = result["data"][0]
        ttn = doc.get("IntDocNumber")
        return {
            "success": True,
            "ttn_number": ttn,
            "ttn_url": f"https://novaposhta.ua/tracking/?cargo_number={ttn}",
            "document_ref": doc.get("Ref"),
            "service_type": service_type,
        }
