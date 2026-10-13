import io
from pathlib import Path

from pypdf import PdfReader

from app.services.invoice_pdf import amount_in_words, build_invoice_pdf, save_invoice_pdf


def test_amount_in_words_matches_sample():
    assert amount_in_words(6500) == "Шість тисяч п'ятсот гривень 00 копійок"
    assert amount_in_words(1) == "Одна гривня 00 копійок"
    assert amount_in_words(21.5) == "Двадцять одна гривня 50 копійок"


def test_build_invoice_pdf_bytes():
    pdf = build_invoice_pdf(
        invoice_number="667",
        buyer={
            "company_name": "ТОВ Тест",
            "edrpou": "12345678",
            "ipn": "123456789012",
            "company_address": "м. Київ, вул. Хрещатик, 1",
            "phone": "+380501112233",
            "contract": "Рахунок",
        },
        items=[{"product": "Шнурок для бейджа 15 мм", "qty": 500, "amount": 6500, "unit_price": 13}],
        total_amount=6500,
        invoice_date=__import__("datetime").date(2026, 10, 9),
    )
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "Покупець:" in text
    assert "ТОВ Тест" in text
    assert "Код за ЄДРПОУ 12345678" in text
    assert "ІПН 123456789012" in text
    assert "Хрещатик" in text
    assert "Тел.:" in text
    assert "Назва компанії:" not in text
    assert "Договір" in text
    assert "Рахунок" in text
    assert "Товари (роботи, послуги)" in text
    assert "Всього:" in text
    assert "Разом" not in text
    assert "Всього найменувань 1, на суму 6 500,00 грн." in text
    assert "Шість тисяч п'ятсот гривень 00 копійок" in text
    assert "Виписав:" in text
    assert "Афонін Дмитро Вікторович" in text


def test_build_invoice_pdf_omits_absent_ipn_and_keeps_full_address():
    address = "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул. Київська, буд. 22"
    pdf = build_invoice_pdf(
        invoice_number="INV-1",
        buyer={
            "company_name": "ФОП Афонін Дмитро Вікторович",
            "edrpou": "3052600213",
            "ipn": "непає",
            "company_address": address,
        },
        items=[{"product": "Шнурок для бейджа", "qty": 100, "amount": 2850, "unit_price": 28.5}],
        total_amount=2850,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "непає" not in text.lower()
    assert "немає" not in text.lower()
    assert "ІПН:" not in text
    assert "Кременецький" in text
    assert "Бриків" in text
    assert "47140" in text


def test_zero_header_total_uses_line_sum_and_short_number():
    pdf = build_invoice_pdf(
        invoice_number="0000-000007",
        buyer={
            "company_name": "ФОП Афонін Дмитро Вікторович",
            "edrpou": "3052600345",
            "company_address": "47140, Тернопільська обл., Кременецький р-н, с. Бриків, вул. Київська, буд. 22",
        },
        items=[{
            "product": "Шнурок для бейджа з логотипом і карабіном, 15 мм",
            "qty": 100,
            "unit_price": 28.5,
            "amount": 2850,
        }],
        total_amount=0,
    )
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "№ 7 від" in text
    assert "0000-000007" not in text
    assert "Всього найменувань 1, на суму 2 850,00 грн." in text
    assert "Дві тисячі вісімсот п'ятдесят гривень 00 копійок" in text
    assert "Нуль гривень" not in text
    assert "Постачальник:" in text
    assert "Покупець:" in text
    assert "Код за ЄДРПОУ 3052600345" in text
    assert "Назва компанії:" not in text


def test_save_invoice_pdf_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.invoice_pdf.INVOICES_DIR", tmp_path)
    path = save_invoice_pdf(
        invoice_number="INV-ABC",
        buyer={"company_name": "ФОП Тест", "edrpou": "87654321", "company_address": "Львів"},
        items=[{"product": "Світловідбиваючий", "qty": 50, "amount": 2500}],
        total_amount=2500,
        order_id=42,
    )
    assert path.exists()
    assert path.suffix == ".pdf"
    assert path.read_bytes()[:4] == b"%PDF"
