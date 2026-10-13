"""Генерація PDF-рахунку з печаткою та підписом для відправки в Telegram."""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from app.config import PROJECT_ROOT, get_settings

BRANDING_DIR = PROJECT_ROOT / "data" / "branding"
INVOICES_DIR = PROJECT_ROOT / "data" / "invoices"

_FONT_REGISTERED = False
FONT_NAME = "InvoiceUA"


def _register_font() -> str:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return FONT_NAME
    candidates = [
        PROJECT_ROOT / "data" / "branding" / "InvoiceFont.ttf",
        PROJECT_ROOT / "data" / "branding" / "DejaVuSans.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
                _FONT_REGISTERED = True
                return FONT_NAME
            except Exception:
                continue
    _FONT_REGISTERED = True
    return "Helvetica"


def _seller() -> dict[str, str]:
    s = get_settings()
    return {
        "name": getattr(s, "invoice_seller_name", None)
        or "ФОП Афонін Дмитро Вікторович",
        "address": getattr(s, "invoice_seller_address", None)
        or "вулиця Київська, буд. 22, с. Бриків, Шумський район, Тернопільська обл., 47140",
        "phone": getattr(s, "invoice_seller_phone", None) or "0958814263",
        "ipn": getattr(s, "invoice_seller_ipn", None) or "3052600213",
        "bank": getattr(s, "invoice_seller_bank", None) or 'КБ "ПРИВАТБАНК" М.ДНІПРО',
        "iban": getattr(s, "invoice_seller_iban", None)
        or "UA393052990000026006003304573",
    }


def _money(value: Any) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    whole = f"{n:,.2f}".replace(",", " ").replace(".", ",")
    return whole


_MONTHS_UA = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)

_ONES_M = (
    "",
    "один",
    "два",
    "три",
    "чотири",
    "п'ять",
    "шість",
    "сім",
    "вісім",
    "дев'ять",
    "десять",
    "одинадцять",
    "дванадцять",
    "тринадцять",
    "чотирнадцять",
    "п'ятнадцять",
    "шістнадцять",
    "сімнадцять",
    "вісімнадцять",
    "дев'ятнадцять",
)
_TENS = (
    "",
    "",
    "двадцять",
    "тридцять",
    "сорок",
    "п'ятдесят",
    "шістдесят",
    "сімдесят",
    "вісімдесят",
    "дев'яносто",
)
_HUNDREDS = (
    "",
    "сто",
    "двісті",
    "триста",
    "чотириста",
    "п'ятсот",
    "шістсот",
    "сімсот",
    "вісімсот",
    "дев'ятсот",
)


def _ua_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _ua_triplet(n: int, *, female: bool = False) -> str:
    n = int(n)
    words: list[str] = []
    if n >= 100:
        words.append(_HUNDREDS[n // 100])
    rest = n % 100
    if rest >= 20:
        words.append(_TENS[rest // 10])
        rest = rest % 10
    if rest:
        if female and rest == 1:
            words.append("одна")
        elif female and rest == 2:
            words.append("дві")
        else:
            words.append(_ONES_M[rest])
    return " ".join(w for w in words if w)


def amount_in_words(value: Any) -> str:
    """6500 → «Шість тисяч п'ятсот гривень 00 копійок»."""
    try:
        amount = abs(float(value or 0))
    except (TypeError, ValueError):
        amount = 0.0
    kop = int(round((amount - int(amount)) * 100))
    whole = int(amount)
    if kop == 100:
        whole += 1
        kop = 0
    millions = whole // 1_000_000
    thousands = (whole // 1000) % 1000
    rest = whole % 1000
    parts: list[str] = []
    if millions:
        parts.append(_ua_triplet(millions))
        parts.append(_ua_plural(millions, "мільйон", "мільйони", "мільйонів"))
    if thousands:
        parts.append(_ua_triplet(thousands, female=True))
        parts.append(_ua_plural(thousands, "тисяча", "тисячі", "тисяч"))
    if rest or not parts:
        parts.append(_ua_triplet(rest, female=True) or "нуль")
    parts.append(_ua_plural(whole, "гривня", "гривні", "гривень"))
    text = " ".join(p for p in parts if p)
    return f"{text[:1].upper()}{text[1:]} {kop:02d} копійок"


def _date_ua(inv_date: date) -> str:
    return f"{inv_date.day:02d} {_MONTHS_UA[inv_date.month - 1]} {inv_date.year} р."


def _signer_name(seller_name: str) -> str:
    name = (seller_name or "").strip()
    if name.lower().startswith("фоп "):
        name = name[4:].strip()
    return name or "Афонін Дмитро Вікторович"


_BOLD_FONT: str | None = None
_BOLD_TRIED = False


def _bold_font() -> str | None:
    global _BOLD_FONT, _BOLD_TRIED
    if _BOLD_TRIED:
        return _BOLD_FONT
    _BOLD_TRIED = True
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("InvoiceUA-Bold", str(path)))
            _BOLD_FONT = "InvoiceUA-Bold"
            return _BOLD_FONT
        except Exception:
            continue
    return None


def _draw_bold_string(c: canvas.Canvas, x: float, y: float, text: str, font: str, size: float) -> None:
    bold = _bold_font() or font
    c.setFillColor(colors.black)
    c.setFont(bold, size)
    c.drawString(x, y, text)


def _printed_invoice_number(number: str) -> str:
    """0000-000007 у журналі 1С друкується як № 7."""
    raw = str(number or "").strip()
    match = re.fullmatch(r"0+-0*(\d+)", raw)
    if match:
        return str(int(match.group(1)))
    return raw


def items_total(items: list[dict[str, Any]] | None) -> float:
    total = 0.0
    for item in items or []:
        amount = item.get("amount")
        if amount is None:
            amount = item.get("total_amount")
        if amount is None and item.get("unit_price") is not None:
            qty = item.get("qty") if item.get("qty") is not None else item.get("quantity")
            try:
                amount = float(qty or 0) * float(item["unit_price"])
            except (TypeError, ValueError):
                amount = 0
        try:
            total += float(amount or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _wrap_text(c: canvas.Canvas, text: str, font: str, size: float, max_w: float) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if c.stringWidth(trial, font, size) <= max_w:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [str(text)]


def _draw_party_block(
    c: canvas.Canvas,
    label: str,
    values: list[str],
    left: float,
    y: float,
    font: str,
    right: float,
) -> float:
    """Як у друкованій формі 1С: підпис зліва, реквізити колонкою справа."""
    value_x = left + 34 * mm
    max_w = right - value_x
    c.setFillColor(colors.black)
    c.setFont(font, 9)
    first = True
    for value in values:
        if not str(value).strip():
            continue
        for line in _wrap_text(c, value, font, 9, max_w):
            if first:
                c.setFont(font, 9)
                c.drawString(left, y, label)
                first = False
            c.setFont(font, 9)
            c.drawString(value_x, y, line)
            y -= 4.4 * mm
    return y


def _draw_stamp_and_sign(c: canvas.Canvas, x: float, y: float, *, signer: str, font: str) -> None:
    stamp = BRANDING_DIR / "stamp.png"
    sign = BRANDING_DIR / "signature.png"
    stamp_ok = False
    if stamp.exists():
        try:
            c.drawImage(str(stamp), x, y, width=38 * mm, height=38 * mm, mask="auto")
            stamp_ok = True
        except Exception:
            stamp_ok = False
    if not stamp_ok:
        pass

    label_x = x + 46 * mm
    line_x = label_x + 22 * mm
    line_w = 55 * mm
    line_y = y + 16 * mm
    c.setFillColor(colors.black)
    c.setStrokeColor(colors.black)
    c.setFont(font, 10)
    c.drawString(label_x, line_y + 1 * mm, "Виписав:")
    sign_ok = False
    if sign.exists():
        try:
            c.drawImage(
                str(sign),
                line_x + 4 * mm,
                line_y + 1.5 * mm,
                width=42 * mm,
                height=16 * mm,
                mask="auto",
            )
            sign_ok = True
        except Exception:
            sign_ok = False
    if not sign_ok:
        pass
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.6)
    c.line(line_x, line_y, line_x + line_w, line_y)
    c.setFillColor(colors.black)
    c.setFont(font, 9)
    c.drawCentredString(line_x + line_w / 2, line_y - 5 * mm, signer)


def _draw_wrapped_lines(c, lines: list[str], x: float, y: float, font: str, size: float, max_w: float) -> float:
    """Друкує рядки з переносом, щоб довга адреса не обрізалась."""
    c.setFont(font, size)
    for raw in lines:
        words = str(raw).split()
        current = ""
        wrapped: list[str] = []
        for word in words:
            trial = f"{current} {word}".strip()
            if c.stringWidth(trial, font, size) <= max_w:
                current = trial
            else:
                if current:
                    wrapped.append(current)
                current = word
        if current:
            wrapped.append(current)
        for line in wrapped or [str(raw)]:
            c.drawString(x, y, line)
            y -= 4.2 * mm
    return y


def _cell(text: str, font: str, size: float = 8, align: int = 0) -> Paragraph:
    style = ParagraphStyle(
        "invCell",
        fontName=font,
        fontSize=size,
        leading=size + 2,
        alignment=align,
        textColor=colors.black,
    )
    safe = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return Paragraph(safe, style)


def build_invoice_pdf(
    *,
    invoice_number: str,
    buyer: dict[str, Any],
    items: list[dict[str, Any]],
    total_amount: float,
    invoice_date: date | None = None,
) -> bytes:
    """Повертає PDF-байти рахунку на оплату."""
    font = _register_font()
    seller = _seller()
    inv_date = invoice_date or date.today()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    left = 15 * mm
    right = width - 15 * mm
    table_w = right - left
    y = height - 16 * mm

    try:
        total_amount = float(total_amount or 0)
    except (TypeError, ValueError):
        total_amount = 0.0
    line_sum = items_total(items)
    if total_amount <= 0 and line_sum > 0:
        total_amount = line_sum

    c.setFillColor(colors.black)
    shown = _printed_invoice_number(invoice_number)
    title = f"Рахунок на оплату № {shown} від {_date_ua(inv_date)}"
    c.setFont(font, 14)
    c.drawString(left, y, title)
    y -= 8 * mm

    y = _draw_party_block(
        c,
        "Постачальник:",
        [
            seller["name"],
            f"п/р {seller['iban']} у банку {seller['bank']},",
            f"{seller['address']},",
            f"тел.: {seller['phone']},",
            f"код за ДРФО {seller['ipn']}, ІПН {seller['ipn']}",
        ],
        left,
        y,
        font,
        right,
    )
    y -= 2 * mm
    buyer_name = buyer.get("company_name") or buyer.get("name") or ""
    phone = str(buyer.get("phone") or "").strip()
    address = re.sub(r"\s+", " ", str(buyer.get("company_address") or buyer.get("address") or "")).strip()
    edrpou = str(buyer.get("edrpou") or "").strip()
    ipn_raw = re.sub(r"\D", "", str(buyer.get("ipn") or ""))
    ipn = ipn_raw if re.fullmatch(r"\d{10,12}", ipn_raw) and ipn_raw != re.sub(r"\D", "", edrpou) else ""
    buyer_values = [buyer_name]
    if address:
        buyer_values.append(address)
    code_line = f"Код за ЄДРПОУ {edrpou}" if edrpou else ""
    if ipn:
        code_line = f"{code_line}, ІПН {ipn}".strip(", ")
    if code_line:
        buyer_values.append(code_line)
    if phone:
        buyer_values.append(f"Тел.: {phone}")
    y = _draw_party_block(c, "Покупець:", buyer_values, left, y, font, right)

    y -= 3 * mm
    contract = str(buyer.get("contract") or "Рахунок").strip() or "Рахунок"
    contract_table = Table(
        [[_cell("Договір:", font, 9), _cell(contract, font, 9)]],
        colWidths=[28 * mm, 40 * mm],
    )
    contract_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("BACKGROUND", (0, 0), (0, 0), colors.Color(0.93, 0.93, 0.93)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    _, contract_h = contract_table.wrap(table_w, 20 * mm)
    contract_table.drawOn(c, left, y - contract_h)
    y -= contract_h + 5 * mm

    header = ["№", "Товари (роботи, послуги)", "Кіл-сть", "Од.", "Ціна", "Сума"]
    rows: list[list[Any]] = [[_cell(h, font, 8, TA_CENTER) for h in header]]
    for idx, item in enumerate(items or [{}], start=1):
        name = str(item.get("product") or item.get("name") or "Товар")
        qty = item.get("qty") or item.get("quantity") or 1
        amount = float(item.get("amount") or item.get("total_amount") or 0)
        try:
            unit = float(item.get("unit_price") or (amount / float(qty or 1)))
        except (TypeError, ValueError, ZeroDivisionError):
            unit = amount
        unit_name = str(item.get("unit") or "шт")
        rows.append(
            [
                _cell(str(idx), font, 8, TA_CENTER),
                _cell(name, font, 8),
                _cell(str(qty), font, 8, TA_CENTER),
                _cell(unit_name, font, 8, TA_CENTER),
                _cell(_money(unit), font, 8, TA_RIGHT),
                _cell(_money(amount), font, 8, TA_RIGHT),
            ]
        )
    col_w = [
        12 * mm,
        table_w - (12 + 22 + 14 + 24 + 28) * mm,
        22 * mm,
        14 * mm,
        24 * mm,
        28 * mm,
    ]
    goods = Table(rows, colWidths=col_w, repeatRows=1)
    goods.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.90, 0.90, 0.90)),
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    _, goods_h = goods.wrap(table_w, height)
    if y - goods_h < 55 * mm:
        c.showPage()
        y = height - 18 * mm
    goods.drawOn(c, left, y - goods_h)
    y -= goods_h + 6 * mm

    c.setFillColor(colors.black)
    c.setFont(font, 10)
    c.drawRightString(right, y, f"Всього: {_money(total_amount)}")
    y -= 6 * mm
    count = len(items or [])
    c.setFont(font, 9)
    c.drawString(
        left,
        y,
        f"Всього найменувань {count}, на суму {_money(total_amount)} грн.",
    )
    y -= 6 * mm
    _draw_bold_string(c, left, y, amount_in_words(total_amount), font, 11)
    y -= 18 * mm
    _draw_stamp_and_sign(
        c,
        left,
        max(18 * mm, y - 28 * mm),
        signer=_signer_name(seller["name"]),
        font=font,
    )

    c.showPage()
    c.save()
    return buf.getvalue()


def save_invoice_pdf(
    *,
    invoice_number: str,
    buyer: dict[str, Any],
    items: list[dict[str, Any]],
    total_amount: float,
    order_id: int | None = None,
) -> Path:
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in str(invoice_number) if ch.isalnum() or ch in "-_") or "invoice"
    name = f"invoice_{order_id or 'x'}_{safe}.pdf"
    path = INVOICES_DIR / name
    path.write_bytes(
        build_invoice_pdf(
            invoice_number=invoice_number,
            buyer=buyer,
            items=items,
            total_amount=total_amount,
        )
    )
    return path
