"""Багатокроковий сценарій консультанта Олега: консультація → CRM → НП → оплата."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ChatDocument, ChatMessageResponse
from app.database.models import ConversationState, Order, Client, DeliveryInfo, Payment, PaymentStatus
from app.services.ai_consultant import AIConsultant
from app.services.order_service import OrderService
from app.services.pricing_calculator import PricingCalculator, format_money, format_quote_message
from app.services.delivery_rules import (
    DELIVERY_TYPE_ADDRESS,
    DELIVERY_TYPE_POSTOMAT,
    DELIVERY_TYPE_WAREHOUSE,
    build_recipient_name,
    validate_delivery_payload,
)
from app.services import chat_history

logger = logging.getLogger(__name__)

consultant = AIConsultant()
calculator = PricingCalculator()
order_service = OrderService()

MANAGER_HANDOFF_REPLY = None  # динамічно з імені менеджера


MANAGER_WAIT_REPLY = (
    "Передав запит менеджеру — він звʼяжеться з вами найближчим часом."
)

SHORT_MANAGER_PHRASE = "Передам менеджеру — він дасть відповідь."

HOLOGRAM_INDIVIDUAL_REPLY = (
    "Такі прораховуємо індивідуально — передаю менеджеру, "
    "він напише вам точну вартість та термін виготовлення.\n"
    "Якщо маєте макет/зразок, відправте будь ласка — це пришвидшить прорахунок."
)

CARABINER_GUIDE_REPLY = (
    "У нас є різні види карабінів, переглянути та вибрати можете за посиланням нижче.\n"
    "https://silicon.com.ua/karabiny-metalevi"
)

CARABINER_BRANDING_REPLY = (
    "Друк логотипу робимо лише на сувенірних алюмінієвих карабінах 40×77 "
    "для ключів на застібці з фіксатором. Вартість і термін прораховуються індивідуально."
)

# Прайс на друк лого на карабінах з фіксатором (грн/шт за тиражами)
CARABINER_FIXATOR_TIERS = [
    (20, 54),
    (50, 54),
    (100, 48),
    (300, 42),
    (500, 42),
    (1000, 36),
]

# Чорні карабіни з фіксатором — 2 види (кожне посилання окремим повідомленням)
CARABINER_BLACK_FIXATOR_LINKS = [
    "https://silicon.com.ua/karabiny-metalevi/3330000045044",
    "https://silicon.com.ua/karabiny-metalevi/3330000045082",
]


def _carabiner_fixator_unit(qty: int) -> int:
    unit = CARABINER_FIXATOR_TIERS[0][1]
    for threshold, price in CARABINER_FIXATOR_TIERS:
        if qty >= threshold:
            unit = price
    return unit


def _carabiner_fixator_pricelist(qty: int | None = None) -> str:
    lines = [
        "Друк лого на сувенірних алюмінієвих карабінах 40×77 для ключів на застібці з фіксатором.",
        "Висота лого — більше 3,5 мм, довжина — до 50 мм.",
    ]
    if qty:
        unit = _carabiner_fixator_unit(int(qty))
        lines.append(
            f"На {qty} шт — {unit} грн/шт, разом {unit * int(qty)} грн."
        )
        lines.append("Строк виготовлення уточнюємо за завантаженням виробництва.")
    else:
        lines.append("Прайс (грн/шт):")
        for threshold, price in CARABINER_FIXATOR_TIERS:
            lines.append(f"{threshold} шт — {price}")
        lines.append("Напишіть тираж — порахую суму.")
    return "\n".join(lines)

MAT_COLOR_GUIDE_REPLY = (
    "Який колір килимка вам потрібен?\n"
    "Доступні кольори можете переглянути на сайті за посиланням нижче.\n"
    "https://silicon.com.ua/silikonovij-kilimok-dlya-vypichki"
)

FITNESS_BAND_UNIT_UAH = 239
FITNESS_BAND_URL = "https://silicon.com.ua/Rubber-bands-for-fitness-JK-RB01"
MAT_UNIT_UAH = 171
MAT_URL = "https://silicon.com.ua/silikonovij-kilimok-dlya-vypichki"


def _strip_escalate_marker(text: str) -> str:
    return re.sub(r"\s*\[ESCALATE\]\s*", "\n", text or "", flags=re.I).strip()


def _dedupe_manager_handoff_phrase(text: str) -> str:
    """Прибрати коротке «Передам менеджеру…», якщо вже є розгорнута передача."""
    raw = _strip_escalate_marker(text or "")
    if not raw:
        return raw
    short = SHORT_MANAGER_PHRASE
    if short not in raw:
        # також коротка форма без тире
        alt = "Передам менеджеру — він дасть відповідь"
        if alt not in raw:
            return raw
        short = alt
    body = raw.replace(short, "").strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip(" \n-—")
    if re.search(
        r"передам?\s+менеджер|передаю\s+менеджер|передав\s+запит\s+менеджер",
        body,
        re.I,
    ):
        return body
    return raw


# Розділювач «основне повідомлення» ↔ «додаткові повідомлення» всередині відповіді
# каталогу (щоб кожне посилання на товар пішло окремим повідомленням для прев'ю фото).
GUIDE_MSG_SEP = "\x1e"

# Відповідь на «Дякую» — чекаємо макет для погодження (на всі категорії товарів).
THANKS_VECTOR_REPLY = (
    "Чекаємо від вас лого у векторному форматі для підготовки макету "
    "на погодження та прорахунку вартості роботи."
)


def _split_guide(guide: str | None) -> tuple[str, list[str]]:
    """Розбити відповідь каталогу на основне повідомлення і список додаткових."""
    if not guide:
        return "", []
    parts = [p.strip() for p in guide.split(GUIDE_MSG_SEP)]
    parts = [p for p in parts if p]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _guide_response(guide: str, action: str = "consult", data: dict | None = None) -> ChatMessageResponse:
    main, extras = _split_guide(guide)
    return ChatMessageResponse(
        response=_finalize_bot_reply(main),
        action=action,
        data=data,
        extra_messages=extras or None,
    )


def _is_pure_thanks(text: str) -> bool:
    """«Дякую», «дякую вам», «спасибі» без іншого запиту/контексту оплати."""
    lower = (text or "").strip().lower().strip(" .!)(—-")
    if not lower:
        return False
    # не чіпати «дякую, оплатив», «дякую, а скільки…», питання тощо
    if any(w in lower for w in ("оплат", "сплат", "квитанц", "рахунок", "скрін", "скрин")):
        return False
    if "?" in (text or ""):
        return False
    thanks_words = ("дякую", "дякyю", "спасибі", "спасибо", "дяк", "thanks", "thank you", "вдячн")
    if not any(w in lower for w in thanks_words):
        return False
    # прибрати слова подяки й подивитись, чи лишилось щось змістовне
    rest = lower
    for w in ("дуже", "вам", "тобі", "велике", "за", "допомог", "консультац", "інформац",
              "дякую", "спасибі", "спасибо", "дяк", "thanks", "thank you", "вдячний", "вдячна", "ок", "окей", "добре"):
        rest = rest.replace(w, " ")
    rest = re.sub(r"[^\wа-яїієґ]+", " ", rest, flags=re.I).strip()
    return len(rest) <= 2


def _sum_qty(text: str) -> int:
    """Сумарна кількість з усіх згадок «N шт» у повідомленні (2 шт + 3 шт = 5)."""
    lower = (text or "").lower()
    found = [
        int(m)
        for m in re.findall(r"(?<![xх×\d])(\d{1,6})\s*(?:шт\.?|штук[ауи]?|pcs)\b", lower)
    ]
    return sum(found)


def _finalize_bot_reply(text: str) -> str:
    cleaned = _dedupe_manager_handoff_phrase(_strip_escalate_marker(text or ""))
    return cleaned.replace(GUIDE_MSG_SEP, "\n").strip()


async def _soft_manager_handoff(
    db: AsyncSession,
    chat_id: str,
    text: str,
    reply: str,
    data: dict[str, Any] | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> ChatMessageResponse:
    """Тікет менеджеру без вимкнення AI (бот далі відповідає на нові питання)."""
    ticket = await chat_history.create_manager_ticket(
        db, chat_id, text, mute_bot=False
    )
    payload = {"ticket_id": ticket.id}
    if extra:
        payload.update(extra)
    return ChatMessageResponse(
        response=_finalize_bot_reply(reply or MANAGER_WAIT_REPLY),
        action="manager_handoff",
        data=payload,
    )


def _is_lanyard_hardware_spec(text: str) -> bool:
    """«20 мм з друком лого на 2 карабіни, 100 шт» — фурнітура шнурка, не окремий карабін."""
    lower = (text or "").lower()
    if any(w in lower for w in ("фіксатор", "для ключ", "сувенірн", "40×77", "40x77", "40х77")):
        return False
    if not any(w in lower for w in ("карабін", "карабин")):
        return False
    has_width = bool(re.search(r"\b(10|15|20|25|30|40)\s*мм", lower))
    has_print = any(w in lower for w in ("лого", "друк", "друком"))
    has_qty = bool(re.search(r"\d+\s*(?:шт|штук)", lower))
    return bool(has_width and (has_print or has_qty or "шнур" in lower))


def _is_lanyard_fastening_choice(text: str) -> bool:
    """«2 карабіни металеві без аксесуарів» після ширини стрічки — кріплення шнурка."""
    lower = (text or "").lower()
    if any(w in lower for w in ("фіксатор", "для ключ", "сувенірн", "40×77", "40x77", "40х77")):
        return False
    if _extract_qty(text) and _is_lanyard_hardware_spec(text):
        return False
    if re.search(
        r"двом[аи]?\s+карабін|2\s*карабін|два\s+карабін|"
        r"один(?:им|ого)?\s+карабін|1\s*карабін|з\s+петл|петля",
        lower,
    ):
        return True
    return bool("карабін" in lower and "без" in lower and "аксесуар" in lower)


def _lanyard_context_ready(data: dict[str, Any] | None) -> bool:
    data = data or {}
    blob = str(
        data.get("product_type")
        or data.get("last_topic")
        or data.get("pending_product_type")
        or ""
    )
    qty = str(data.get("quantity") or "").strip()
    size = str(data.get("size_mm") or "").strip()
    return _product_family(blob) == "lanyard" and bool(qty) and bool(size)


def _lanyard_fastening_calc(text: str, data: dict[str, Any] | None) -> dict | None:
    """Після ширини і типу стрічки клієнт обирає 1 чи 2 карабіни — рахуємо збережений тираж."""
    data = data or {}
    if not _is_lanyard_fastening_choice(text) or not _lanyard_context_ready(data):
        return None
    qty_raw = re.sub(r"\D", "", str(data.get("quantity") or ""))
    qty = int(qty_raw or "0")
    size = str(data.get("size_mm") or "")
    if qty < 1 or not size:
        return None
    ribbon = str(data.get("lanyard_ribbon") or "")
    combined = f"шнурки для бейджа {size} мм {ribbon} {qty} шт {text}"
    return _quote_calculate(
        "шнурки для бейджів",
        qty,
        size_mm=size,
        text=combined,
        data=data,
    )


def _is_standalone_carabiner_query(text: str) -> bool:
    lower = (text or "").lower()
    if not any(w in lower for w in ("карабін", "карабин")):
        return False
    # фурнітура шнурка / брелока — не каталог металевих карабінів
    if any(w in lower for w in ("шнур", "бейдж", "брелок", "ретрактор", "стрічк")):
        return False
    if _is_lanyard_hardware_spec(lower):
        return False
    return True


def _wants_branding(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "бренд",
            "логотип",
            "лого ",
            " з лого",
            "друк",
            "нанесен",
            "друком",
        )
    )


def _names_other_product(lower: str, own: str) -> bool:
    """Чи згадано в повідомленні інший конкретний товар (не з сімейства `own`)."""
    groups = {
        "mat": ("килим", "випічк"),
        "fitness": ("резинк", "резинок", "фітнес", "fitness", "jk-rb"),
        "carabiner": ("карабін", "карабин"),
        "hologram": ("голограм",),
        "cardholder": ("кардхолдер", "кард-холдер", "card holder", "картхолдер"),
        "pocket": ("кишеньк", "кишеня"),
        "lanyard": ("шнур",),
        "brelok": ("брелок",),
        "cup": ("чашк", "екочаш"),
        "cap": ("шапоч", "плаванн"),
        "retractor": ("ретрактор", "кліп рулетк", "витяжн"),
        "slap": ("слеп", "slap"),
        "tyvek": ("tyvek", "контрольн"),
        "rfid": ("rfid", "рфід", "чіп"),
        "reflective": ("світловідб", "відбивн", "рефлект"),
        "fabric": ("тканин",),
        "coster": ("костер", "підставк"),
        "bracelet": ("браслет",),
    }
    for fam, kws in groups.items():
        if fam == own:
            continue
        if any(k in lower for k in kws):
            return True
    return False


CARDHOLDER_GUIDE_REPLY = (
    "Кардхолдери вам потрібно з друком лого чи без?\n"
    "Який колір та скільки штук?"
)

CARDHOLDER_PLASTIC_UNIT = 38
CARDHOLDER_PLASTIC_COLOR_LINKS = [
    "https://silicon.com.ua/kardholderi/3330000045396",
    "https://silicon.com.ua/kardholderi/3330000045372",
    "https://silicon.com.ua/kardholderi/3330000045334",
    "https://silicon.com.ua/kardholderi/3330000045358",
    "https://silicon.com.ua/kardholderi/kardholder-plastikoviy-chorniy-55-85mm",
]

BADGE_POCKET_GUIDE = (
    "Виберіть будь ласка розмір прозорих кишеньок, який вам потрібно:\n"
    "Кишенька для бейджа 65×115 мм\n"
    "Кишенька для бейджа 100×85 мм\n"
    "Кишенька для бейджа 83×135 мм\n"
    "Кишенька для бейджа 115×100 мм\n"
    "Кишенька для бейджа 96×150 мм\n"
    "Кишенька для бейджа А6 118×182 мм\n"
    "Кишенька для бейджа А5 162×242 мм"
)

BADGE_POCKET_PRICES: list[tuple[tuple[str, str], int, str]] = [
    (("65", "115"), 12, "Кишенька для бейджа 65×115 мм"),
    (("100", "85"), 12, "Кишенька для бейджа 100×85 мм"),
    (("83", "135"), 12, "Кишенька для бейджа 83×135 мм"),
    (("115", "100"), 12, "Кишенька для бейджа 115×100 мм"),
    (("96", "150"), 15, "Кишенька для бейджа 96×150 мм"),
    (("118", "182"), 16, "Кишенька для бейджа А6 118×182 мм"),
    (("162", "242"), 22, "Кишенька для бейджа А5 162×242 мм"),
]


def _match_badge_pocket(text: str) -> tuple[tuple[str, str], int, str] | None:
    lower = (text or "").lower()
    if re.search(r"\ba6\b", lower) or "118" in lower:
        return BADGE_POCKET_PRICES[5]
    if re.search(r"\ba5\b", lower) or "162" in lower:
        return BADGE_POCKET_PRICES[6]
    dims = re.findall(r"(\d+)\s*[*xх×]\s*(\d+)", lower)
    for a, b in dims:
        for pair, price, name in BADGE_POCKET_PRICES:
            if {a, b} == set(pair):
                return pair, price, name
    return None

LANYARD_COLORS_REPLY = (
    "У наявності шнурки для бейджа\n"
    "по ширині стрічки — 10 мм, 15 мм, 20 мм\n"
    "з одним карабіном та з двома.\n"
    "Напишіть ширину і з одним карабіном чи з двома — надішлю посилання на товар."
)

LANYARD_PRINT_GUIDE = (
    "З друком логотипу є доступні розміри стрічки по ширині:\n"
    "15 мм, 20 мм, 25 мм, 30 мм, 40 мм — гладенька стрічка\n"
    "15 мм, 20 мм, 25 мм — репсова стрічка\n"
    "Строк виготовлення близько 10 робочих днів.\n"
    "Вам потрібна гладенька стрічка чи репсова, в якій ширині та скільки штук?"
)

LANYARD_FASTENING_GUIDE = (
    "Для цієї стрічки доступні кріплення:\n"
    "карабін металевий, петля, карабін чорний, карабін на застібці без фіксатора, карабін чорний сумочний.\n"
    "Вам потрібно з одним карабіном чи з двома?\n"
    "Додатково: роз'єм безпеки, фастекс, ретрактор з витяжним тросиком.\n"
    "Ретрактори пришивні — білий, чорний, синій, блакитний, жовтий, зелений, помаранчевий, червоний.\n"
    "https://silicon.com.ua/retraktori-dlya-beydzhiv\n"
    "Ретрактор чорний з кільцем та кліпсою — "
    "https://silicon.com.ua/retraktori-dlya-beydzhiv/retraktor-dlya-beydzha-z-kiltsem-ta-klipsoyu"
)

PHONE_ACCESSORY_GUIDE = (
    "Аксесуари для мобільних телефонів є в різних розмірах і під різні моделі. "
    "Напишіть, будь ласка, який саме товар, розмір/модель і кількість."
)

LANYARD_LINK_BY_WIDTH = {
    "10": "https://silicon.com.ua/shnurky-dlya-beydzhiv",
    "15": "https://silicon.com.ua/shnurky-dlya-beydzhiv",
    "20": "https://silicon.com.ua/shnurky-dlya-beydzhiv",
}

SILICONE_BRACELET_SIZE_GUIDE = (
    "У нас є три розміри силіконових браслетів — по ширині (5 мм, 12 мм, 20 мм) "
    "та по окружності (202 мм — дорослий, 180 мм — підлітковий, 160 мм — дитячий).\n"
    "Переглянути та вибрати розмір і колір можете за посиланнями нижче:\n"
    "Дорослий (202 мм):\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-brasleti-2025-mm\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-brasleti-202122-mm\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-brasleti-2022-mm\n"
    "Підлітковий (180 мм):\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-brasleti-18052-mm\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-braslety_180_12\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-braslety_180_20\n"
    "Дитячий (160 мм):\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-braslety_160_5\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-brasleti-dityachi-160122-mm\n"
    "https://silicon.com.ua/silikonovi-braslety/silikonovi-braslety_160_20\n"
    "Напишіть, будь ласка, ширину, окружність і тираж — порахую вартість."
)


def _is_generic_silicone_bracelet_query(lower: str) -> bool:
    """«силіконові браслети» без ширини/окружності/тиражу → направляюче питання."""
    if "силіконов" not in lower or "браслет" not in lower:
        return False
    if _product_family(lower) != "silicone":
        return False
    if _extract_qty(lower):
        return False
    if re.search(r"\b(5|12|20)\s*мм", lower):
        return False
    if re.search(r"\b(202|180|160)\b", lower):
        return False
    if re.search(r"сегмент|сигмент", lower):
        return False
    # питання про колір — не підміняти палітру таблицею розмірів
    if any(w in lower for w in ("колір", "кольор", "палітр", "pantone")):
        return False
    return True


def _wants_lanyard_stock(lower: str) -> bool:
    """Питання саме про наявні шнурки, не про друк лого."""
    if any(w in lower for w in ("з лого", "логотип", "друк", "друком", "бренд")):
        return False
    if "не" in lower and "наявн" in lower:
        return False
    return any(w in lower for w in ("наявн", "які кольори", "який колір", "яких кольор", "у вас є"))


def _wants_lanyard_print(lower: str) -> bool:
    if _wants_lanyard_stock(lower):
        return False
    return any(
        w in lower
        for w in (
            "лого",
            "друк",
            "друком",
            "бренд",
            "варт",
            "цін",
            "термін",
            "строк",
            "виготов",
            "гладеньк",
            "репсов",
        )
    ) or ("не" in lower and "наявн" in lower)


def _mentioned_families(text: str) -> list[str]:
    """Усі товари в повідомленні, у порядку згадки."""
    lower = (text or "").lower()
    specs = (
        ("pocket", r"кишеньк\w*|кишен[яі]\w*"),
        ("cardholder", r"кардхолдер\w*|картхолдер\w*"),
        ("lanyard", r"шнур\w*"),
        ("brelok", r"брелок\w*"),
        ("cups", r"еко-?чаш\w*|чашк\w*"),
        ("ring", r"силіконов\w*\s+кільц\w*|кільц\w*"),
        ("hologram", r"голограм\w*"),
        ("phone", r"аксесуар\w*\s+для\s+моб\w*|чохол\w*"),
        ("mat", r"килим\w*"),
        ("carabiner", r"карабін\w*|карабин\w*"),
        ("silicone", r"силіконов\w*(?:\s+браслет\w*)?"),
        ("reflective", r"світловідб\w*|відбивн\w*"),
        ("tyvek", r"tyvek|контрольн\w*"),
        ("slap", r"slap|слеп\w*"),
        ("caps", r"шапоч\w*"),
        ("retractor", r"ретрактор\w*"),
    )
    found: list[tuple[int, str]] = []
    for fam, pat in specs:
        m = re.search(pat, lower)
        if m:
            found.append((m.start(), fam))
    found.sort()
    out: list[str] = []
    for _pos, fam in found:
        if fam in out:
            continue
        if fam == "carabiner" and ("lanyard" in out or _is_lanyard_hardware_spec(lower) or "шнур" in lower):
            continue
        if fam == "silicone" and "ring" in out:
            continue
        if fam == "silicone" and "браслет" not in lower and "силікон" not in lower:
            continue
        if fam == "ring" and "браслет" in lower and "кільц" not in lower:
            continue
        out.append(fam)
    if _is_lanyard_hardware_spec(lower) and "lanyard" not in out:
        out.append("lanyard")
    return out


def _lanyard_section_reply(
    text: str,
    *,
    topic_is_lanyard: bool = False,
    data: dict[str, Any] | None = None,
) -> str | None:
    lower = (text or "").lower()
    if _is_quote_correction(text) or _wants_remove_from_cart(text):
        return None
    if _is_lanyard_fastening_choice(text) and _lanyard_context_ready(data):
        return None
    about = "шнур" in lower or topic_is_lanyard or _is_lanyard_hardware_spec(lower)
    if not about or any(w in lower for w in ("кишеньк", "кишеня", "ретрактор")):
        return None
    qty = _extract_qty(text)
    width = re.search(r"\b(10|15|20|25|30|40)\s*мм", lower)
    two = bool(re.search(r"двом[аи]?\s+карабін|2\s*карабін|два\s+карабін", lower))
    ribbon = "гладеньк" in lower or "репсов" in lower
    if width and qty and (two or not ribbon):
        return None
    if width and qty and ribbon and not two:
        return LANYARD_FASTENING_GUIDE
    if _wants_lanyard_stock(lower) and not _wants_lanyard_print(lower):
        return LANYARD_COLORS_REPLY
    if topic_is_lanyard or "шнур" in lower or _wants_lanyard_print(lower) or _is_lanyard_hardware_spec(lower):
        return LANYARD_PRINT_GUIDE
    return None


def _catalog_guide_reply(text: str, data: dict[str, Any] | None = None) -> str | None:
    """Готові відповіді по позиціях з сайту (карабіни / килимки / резинки / голограми)."""
    data = data or {}
    lower = (text or "").lower()
    qty = _extract_qty(text)
    topic = str(
        data.get("last_topic")
        or data.get("product_type")
        or data.get("pending_product_type")
        or ""
    ).lower()
    # Не дати старому topic перехопити запит про ІНШИЙ товар
    # (напр. запит про кардхолдери після килимків не має давати відповідь по килимках).
    topic_fam = _product_family(topic) if topic else ""
    if topic and _names_other_product(lower, topic_fam):
        topic = ""
    # Названий у цьому повідомленні товар важливіший за попередній фокус.
    incoming = _extract_product_hint(text)
    incoming_fam = _product_family(incoming or "")
    if incoming_fam and incoming_fam not in {"", "other"}:
        if _product_family(topic) != incoming_fam:
            topic = (incoming or "").lower()

    fams = _mentioned_families(text)
    if len(fams) >= 2 and not qty:
        parts: list[str] = []
        for fam in fams:
            if fam == "silicone":
                parts.append(
                    "Силіконові браслети з лого. Строк 5–7 робочих днів.\n"
                    + SILICONE_BRACELET_SIZE_GUIDE
                )
            elif fam == "lanyard":
                parts.append(
                    "Шнурки для бейджа з лого:\n"
                    + (_lanyard_section_reply(text, data=data) or LANYARD_PRINT_GUIDE)
                )
            elif fam == "cups":
                parts.append("Еко-чашки: 350 мл або 450 мл, стакан сірий чи кремовий. Який об'єм і тираж?")
            elif fam == "brelok":
                parts.append(
                    "Брелоки з лого: стрічка 20 мм або 25 мм. Яка ширина, який варіант кріплення і тираж?"
                )
            elif fam == "pocket":
                parts.append(BADGE_POCKET_GUIDE)
            elif fam == "cardholder":
                parts.append(CARDHOLDER_GUIDE_REPLY)
            elif fam == "ring":
                parts.append("Силіконові кільця є в різних розмірах. Який внутрішній діаметр (мм) і тираж?")
            elif fam == "hologram":
                parts.append("Голограми: який розмір, тираж і чи є макет?")
            elif fam == "phone":
                parts.append(PHONE_ACCESSORY_GUIDE)
            elif fam == "mat":
                parts.append("Килимки для випічки: який колір і скільки штук?")
            elif fam == "carabiner":
                parts.append("Карабіни металеві — окремий товар. Який вид з каталогу і скільки штук?")
        if len(parts) >= 2:
            return "\n\n".join(parts)

    # --- прозорі кишеньки для бейджа (категорія кардхолдери) ---
    if (
        any(w in lower for w in ("кишеньк", "кишеня", "кишені"))
        or "кишеньк" in topic
        or _product_family(lower) == "pocket"
    ):
        if _match_badge_pocket(text) and qty:
            return None
        if _match_badge_pocket(text):
            _dims, price, name = _match_badge_pocket(text)
            return f"{name} — {price} грн/шт. Напишіть кількість — порахую суму."
        return BADGE_POCKET_GUIDE

    # --- кардхолдери ---
    if any(w in lower for w in ("кардхолдер", "кард-холдер", "картхолдер", "card holder")) or (
        "кардхолдер" in topic
    ):
        if any(w in lower for w in ("колір", "кольор", "палітр")):
            intro = (
                "Пластикові кардхолдери 55×85 мм — кольори на сайті. "
                "Нижче кожне посилання окремим повідомленням, щоб у Telegram було фото:"
            )
            return GUIDE_MSG_SEP.join([intro, *CARDHOLDER_PLASTIC_COLOR_LINKS])
        if qty and (_wants_branding(text) or "з друком" in lower):
            return (
                "Кардхолдери з друком лого — вартість за прайсом після макету. "
                "Надішліть лого у векторі (AI, EPS, SVG, PDF). Передам менеджеру прорахувати."
            )
        if qty and any(w in lower for w in ("без друку", "без лого")):
            total = CARDHOLDER_PLASTIC_UNIT * int(qty)
            return (
                f"Кардхолдер пластиковий 55×85 мм без друку — {CARDHOLDER_PLASTIC_UNIT} грн/шт. "
                f"На {qty} шт — {total} грн."
            )
        return CARDHOLDER_GUIDE_REPLY

    # --- силіконові браслети без уточнень → направляюче питання по розмірах ---
    if _is_generic_silicone_bracelet_query(lower):
        return SILICONE_BRACELET_SIZE_GUIDE

    # --- шнурки: друк лого (розмір стрічки) або наявність, не каталог карабінів ---
    lan_topic = _product_family(topic) == "lanyard" or "шнур" in topic
    lan_reply = _lanyard_section_reply(text, topic_is_lanyard=lan_topic, data=data)
    if lan_reply:
        return lan_reply

    # --- брелоки без ширини ---
    if _product_family(lower) == "brelok" and not re.search(r"\b(20|25)\s*мм", lower) and not qty:
        return (
            "Брелоки з лого: стрічка 20 мм або 25 мм, з карабіном / чорним карабіном / "
            "кільцем / кольоровим карабіном. Яка ширина і який варіант?"
        )

    # --- екочашки без об'єму ---
    if _product_family(lower) == "cups" and "350" not in lower and "450" not in lower and not qty:
        return "Еко-чашки 350 мл або 450 мл, стакан сірий чи кремовий. Який об'єм і тираж?"

    # --- кільця ---
    if _product_family(lower) == "ring" and not re.search(r"\d+\s*мм", lower) and not qty:
        return "Силіконові кільця є в різних розмірах. Який внутрішній діаметр (мм) і тираж?"

    # --- аксесуари для телефонів ---
    if _product_family(lower) == "phone" and not qty:
        return PHONE_ACCESSORY_GUIDE

    # --- резинки для фітнесу ---
    if any(w in lower for w in ("резинк", "резинок", "фітнес", "fitness", "jk-rb")) or (
        "резин" in topic and ("фітнес" in topic or "fitness" in topic)
    ):
        parts: list[str] = []
        if qty:
            total = FITNESS_BAND_UNIT_UAH * int(qty)
            parts.append(
                f"Резинки для фітнесу SILICON Resistance Loop Bands JK-RB01 (набір 5 в 1): "
                f"{FITNESS_BAND_UNIT_UAH} грн/шт. На {qty} шт — {total} грн."
            )
        else:
            parts.append(
                f"Резинки для фітнесу SILICON Resistance Loop Bands JK-RB01 (набір 5 в 1) — "
                f"{FITNESS_BAND_UNIT_UAH} грн/шт."
            )
        parts.append(f"Посилання: {FITNESS_BAND_URL}")
        if not qty:
            parts.append("Яка кількість потрібна?")
        if _wants_branding(text):
            parts.append(
                "Робимо друк логотипу на резинках для фітнесу.\n"
                "Для прорахунку вартості роботи та терміну потрібен макет у векторному "
                "форматі — ai, eps, svg, pdf.\n"
                "Термін виготовлення 7–9 робочих днів."
            )
        return "\n".join(parts)

    # --- килимки для випічки ---
    if "килим" in lower or "випічк" in lower or "килим" in topic:
        color_known = bool(
            re.search(
                r"\b(біл|чорн|червон|зелен|жовт|сір|сир|фіолет|оранж|прозор|"
                r"бірюз|салат|cyan|black|white)\w*",
                lower,
            )
        )
        if not color_known:
            return MAT_COLOR_GUIDE_REPLY
        total_qty = _sum_qty(text) or (int(qty) if qty else 0)
        if total_qty:
            total = MAT_UNIT_UAH * total_qty
            return (
                f"Силіконовий килимок для випічки 280×380 мм — {MAT_UNIT_UAH} грн/шт. "
                f"На {total_qty} шт — {total} грн.\n"
                f"Кольори та наявність: {MAT_URL}"
            )
        return (
            f"Силіконовий килимок для випічки 280×380 мм — {MAT_UNIT_UAH} грн/шт.\n"
            f"{MAT_URL}\n"
            "Яка кількість потрібна?"
        )

    # Кріплення вже названого шнурка — не каталог металевих карабінів
    if _is_lanyard_fastening_choice(text) and _lanyard_context_ready(data):
        return None

    # --- металеві карабіни (окремий товар, не фурнітура шнурка) ---
    if _is_standalone_carabiner_query(text):
        has_fixator = any(w in lower for w in ("фіксатор", "застібц", "застежк"))
        is_black = "чорн" in lower
        wants_price = _wants_branding(text) or any(
            w in lower for w in ("ціна", "варт", "кошту", "прайс", "прорахун", "скільки")
        )
        # Чорний карабін з фіксатором — 2 види; кожне посилання окремим повідомленням
        # (у Telegram разом в одному повідомленні прев'ю з фото не відображається).
        if is_black and has_fixator:
            main = (
                "У нас є 2 види чорних карабінів з фіксатором:\n"
                "1) Сувенірний алюмінієвий карабін чорний 40×77 для ключів на застібці з фіксатором\n"
                "2) Сувенірний алюмінієвий карабін чорно-помаранчевий 40×77 для ключів на застібці з фіксатором\n"
                "Напишіть, будь ласка, який вам потрібен:"
            )
            if wants_price:
                main = f"{main}\n{_carabiner_fixator_pricelist(int(qty) if qty else None)}"
            return GUIDE_MSG_SEP.join([main, *CARABINER_BLACK_FIXATOR_LINKS])
        # Друк лого на карабіні з фіксатором — прайс
        if has_fixator and wants_price:
            return _carabiner_fixator_pricelist(int(qty) if qty else None)
        # Загальний запит — направляюче питання + категорія
        parts = [CARABINER_GUIDE_REPLY]
        if not qty:
            parts.append(
                "Після вибору певного виду карабіну чи карабінів уточніть, будь ласка, кількість."
            )
        else:
            parts.append(f"Кількість прийняв: {qty} шт. Який саме вид з каталогу беремо?")
        if _wants_branding(text):
            parts.append(CARABINER_BRANDING_REPLY)
        return "\n".join(parts)

    # --- голограми ---
    about_hologram = "голограм" in lower or "голограм" in topic
    if about_hologram:
        # «логотип» у загальному питанні ще не деталі прорахунку
        has_details = bool(
            qty
            or re.search(r"\d+\s*мм", lower)
            or any(w in lower for w in ("коло", "круг", "макет", "чорн", "квадрат", "овал"))
        )
        if has_details:
            accepted = ""
            bits = ["голограми"]
            size_m = re.search(r"(\d+)\s*мм", lower)
            if size_m:
                bits.append(
                    f"коло {size_m.group(1)} мм"
                    if "коло" in lower or "круг" in lower
                    else f"{size_m.group(1)} мм"
                )
            if "чорн" in lower:
                bits.append("чорний логотип")
            if qty:
                bits.append(f"тираж {qty}")
            if len(bits) > 1:
                accepted = f"Прийняв: {', '.join(bits)}.\n"
            return f"{accepted}{HOLOGRAM_INDIVIDUAL_REPLY}"
        return (
            "Голограми з логотипом прораховуємо індивідуально — залежить від розміру, "
            "тиражу та складності макету. Надішліть бажаний розмір, кількість і макет/зразок, "
            "якщо є — зорієнтую по ціні та термінах."
        )

    return None


def manager_greeting_text(name: str | None) -> str:
    display = (name or "").strip() or "менеджер"
    return (
        f"Вітаю!\n"
        f"Мене звати {display}, я менеджер компанії Silicon.\n"
        f"Чим я можу вам допомогти?"
    )


async def _manager_greeting(db: AsyncSession) -> str:
    """Залишено для сумісності; ім'я береться з активного менеджера/власника."""
    from app.database.models import AdminUser

    result = await db.execute(
        select(AdminUser)
        .where(AdminUser.is_active == True, AdminUser.role == "manager")  # noqa: E712
        .order_by(AdminUser.id.desc())
        .limit(1)
    )
    manager = result.scalar_one_or_none()
    if not manager:
        result = await db.execute(
            select(AdminUser).where(AdminUser.is_active == True, AdminUser.role == "owner").limit(1)  # noqa: E712
        )
        manager = result.scalar_one_or_none()
    name = (manager.full_name if manager and manager.full_name else None) or "менеджер"
    return manager_greeting_text(name)

GREETING = (
    "Вітаю! Мене звати Олег, я менеджер компанії Silicon. "
    "Чим можу вам допомогти?"
)


def _current_greeting() -> str:
    from app.services.bot_settings import get_greeting_sync

    return get_greeting_sync() or GREETING


def _ai_live() -> bool:
    """GPT у проді: відповідає першим. У тестах (mock) лишаються шаблони."""
    return consultant.openai_live()

ORDER_PROMPT = "Ок, оформлюємо. Напишіть ім'я та прізвище одним повідомленням."

ORDER_CONFIRM_PROMPT = (
    "Оформлюємо? Якщо треба ще позицію в це замовлення — напишіть яка і скільки."
)

KNOWN_CITIES = {
    "київ": "Київ",
    "киев": "Київ",
    "львів": "Львів",
    "львов": "Львів",
    "одеса": "Одеса",
    "одесса": "Одеса",
    "харків": "Харків",
    "харьков": "Харків",
    "дніпро": "Дніпро",
    "днепр": "Дніпро",
    "запоріжжя": "Запоріжжя",
    "винниця": "Вінниця",
    "вінниця": "Вінниця",
    "житомир": "Житомир",
    "полtava": "Полтава",
    "чернігів": "Чернігів",
    "чернигов": "Чернігів",
    "суми": "Суми",
    "рівне": "Рівне",
    "ровно": "Рівне",
    "тернопіль": "Тернопіль",
    "івано-франківськ": "Івано-Франківськ",
    "луцьк": "Луцьк",
    "ужгород": "Ужгород",
    "хрещатик": "Хрещатик",
}

DELIVERY_TYPE_PROMPT = (
    "Куди зручніше відправити Новою поштою — на відділення, у поштомат чи за адресою?"
)

WAREHOUSE_PROMPT = "Який номер відділення чи поштомату?"

ADDRESS_PROMPT = "Напишіть, будь ласка, повну адресу доставки."

CRM_FIELD_QUESTIONS = {
    "person_name": "Напишіть ім'я та прізвище одним повідомленням.",
    "phone": "Напишіть свій номер телефону, щоб ми могли з вами зв'язатися.",
    "product_type": "Який саме товар беремо?",
    "quantity": "Яка кількість потрібна?",
}

DELIVERY_FIELD_ORDER = [
    ("recipient_last_name", "Прізвище отримувача?"),
    ("recipient_first_name", "А ім'я отримувача?"),
    ("recipient_patronymic", "І по батькові, будь ласка?"),
    ("recipient_phone", "Телефон отримувача?"),
    ("city", "У яке місто відправляємо?"),
    ("warehouse", "Який номер відділення чи поштомату?"),
    ("street_address", "Напишіть, будь ласка, повну адресу?"),
]

PAYMENT_PROMPT = "Карткою онлайн, рахунок для ФОП/ТОВ чи готівкою в офісі?"

INVOICE_PROMPT = (
    "Для підготовки рахунку напишіть будь ласка реквізити платника\n"
    "Назва компанії:\n"
    "код ЄДРПОУ:\n"
    "ІПН:\n"
    "Індекс та адреса реєстрації:\n"
    "Або ж відправте у форматі pdf чи фото"
)


def _has_invoice_word(lower: str) -> bool:
    """«рахунок», але не «прорахунок»."""
    return bool(re.search(r"(?<![а-яіїєґa-z])рахун", lower or ""))


def _wants_invoice_requisites(text: str) -> bool:
    """Клієнт просить рахунок, а не картку і не готівку."""
    lower = (text or "").lower()
    if any(w in lower for w in ("готівк", "кеш", "liqpay", "лікпей", "картк", "наклад", "налож", "післяплат")):
        return False
    if not _has_invoice_word(lower) and "безготів" not in lower:
        return False
    if "iban" in lower and not _has_invoice_word(lower):
        return False
    if any(w in lower for w in ("як оплат", "які способ", "яка оплат", "спосіб оплат", "варіант оплат")):
        return False
    if any(
        w in lower
        for w in (
            "підготу",
            "на оплат",
            "будь ласка",
            "вистав",
            "на реквізит",
            "безготів",
            "за рахунком",
            "для фоп",
            "для тов",
            "потріб",
            "хочу",
            "прошу",
            "зробіть",
            "зробити",
            "надішл",
            "скинь",
        )
    ):
        return True
    compact = lower.strip().strip(".!?")
    return compact in {"рахунок", "pdf", "пдф", "рахунок pdf"}

EDRPOU_WHERE_REPLY = (
    "Код ЄДРПОУ є у виписці з реєстру або в договорі: "
    "8 цифр для ТОВ, 10 для ФОП. Надішліть його сюди, як є."
)

THANKS = "Замовлення прийняв. Якщо щось ще потрібно — пишіть."

REAL_MANAGER_REPLY = "Ні. Я реальний менеджер."


async def _get_state(db: AsyncSession, chat_id: str) -> ConversationState:
    result = await db.execute(select(ConversationState).where(ConversationState.chat_id == chat_id))
    state = result.scalar_one_or_none()
    if state:
        return state
    state = ConversationState(chat_id=chat_id, step="idle", data_json="{}")
    db.add(state)
    await db.flush()
    return state


def _load_data(state: ConversationState) -> dict[str, Any]:
    try:
        return json.loads(state.data_json or "{}")
    except json.JSONDecodeError:
        return {}


def _save_data(state: ConversationState, data: dict[str, Any]) -> None:
    state.data_json = json.dumps(data, ensure_ascii=False)


def _parse_kv(text: str) -> dict[str, str]:
    """Парсить 'Ключ: значення' рядки та вільний текст з телефоном/email."""
    mapping = {
        "прізвище": "last_name",
        "фамилия": "last_name",
        "ім'я": "first_name",
        "імя": "first_name",
        "имя": "first_name",
        "по батькові": "patronymic",
        "по батькови": "patronymic",
        "отчество": "patronymic",
        "телефон": "phone",
        "phone": "phone",
        "email": "email",
        "емейл": "email",
        "пошта": "email",
        "місто": "city",
        "город": "city",
        "товар": "product_type",
        "кількість": "quantity",
        "количество": "quantity",
        "логотип": "logo",
        "коментар": "comment",
        "комментарий": "comment",
        "відділення": "warehouse",
        "отделение": "warehouse",
        "поштомат": "warehouse",
        "адреса": "street_address",
        "адрес": "street_address",
        "адреса організації": "company_address",
        "юридична адреса": "company_address",
        "компанія": "company_name",
        "компания": "company_name",
        "назва": "company_name",
        "назва організації": "company_name",
        "єдрпоу": "edrpou",
        "едрпоу": "edrpou",
        "edrpou": "edrpou",
        "іпн": "ipn",
        "ипн": "ipn",
        "інн": "ipn",
        "инн": "ipn",
        "ipn": "ipn",
        "iban": "bank_account",
        "р/р": "bank_account",
        "банк": "bank_name",
    }
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line and "—" not in line and "-" not in line[1:]:
            continue
        sep = ":" if ":" in line else ("—" if "—" in line else "-")
        key_raw, val = line.split(sep, 1)
        key = key_raw.strip().lower()
        val = val.strip()
        for uk, en in mapping.items():
            if uk in key:
                out[en] = val
                break
    phone = re.search(r"(\+?380\d{9}|\+?0\d{9})", text.replace(" ", ""))
    if phone and "phone" not in out:
        out["phone"] = phone.group(1)
    email = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if email and "email" not in out:
        out["email"] = email.group(0)
    return out


def _missing_crm_fields(data: dict[str, Any]) -> list[str]:
    """Контакти: ім'я+прізвище разом, телефон. Місто — на кроці доставки."""
    _sync_cart_fields(data)
    order: list[str] = []
    if not (data.get("first_name") and data.get("last_name")):
        order.append("person_name")
    if not data.get("phone"):
        order.append("phone")
    if not data.get("product_type"):
        order.append("product_type")
    if not data.get("quantity"):
        order.append("quantity")
    return order


def _split_person_name(raw: str) -> tuple[str, str]:
    """Ім'я та прізвище з одного рядка — як написав клієнт, без «виправлень»."""
    parts = [p for p in re.split(r"\s+", (raw or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    # «Ім'я Прізвище» або «Прізвище Ім'я» — беремо перше слово як ім'я, решту як прізвище
    return parts[0], " ".join(parts[1:])


def _extract_alt_qty(text: str) -> str | None:
    """«а якщо 200», «давайте 500» — тираж без слова «шт»."""
    q = _extract_qty(text)
    if q:
        return q
    lower = (text or "").lower()
    if not any(w in lower for w in ("якщо", "тоді", "давайте", "зробіть", "зробимо")):
        return None
    if re.search(r"\d+\s*мм", lower):
        return None
    m = re.search(r"\b(\d{2,5})\b", lower)
    return m.group(1) if m else None


_ORDER_SIDE_MARKERS = (
    "доставк",
    "нова пошт",
    "самовивіз",
    "оплат",
    "liqpay",
    "лікпей",
    "швидш",
    "термінов",
    "строк",
    "термін",
    "скільки днів",
    "макет",
    "формат",
    "колір",
    "кольор",
    "фіксатор",
    "pantone",
    "вдавлен",
    "випукл",
    "хто ви",
    "що робите",
    "матеріал",
    "з чого",
    "з якого",
    "наклад",
    "налож",
    "післяплат",
    "готівк",
    "аксесуар",
    "карабін",
    "фастекс",
    "ретрактор",
)


def _is_quote_correction(text: str) -> bool:
    """Клієнт просить виправити прорахунок (ширина / товар), а не оформлювати."""
    lower = (text or "").lower()
    if any(
        w in lower
        for w in (
            "виправ",
            "виправіть",
            "поправ",
            "не правильно",
            "неправильно",
            "помилк",
            "не те",
            "перерахуй",
            "перерах",
        )
    ):
        return True
    # «я вище писав силіконові…» — заміна товару, не додавання
    if ("вище" in lower or "раніше" in lower) and any(
        w in lower for w in ("писав", "писала", "казав", "казала", "мав на увазі", "мала на увазі")
    ):
        return True
    # «прорахунок на 20 мм, а не 15»
    if re.search(r"\bне\s+\d+\s*мм", lower) and re.search(r"\d+\s*мм", lower):
        return True
    if "а не" in lower and re.search(r"\d+\s*мм", lower):
        return True
    if any(w in lower for w in ("потрібно на", "треба на", "рахуйте на", "рахуй на")) and re.search(
        r"\d+\s*мм", lower
    ):
        return True
    return False


def _has_client_question(text: str, data: dict[str, Any] | None = None) -> bool:
    """Питання клієнта важливіше за воронку — навіть якщо в повідомленні є тираж."""
    if not (text or "").strip():
        return False
    # «???» / «ви тут?» — не консультаційне питання, а presence nudge
    if _is_presence_nudge(text):
        return False
    if _is_quote_correction(text):
        return True
    lower = (text or "").lower()
    # «а якщо 200?» — зміна тиражу, не консультаційне питання
    if _extract_alt_qty(text):
        substantive = (
            "колір", "кольор", "фіксатор", "оплат", "достав", "макет", "формат",
            "матеріал", "аксесуар", "карабін", "фастекс", "наклад", "налож",
            "готівк", "термінов", "швидш", "строк",
        )
        if not any(w in lower for w in substantive) and not _asks_where_or_what(text):
            return False
    if _looks_like_question(text) or _asks_where_or_what(text) or _is_side_question(text, data):
        return True
    question_words = (
        "який ",
        "які ",
        "яка ",
        "яке ",
        "чи ",
        "можна ",
        "скільки ",
        "коли ",
        "чому ",
        "де ",
        "як ",
        "що ",
        "підкаж",
        "поясни",
        "розкаж",
        "доступн",
    )
    if any(w in lower for w in question_words):
        return True
    return False


def _split_qty_and_question(text: str) -> tuple[str | None, str]:
    """Відділити тираж від питання: «200 шт Який колір фіксаторів?»."""
    qty = _extract_qty(text)
    if not qty:
        return None, text
    # лише тираж зі шт/штук — не чіпати 350 мл / 15 мм / 450 мл
    question = re.sub(
        rf"(?i)(?:^|[\s,;]+){re.escape(qty)}\s*(?:шт\.?|штук\w*|pcs)\b\.?",
        " ",
        text or "",
        count=1,
    )
    question = re.sub(r"(?i)\bкількість\s*:?\s*", " ", question)
    question = re.sub(r"\s+", " ", question).strip(" ,.;")
    return qty, question or text


def _is_definition_question(text: str) -> bool:
    """«Що це / що означає» про нашу продукцію — відповідь, не воронка."""
    lower = (text or "").strip().lower()
    if not lower:
        return False
    if any(
        w in lower
        for w in ("слово", "англійськ", "переклад", "по-англій", "hello")
    ):
        return False
    marked = any(
        w in lower
        for w in (
            "що таке",
            "що означа",
            "що значить",
            "що це за",
            "а що це",
            "а що за",
            "поясни",
            "розшифр",
            "не знаю що",
            "не розумію",
            "не поня",
            "для чого він",
            "для чого воно",
            "для чого rfid",
            "чим відрізн",
            "яка різниц",
            "розкажи про",
            "розкажіть про",
        )
    )
    if not marked:
        return False
    return bool(_extract_product_hint(text)) or any(
        w in lower
        for w in (
            "rfid",
            "чіп",
            "браслет",
            "шапоч",
            "чашк",
            "tyvek",
            "силікон",
            "шнур",
            "продукц",
            "виріб",
        )
    )


def _wants_catalog_overview(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "асортимент",
            "каталог",
            "список товар",
            "які товар",
            "які є товар",
            "які у вас є",
            "прайс відразу",
            "прайс на все",
            "ціни на кожн",
            "ціни на всі",
            "на кожну щоб",
            "на кожну, щоб",
            "покажи товар",
            "весь прайс",
        )
    )


def _says_already_provided(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    if any(w in lower for w in ("оформл", "замовля", "рахунок")):
        return False
    return any(
        w in lower
        for w in (
            "вище напис",
            "вище писав",
            "я ж напис",
            "я вже напис",
            "я ж вище",
            "дивись вище",
            "див. вище",
            "в попередн",
            "попередньому повідом",
        )
    ) or lower in {"вище", "див вище", "я ж писав", "писав вище"}


def _prefers_consult(text: str) -> bool:
    """Питання, на які треба відповісти, а не гнати воронку."""
    return _is_definition_question(text) or _wants_catalog_overview(text)


def _is_product_followup(text: str, data: dict[str, Any]) -> bool:
    """Уточнення по вже названому товару: «а з якого вона матеріалу?»."""
    if not (data.get("product_type") or data.get("pending_product_type") or data.get("cart")):
        if _extract_product_hint(text):
            return bool(
                re.search(
                    r"матеріал|з чого|з якого|що за |а що ",
                    (text or "").lower(),
                )
            )
        return False
    lower = (text or "").lower().strip()
    if len(lower) < 3:
        return False
    return any(
        w in lower
        for w in (
            "матеріал",
            "з чого",
            "з якого",
            "якого вона",
            "яка вона",
            "а що за",
            "що за шап",
            "це що",
            "колір",
            "кольор",
            "фіксатор",
            "доступн",
            "аксесуар",
            "карабін",
            "фастекс",
            "розмір",
        )
    ) or (
        "?" in text
        and any(w in lower for w in ("вона", "він", "їх", "цього", "ця", "вас"))
        and not _extract_qty(text)
    )


def _product_followup_reply(text: str, data: dict[str, Any]) -> str | None:
    """Факти по товару з сайту/бази, без «який тираж?» замість відповіді."""
    if (
        not _is_definition_question(text)
        and not _is_product_followup(text, data)
        and not _extract_product_hint(text)
    ):
        return None
    lower = (text or "").lower()
    named = _extract_product_hint(text)
    if named:
        prod = named.lower()
    else:
        prod = str(
            data.get("last_topic")
            or data.get("product_type")
            or data.get("pending_product_type")
            or ""
        ).lower()
    about_material = any(w in lower for w in ("матеріал", "з чого", "з якого", "якого вона"))
    about_what = _is_definition_question(text) or any(
        w in lower for w in ("що за", "це що", "а що ", "означа")
    )
    if "rfid" in prod or "rfid" in lower:
        if about_what or about_material or "чіп" in lower:
            return (
                "RFID — це радіочіп у браслеті. Зчитувач бачить його на вході "
                "(турнікет, каса), без паперового штрихкоду. Робимо такі під захід; "
                "ціна індивідуальна, залежить від чипа і обсягу замовлення."
            )
    if "шапоч" in prod or "плаван" in prod or "шапоч" in lower:
        if about_material or about_what:
            return "Це силіконові шапочки для плавання з друком лого."
    if "чашк" in prod:
        if about_material or about_what:
            return (
                "Еко-чашки з бамбукового волокна, поліпропілену і силіконової кришки/тримача, "
                "з нанесенням лого."
            )
    if "tyvek" in prod:
        if about_material or about_what:
            return "Tyvek — міцний вологостійкий папір, одноразові контрольні браслети на захід."
    if ("силікон" in prod or "браслет" in prod) and (about_material or about_what):
        return (
            "Силіконові браслети — гіпоалергенний силікон з логотипом; "
            "зазвичай 5, 12 або 20 мм."
        )
    about_color = any(w in lower for w in ("колір", "кольор", "палітр", "pantone"))
    # колір ДРУКУ лого (1/2/3/4 кольори) ≠ колір самого браслета
    about_print_colors = bool(
        re.search(r"\d\s*(?:колір|кольор)", lower)
        or any(w in lower for w in ("друк", "лого", "кольорів друку", "кольору друку"))
    ) and any(w in lower for w in ("кольор", "колір", "друк", "лого"))
    if "тканинн" in prod or "тканинн" in lower:
        if any(w in lower for w in ("фіксатор", "колір", "кольор")):
            return (
                "Для тканинних браслетів фіксатори є в чорному та білому кольорах — "
                "на вартість не впливає. Друк логотипу на зовнішній стороні стрічки; "
                "внутрішня — фон або без друку. Ширина стрічки 15 мм, довжина 350 мм."
            )
        if about_material or about_what:
            return (
                "Тканинні браслети: стрічка 15 мм, довжина в розгорнутому вигляді 350 мм, "
                "фіксатор чорний або білий."
            )
    if about_color and not about_print_colors and (
        "силікон" in prod
        or (
            "браслет" in prod
            and "тканин" not in prod
            and "світл" not in prod
            and "tyvek" not in prod
            and "слеп" not in prod
            and "slap" not in prod
        )
    ):
        return (
            "Кольори силіконових браслетів — широка палітра на сайті: "
            "https://silicon.com.ua/silikonovi-brasleti "
            "Напишіть бажаний колір або pantone — підкажемо чи є в наявності."
        )
    if about_color and not about_print_colors and ("світл" in prod or "світловідбив" in lower):
        return (
            "Кольори світловідбиваючих браслетів дивись тут: "
            "https://silicon.com.ua/svitlovidbivayuchi-brasleti"
        )
    if about_color and not about_print_colors and (
        "tyvek" in prod or "контрольн" in prod or "tyvek" in lower
    ):
        return (
            "Кольори контрольних Tyvek: "
            "https://silicon.com.ua/kontrolni-braslety-tyvek "
            "1 колір друку = чорний на будь-якому кольорі браслета; кольоровий друк — за прайсом на білих."
        )
    # Slap: питання про колір браслета (палітра) — лінк; про кольори ДРУКУ — не сюди (прайс)
    if about_color and not about_print_colors and (
        "слеп" in prod or "slap" in prod or "слеп" in lower or "slap" in lower
    ):
        return (
            "Кольори самих slap-браслетів можна вибрати на сайті: "
            "https://silicon.com.ua/silikonovi-slep-braslety"
        )
    # Slap + кольори друку / вартість — орієнтир з прайсу (1–4 кольори)
    if (
        ("слеп" in prod or "slap" in prod or "слеп" in lower or "slap" in lower)
        and (about_print_colors or _wants_price_or_quote(text))
        and not _extract_qty(text)
    ):
        start = calculator.starting_quote("slap", text=text)
        if start:
            return start

    if ("шнур" in prod or "шнур" in lower or _product_family(prod) == "lanyard") and not _extract_qty(text):
        if _is_quote_correction(text) or _is_lanyard_fastening_choice(text):
            return None
        if _wants_lanyard_stock(lower) and not _wants_lanyard_print(lower):
            return LANYARD_COLORS_REPLY
        if _wants_lanyard_print(lower) or "шнур" in lower or "шнур" in prod:
            return LANYARD_PRINT_GUIDE
    if "шнур" in prod and (about_material or about_what):
        return "Шнурки для бейджів — репсові стрічки з фурнітурою під задачу."
    if "ретрактор" in prod or "ретрактор" in lower:
        if about_what or about_material or "що" in lower:
            return (
                "Ретрактор — кліп-рулетка / бейдж з витяжним тросиком. "
                "Строк 5–10 робочих днів залежно від кількості."
            )
    if about_what and prod:
        return (
            f"{prod[0].upper() + prod[1:] if prod else 'Ця позиція'} — з нашого асортименту silicon.com.ua. "
            "Можу коротко сказати, для чого він і як рахується."
        )
    return None


def _contextual_fallback(question: str, data: dict[str, Any] | None = None) -> str:
    """Не казати «не впевнений, що порахувати», якщо товар уже в діалозі."""
    data = data or {}
    follow = _product_followup_reply(question, data)
    if follow:
        return follow
    prod = str(
        data.get("product_type") or data.get("pending_product_type") or data.get("last_topic") or ""
    ).strip()
    qty = str(data.get("quantity") or "").strip()
    lower = (question or "").lower()
    if data.get("invoice_pdf_path") or data.get("invoice_number"):
        return (
            "Рахунок уже надіслав файлом. Як оплатите за реквізитами — киньте скрін квитанції сюди."
        )
    if any(w in lower for w in ("оплат", "liqpay", "лікпей", "рахунок", "наклад", "налож", "готівк", "кеш")):
        if _wants_invoice_requisites(question):
            return INVOICE_PROMPT
        if any(w in lower for w in ("готівк", "кеш", "готівкою", "на місці", "в офіс")):
            url = (calculator.templates or {}).get("contacts_url") or "https://silicon.com.ua/kontakti"
            return (
                "Оплата готівкою можлива.\n"
                "Для запуску роботи в друк потрібно приїхати до нас у офіс і оплатити замовлення готівкою.\n"
                "Передплата 100%.\n"
                f"Адреса та години роботи — у розділі Контакти: {url}"
            )
        if any(w in lower for w in ("наклад", "налож", "післяплат", "при отриманн")):
            return (
                "Накладеним платежем на Новій пошті чи Укрпошті не відправляємо. "
                "Друковану продукцію запускаємо лише після повної оплати та погодження макету до друку. "
                "Можна карткою онлайн або рахунком для ФОП/ТОВ."
            )
        return "Карткою онлайн, рахунок для ФОП/ТОВ або готівкою в офісі (100% передплата). Як зручніше?"
    if any(w in lower for w in ("зв'яж", "звяж", "хтось зв", "хто зв", "менеджер")):
        return "Так, я тут у чаті. Після оплати сам побачу надходження і напишу вам."
    if _wants_catalog_overview(question):
        return AIConsultant().build_assortment_overview()
    if prod and qty and not _looks_like_question(question):
        return f"По {prod} {qty} шт. Що ще потрібно — інший товар, уточнення чи оформлення?"
    if prod and not _looks_like_question(question):
        return f"По {prod} можу зорієнтувати: тираж, ціна чи деталі виробу."
    return "Напишіть, який товар і що потрібно — опис, ціна чи оформити."


def _wants_price_or_quote(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "цін",
            "варт",
            "кошту",
            "по чому",
            "почому",
            "порахуй",
            "прорахуй",
            "рахуй",
            "порахув",
            "прорахув",
            "скільки буде",
        )
    )


def _is_offtopic_question(text: str, data: dict[str, Any] | None = None) -> bool:
    """Питання не про прайс Silicon і не про оформлення — відповісти, можна з інтернету."""
    from app.services.web_search import is_catalog_or_order_topic, should_web_search

    if _is_presence_nudge(text):
        return False
    if _extract_qty(text) or _extract_product_hint(text) or _wants_price_or_quote(text):
        return False
    if _is_product_followup(text, data or {}):
        return False
    lower = (text or "").lower()
    if any(w in lower for w in _ORDER_SIDE_MARKERS):
        return False
    if is_catalog_or_order_topic(text):
        return False
    return should_web_search(text)


def _is_side_question(text: str, data: dict[str, Any] | None = None) -> bool:
    lower = (text or "").lower()
    # «2 карабіни» в специфікації шнурка — це фурнітура, не окреме питання про карабіни
    if _is_lanyard_hardware_spec(text) and _extract_qty(text):
        return False
    if data and _is_lanyard_fastening_choice(text) and _lanyard_context_ready(data):
        return False
    return any(w in lower for w in _ORDER_SIDE_MARKERS) or (
        "?" in text
        and not _extract_qty(text)
        and not _extract_product_hint(text)
        and not _wants_price_or_quote(text)
        and not _is_presence_nudge(text)
        and not _is_offtopic_question(text, data)
    )


def _is_interrupt_question(text: str, field: str | None = None) -> bool:
    """Питання не по поточному полю — відповісти і лишитись на кроці."""
    lower = (text or "").strip().lower()
    if not lower:
        return False
    parsed = _parse_kv(text)
    if any(parsed.get(k) for k in ("first_name", "last_name", "phone", "edrpou", "quantity", "product_type")):
        return False
    compact = lower.replace(" ", "").replace("-", "")
    if field == "phone" and re.search(r"(\+?380\d{9}|\+?0\d{9}|\d{9,12})", compact):
        return False
    if field == "quantity" and (_extract_qty(text) or re.search(r"\b\d{1,6}\b", lower)):
        return False
    if field == "product_type" and _extract_product_hint(text) and "?" not in text:
        return False
    if field == "person_name":
        if _asks_where_or_what(text) or _is_side_question(text):
            return True
        words = [w for w in re.split(r"\s+", text.strip()) if w]
        faq = (
            "достав", "оплат", "строк", "термін", "макет", "цін", "скільки",
            "які ", "що ", "як ", "хто ", "коли ", "чому", "логотип", "pantone",
            "де ", "єдрпоу", "навіщо", "поясни",
        )
        if "?" not in text and len(words) <= 5 and not any(x in lower for x in faq):
            return False
    if field == "delivery_type":
        if lower in {"1", "2", "3"} or any(
            w in lower for w in ("відділен", "поштомат", "адрес", "самовивіз", "пошт", "нп")
        ):
            return False
    if field == "payment":
        if any(w in lower for w in ("як оплат", "оплату як", "які способ", "чим оплат")):
            return True
        if "?" in text and not any(
            w in lower
            for w in ("liqpay", "лікпей", "картк", "рахунок", "оплат", "лігпей")
        ):
            return True
        return False
    if field == "delivery_field":
        if re.search(r"\d+", lower) or any(
            w in lower for w in ("відділен", "поштомат", "вул", "просп", "буд")
        ):
            return False
        return _looks_like_question(text)
    if field == "edrpou":
        if _extract_edrpou(text) or re.fullmatch(r"\d{8,10}", compact):
            return False
        return _looks_like_question(text) or _asks_where_or_what(text) or _is_side_question(text)
    return _looks_like_question(text) or _asks_where_or_what(text) or _is_side_question(text)


async def _answer_interrupt(
    db: AsyncSession,
    state: ConversationState,
    data: dict[str, Any],
    text: str,
    next_prompt: str,
    action: str,
    *,
    field: str | None = None,
) -> ChatMessageResponse:
    """Відповісти на питання і вийти з «зациклення» на поточному полі."""
    help_text = _field_help_reply(field, text)
    escalate = False
    if help_text:
        reply = help_text
    else:
        # Спочатку факти по товару (кольори/матеріал) — не starting_quote замість відповіді
        follow = _product_followup_reply(text, data)
        if follow:
            reply = follow
        else:
            result = await consultant.consult(db, text, state.chat_id)
            reply = _strip_echo(result.get("response") or "", text)
            if reply == AIConsultant.FALLBACK_RESPONSE:
                reply = _contextual_fallback(text, data)
            # якщо consult знову дав «від X грн / тираж» замість відповіді на питання
            if _has_client_question(text, data) and (
                "напишіть тираж" in reply.lower() or "порахую суму" in reply.lower()
            ):
                alt = _contextual_fallback(text, data)
                if alt and "напишіть, який товар" not in alt.lower():
                    reply = alt
            escalate = bool(result.get("escalate_to_manager"))
    if escalate:
        ticket = await chat_history.create_manager_ticket(
            db, state.chat_id, text, mute_bot=False
        )
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_finalize_bot_reply(reply or MANAGER_WAIT_REPLY),
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )
    # коли GPT живий — не дописувати анкетний крок поверх відповіді
    if (
        next_prompt
        and not _ai_live()
        and not _is_question_about_field(text, field)
        and not _is_offtopic_question(text, data)
        and not (field == "edrpou" and _asks_where_or_what(text))
        and next_prompt.rstrip("?")[:18].lower() not in reply.lower()
    ):
        reply = f"{reply.rstrip()}\n\n{next_prompt}"
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(response=_finalize_bot_reply(reply), action=action)


async def _consult_now(
    db: AsyncSession,
    state: ConversationState,
    data: dict[str, Any],
    text: str,
    *,
    action: str = "consult",
    append: str | None = None,
) -> ChatMessageResponse:
    """GPT / база знань першими; воронка не перебиває відповідь."""
    guide = _catalog_guide_reply(text, data)
    if guide:
        escalate = "голограм" in (text or "").lower() and guide.startswith("Прийняв")
        if "передаю менеджеру" in guide.lower() or "передам менеджеру" in guide.lower():
            escalate = True
        if escalate:
            ticket = await chat_history.create_manager_ticket(
                db, state.chat_id, text, mute_bot=False
            )
            if "голограм" in (text or "").lower() or "голограм" in str(
                data.get("last_topic") or ""
            ):
                data["last_topic"] = "голограми"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_finalize_bot_reply(guide),
                action="manager_handoff",
                data={"ticket_id": ticket.id},
            )
        fam = _product_family(text)
        if fam not in {"", "other"}:
            data["last_topic"] = {
                "mat": "силіконові килимки",
                "hologram": "голограми",
                "fitness": "резинки для фітнесу",
                "carabiner": "карабіни металеві",
                "pocket": "кишенька для бейджа",
                "cardholder": "кардхолдери",
                "lanyard": "шнурки для бейджів",
                "brelok": "брелоки",
                "cups": "еко-чашки",
                "ring": "кільця",
                "phone": "аксесуари для телефонів",
                "silicone": "силіконові браслети",
            }.get(fam) or data.get("last_topic")
        _save_data(state, data)
        await db.commit()
        return _guide_response(guide, action="consult")

    result = await consultant.consult(db, text, state.chat_id)
    reply = _strip_echo(result.get("response") or "", text)
    if reply == AIConsultant.FALLBACK_RESPONSE:
        reply = _contextual_fallback(text, data) or _catalog_guide_reply(text, data) or reply
    if result.get("escalate_to_manager"):
        ticket = await chat_history.create_manager_ticket(
            db, state.chat_id, text, mute_bot=False
        )
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_finalize_bot_reply(reply or MANAGER_WAIT_REPLY),
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )
    if (
        append
        and not _ai_live()
        and append.rstrip("?")[:18].lower() not in reply.lower()
        and "оформлю" not in reply.lower()
        and "не впевнений" not in reply.lower()
    ):
        reply = f"{reply.rstrip()}\n\n{append}"
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(response=_finalize_bot_reply(reply), action=action)


def _asks_if_bot(text: str) -> bool:
    """«Ти бот / іі / нейронка?» — не плутати із запитом живого менеджера."""
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "ти бот",
            "ви бот",
            "це бот",
            "ти робот",
            "ви робот",
            "це робот",
            "штучний інтелект",
            "искусственн",
            "нейромереж",
            "нейронк",
            "chatgpt",
            "gpt?",
            "ти gpt",
            "ти іі",
            "ти ии",
            "ти аі",
            "ти ai",
            "це іі",
            "це аі",
            "це ai",
            "аі агент",
            "ai агент",
            "іі агент",
            "ии агент",
            "ai-агент",
            "аі-агент",
            "ai agent",
            "ти агент",
            "ви агент",
            "це агент",
            "автоответ",
            "ти живий",
            "ви живі",
            "ти людина",
            "ви людина",
            "це людина",
            "ти реальн",
            "ви реальн",
            "спілкуюсь з ботом",
            "это бот",
            "ты бот",
            "вы бот",
            "это ии",
            "ты ии",
        )
    )


SCREENSHOT_MARKER_RE = re.compile(r"^\[СКРІН(?:\s+ОПЛАТИ)?(?:\|([^\]]+))?\]\s*", re.IGNORECASE)

INVOICE_SCREENSHOT_HINT = (
    "Після оплати надішліть сюди скрін квитанції — менеджер перевірить і напише вам у цей чат."
)


def _invoice_pdf_client_message(number: str | None, *, resend: bool = False) -> str:
    label = (number or "").strip()
    named = f" {label}" if label else ""
    if resend:
        return (
            f"Ще раз надсилаю рахунок{named} файлом. "
            "Оплата за реквізитами в PDF. Як проведете — киньте скрін квитанції сюди."
        )
    return (
        f"Надсилаю рахунок{named} — файл нижче. "
        "Оплатити можна за реквізитами з PDF. "
        "Якщо зручніше карткою, напишіть — зроблю посилання. "
        "Як оплатите за рахунком, киньте сюди скрін квитанції, перевіримо і відповімо в цей чат."
    )


def _strip_screenshot_marker(text: str) -> tuple[str, str | None]:
    m = SCREENSHOT_MARKER_RE.match(text or "")
    if not m:
        return (text or "").strip(), None
    rest = (text[m.end() :] if text else "").strip()
    return rest or "[скрін]", (m.group(1) or "").strip() or None


def _has_invoice_session(data: dict[str, Any]) -> bool:
    return bool(data.get("invoice_pdf_path") or data.get("invoice_number"))


def _screenshot_is_payment(text: str, data: dict[str, Any]) -> bool:
    """Скрін стосується оплати (а не макету/правок/випадкового фото)?"""
    lower = (text or "").lower()
    if any(
        w in lower
        for w in (
            "оплат", "сплат", "квитанц", "чек", "рахунок", "реквізит",
            "liqpay", "лікпей", "переказ", "платіж", "платеж", "оплачен",
        )
    ):
        return True
    # активна сесія рахунку/оплати — скрін найімовірніше квитанція
    return bool(
        _has_invoice_session(data)
        or data.get("order_id")
        or data.get("payment_url")
        or data.get("awaiting_payment")
    )


def _says_already_paid(text: str) -> bool:
    lower = (text or "").lower()
    if lower.startswith("[скрін оплати") or "скрін оплати" in lower:
        return True
    if any(w in lower for w in ("скрін", "скрин", "квитанц")) and any(
        w in lower for w in ("оплат", "рахунок", "реквізит")
    ):
        return True
    if any(
        w in lower
        for w in (
            "оплатив",
            "оплатила",
            "оплачено",
            "сплатив",
            "сплатила",
            "сплачено",
            "заплатив",
            "заплатила",
            "вже оплат",
            "уже оплат",
            "гроші пройшли",
            "гроші пішли",
            "оплата пройшла",
            "оплата пішла",
            "кинув на карт",
            "скинув на карт",
            "перевів",
            "перевела",
        )
    ):
        return True
    sent = any(
        w in lower
        for w in (
            "кинув",
            "кинула",
            "скинув",
            "скинула",
            "перевів",
            "перевела",
            "перекинув",
            "перекинула",
            "відправив",
            "відправила",
        )
    )
    money = any(w in lower for w in ("грош", "кошт", "карт", "liqpay", "лікпей", "оплат"))
    return sent and money


def _says_pay_link_broken(text: str) -> bool:
    lower = (text or "").lower()
    broken = any(
        w in lower
        for w in ("не працює", "не открыв", "не відкрив", "не відкрива", "помилка", "не грузить", "не завантаж")
    )
    about = any(w in lower for w in ("посилан", "оплат", "liqpay", "лікпей", "лінк", "link", "сторінк"))
    return broken and about


def _asks_after_pay_contact(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "хтось зв",
            "хто зв",
            "зв'яжеться",
            "звяжеться",
            "зв'яжетесь",
            "звяжетесь",
            "після оплати зі мною",
            "після оплати хто",
        )
    )


async def _reply_payment_link_issue(
    db: AsyncSession, state: ConversationState, data: dict[str, Any]
) -> ChatMessageResponse:
    order_id = data.get("order_id")
    if order_id:
        pay = await order_service.create_payment(db, int(order_id), force_new=True)
        url = (pay or {}).get("payment_url") or ""
        if pay.get("already_paid"):
            return ChatMessageResponse(
                response="Бачу вашу оплату. Беремо замовлення в роботу.",
                action="payment_confirmed",
                data=pay,
            )
        if url.startswith("http"):
            state.step = "idle"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=(
                    "Ок, зрозумів. Спробуйте ще раз це посилання — має відкритись сторінка LiqPay:\n"
                    f"{url}\n\n"
                    "Якщо знову не відкриється, напишіть «рахунок» — виставлю рахунок для ФОП/ТОВ."
                ),
                action="payment_created",
                data=pay,
            )
    return ChatMessageResponse(
        response=(
            "Ок. Можу виставити рахунок для оплати за реквізитами — напишіть «рахунок» і ЄДРПОУ. "
            "Або напишіть «карткою», і я надішлю посилання ще раз."
        ),
        action="payment_info",
    )


async def _latest_payment(db: AsyncSession, order_id: Any) -> Payment | None:
    if not order_id:
        return None
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return None
    result = await db.execute(
        select(Payment).where(Payment.order_id == oid).order_by(Payment.id.desc())
    )
    return result.scalars().first()


async def _reply_payment_status(
    db: AsyncSession,
    data: dict[str, Any],
    *,
    chat_id: str | None = None,
    user_text: str = "",
    screenshot_path: str | None = None,
) -> ChatMessageResponse:
    pay = await _latest_payment(db, data.get("order_id"))
    if pay and pay.status != PaymentStatus.SUCCESS:
        pay = await order_service.refresh_payment_from_liqpay(db, pay, notify=False)
    order_id = data.get("order_id")
    if order_id:
        try:
            result = await db.execute(select(Order).where(Order.id == int(order_id)))
            order = result.scalar_one_or_none()
        except (TypeError, ValueError):
            order = None
        if order and order.payment_status == PaymentStatus.SUCCESS:
            return ChatMessageResponse(
                response="Бачу вашу оплату. Беремо замовлення в роботу.",
                action="payment_confirmed",
            )
    if pay and pay.status == PaymentStatus.SUCCESS:
        return ChatMessageResponse(
            response="Бачу вашу оплату. Беремо замовлення в роботу.",
            action="payment_confirmed",
        )
    if chat_id and (_has_invoice_session(data) or screenshot_path):
        await chat_history.upsert_invoice_payment_notice(
            db,
            chat_id=str(chat_id),
            order_id=data.get("order_id"),
            invoice_number=data.get("invoice_number"),
            amount=data.get("total_amount"),
            company_name=data.get("company_name"),
            message=user_text,
            screenshot_path=screenshot_path,
        )
        looks_like_shot = bool(screenshot_path) or any(
            w in (user_text or "").lower() for w in ("скрін", "скрин", "квитанц")
        )
        if looks_like_shot:
            return ChatMessageResponse(
                response=(
                    "Дякую, отримав. Передав менеджеру перевірити оплату за рахунком — "
                    "він напише вам у цей чат."
                ),
                action="invoice_payment_notice",
            )
        return ChatMessageResponse(
            response=(
                "Ок, дякую. Надішліть, будь ласка, скрін квитанції сюди — "
                "менеджер перевірить оплату за рахунком і напише вам у цей чат."
            ),
            action="invoice_payment_notice",
        )
    if pay and pay.payment_url:
        return ChatMessageResponse(
            response=(
                "Поки оплату в LiqPay не бачу. Напишіть, коли оплачували — перевірю ще раз. "
                "Якщо посилання не відкривається — напишіть «рахунок», виставлю рахунок."
            ),
            action="payment_pending",
        )
    return ChatMessageResponse(
        response="Поки оплату не бачу. Якщо вже платили — напишіть приблизно коли, перевірю.",
        action="payment_info",
    )


def _event_reco(text: str) -> str | None:
    lower = (text or "").lower()
    if any(w in lower for w in ("фестивал", "концерт", "масов")):
        return "На фестиваль зручніше Tyvek або RFID, якщо потрібен контроль входу. Який тираж?"
    if any(w in lower for w in ("корпоратив", "офіс", "команд")):
        return "На корпоратив зазвичай силікон з лого — 12 мм найходовіший. Який тираж?"
    return None


def _extract_qty(text: str) -> str | None:
    lower = text.lower()
    # \b + без пробілів у числі — щоб «300x30 500 штук» не стало 30500
    m = re.search(
        r"(?<![xх×\d])(\d{1,6})\s*(?:шт\.?|штук[ауи]?|pcs)\b",
        lower,
    )
    if m:
        return m.group(1)
    return None


def _is_reflective_text(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        w in lower
        for w in (
            "світловідб",
            "світло відб",
            "відбивн",
            "рефлект",
            "reflective",
            "reflect",
        )
    )


def _product_family(text: str) -> str:
    lower = (text or "").lower()
    if any(w in lower for w in ("шапоч", "плаванн")):
        return "caps"
    if any(w in lower for w in ("чашк", "екочаш", "еко-чаш")):
        return "cups"
    # Кишенька / кардхолдер — раніше за «бейдж», інакше тягне шнурок
    if any(w in lower for w in ("кишеньк", "кишеня", "кишені")):
        return "pocket"
    if any(w in lower for w in ("кардхолдер", "кард-холдер", "картхолдер", "card holder")):
        return "cardholder"
    # Ретрактор / брелок — раніше за «бейдж», інакше тягне шнурок
    if any(w in lower for w in ("ретрактор", "кліп рулетк", "витяжн")):
        return "retractor"
    if "брелок" in lower:
        return "brelok"
    if _is_lanyard_hardware_spec(lower):
        return "lanyard"
    if any(w in lower for w in ("шнур",)) or (
        "бейдж" in lower and "кишень" not in lower and "кардхолдер" not in lower
    ):
        return "lanyard"
    if "килим" in lower:
        return "mat"
    if any(w in lower for w in ("резинк", "резинок", "фітнес", "fitness", "jk-rb")):
        return "fitness"
    if _is_standalone_carabiner_query(lower):
        return "carabiner"
    if "голограм" in lower:
        return "hologram"
    if any(w in lower for w in ("кільц",)) and "браслет" not in lower:
        return "ring"
    if any(w in lower for w in ("чохол", "аксесуар для моб", "мобільн")):
        return "phone"
    if "атласн" in lower:
        return "atlas"
    if "тканинн" in lower:
        return "fabric"
    if "slap" in lower or "слеп" in lower:
        return "slap"
    if "tyvek" in lower or "контрол" in lower:
        return "tyvek"
    if "rfid" in lower:
        return "rfid"
    if _is_reflective_text(lower):
        return "reflective"
    if "силікон" in lower or "браслет" in lower:
        return "silicone"
    return lower[:24] or "other"


def _is_silicone_bracelet_text(text: str) -> bool:
    return _product_family(text) == "silicone"


def _product_for_calc(text: str, data: dict[str, Any], hint: str | None = None) -> str:
    raw = hint or _extract_product_hint(text) or data.get("pending_product_type") or data.get("product_type") or ""
    blob = f"{raw} {text}"
    fam = _product_family(blob)
    mapping = {
        "caps": "шапочки для плавання",
        "cups": "еко-чашки",
        "lanyard": "шнурки для бейджів",
        "brelok": "брелок",
        "mat": "килимки",
        "fitness": "резинки для фітнесу",
        "carabiner": "карабіни металеві",
        "hologram": "голограми",
        "retractor": "ретрактор",
        "atlas": "атласна",
        "fabric": "тканинний браслет",
        "slap": "slap",
        "tyvek": "tyvek",
        "rfid": "rfid",
        "reflective": "світловідбиваючий",
        "silicone": "силіконовий",
        "pocket": "кишенька для бейджа",
        "cardholder": "кардхолдер",
        "ring": "кільце",
        "phone": "аксесуари для телефонів",
    }
    return mapping.get(fam, str(raw))


def _extract_product_hint(text: str) -> str | None:
    lower = (text or "").lower()
    if any(w in lower for w in ("кишеньк", "кишеня", "кишені")):
        return "кишенька для бейджа"
    if any(w in lower for w in ("кардхолдер", "кард-холдер", "картхолдер", "card holder")):
        return "кардхолдери"
    if any(w in lower for w in ("чохол", "аксесуар для моб", "аксесуари для мобільн")):
        return "аксесуари для телефонів"
    if _is_lanyard_hardware_spec(text):
        return "шнурки для бейджів"
    # «2 карабіни» після шнурка — кріплення, не перемикання на металеві карабіни
    if _is_lanyard_fastening_choice(text):
        return None
    if _is_standalone_carabiner_query(text):
        return "карабіни металеві"
    if any(w in lower for w in ("резинк", "резинок", "фітнес", "fitness", "jk-rb")):
        return "резинки для фітнесу"
    # «не браслет, а еко-чашки» — брати те, що після заперечення
    if re.search(r"\bне\b", lower) and re.search(r"\sа\s", lower):
        after = re.split(r"\sа\s", lower, maxsplit=1)[-1]
        picked = _extract_product_hint_raw(after)
        if picked:
            return picked
    return _extract_product_hint_raw(lower)


def _extract_product_hint_raw(lower: str) -> str | None:
    mapping = [
        ("rfid", "RFID-браслети"),
        ("світловідб", "світловідбиваючі браслети"),
        ("світло відб", "світловідбиваючі браслети"),
        ("відбивн", "світловідбиваючі браслети"),
        ("рефлект", "світловідбиваючі браслети"),
        ("reflective", "світловідбиваючі браслети"),
        ("tyvek", "браслети Tyvek"),
        ("контрол", "браслети Tyvek"),
        ("slap", "slap-браслети"),
        ("слеп", "slap-браслети"),
        ("шапоч", "шапочки для плавання"),
        ("плаванн", "шапочки для плавання"),
        ("еко-чаш", "еко-чашки"),
        ("екочаш", "еко-чашки"),
        ("чашк", "еко-чашки"),
        ("килим", "силіконові килимки"),
        ("резинк", "резинки для фітнесу"),
        ("резинок", "резинки для фітнесу"),
        ("фітнес", "резинки для фітнесу"),
        ("брелок", "стрічки-брелоки"),
        ("голограм", "голограми"),
        ("ретрактор", "ретрактори"),
        ("кліп рулетк", "ретрактори"),
        ("витяжн", "ретрактори"),
        ("кишеньк", "кишенька для бейджа"),
        ("кардхолдер", "кардхолдери"),
        ("картхолдер", "кардхолдери"),
        ("card holder", "кардхолдери"),
        ("шнур", "шнурки для бейджів"),
        ("бейдж", "шнурки для бейджів"),
        ("атласн", "стрічка атласна"),
        ("тканинн", "тканинні браслети"),
        ("силікон", "силіконові браслети"),
        ("браслет", "силіконові браслети"),
    ]
    # якщо в одному повідомленні і силікон, і світловідбиваючі — не обирати один «головний»
    if "силікон" in lower and _is_reflective_text(lower):
        return None
    for key, name in mapping:
        if key in lower:
            return name
    return None


def _extract_order_lines(text: str) -> list[dict[str, Any]]:
    """Кілька позицій з одного повідомлення: товар + кількість (+ мм)."""
    lower = (text or "").lower()
    patterns = [
        (r"шапоч\w*|плаванн\w*", "шапочки для плавання"),
        (r"еко-?чаш\w*|чашк\w*", "еко-чашки"),
        (r"ретрактор\w*|кліп\s*рулетк\w*|витяжн\w*", "ретрактор"),
        (r"брелок\w*", "брелок"),
        (r"кишеньк\w*|кишен[яі]\w*", "кишенька для бейджа"),
        (r"кардхолдер\w*|картхолдер\w*", "кардхолдер"),
        (r"шнур\w*", "шнурки для бейджів"),
        (r"світловідб\w*|світло\s*відб\w*|відбивн\w*|рефлект\w*|reflective", "світловідбиваючий"),
        (r"силікон\w*", "силіконовий"),
        (r"rfid", "RFID"),
        (r"tyvek|контрол\w*", "tyvek"),
        (r"slap|слеп", "slap"),
        (r"тканинн\w*", "тканинний браслет"),
    ]
    hits: list[dict[str, Any]] = []
    for pat, ptype in patterns:
        for m in re.finditer(pat, lower):
            hits.append({"start": m.start(), "end": m.end(), "product_type": ptype})
    if not hits:
        return []
    hits.sort(key=lambda h: h["start"])
    cleaned: list[dict[str, Any]] = []
    for h in hits:
        if cleaned and h["start"] < cleaned[-1]["end"]:
            continue
        cleaned.append(h)

    lines: list[dict[str, Any]] = []
    for i, h in enumerate(cleaned):
        seg_end = cleaned[i + 1]["start"] if i + 1 < len(cleaned) else len(lower)
        segment = lower[h["start"] : seg_end]
        qty_raw = _extract_qty(segment)
        if not qty_raw:
            interest = re.search(r"(?:цікавить|потрібн\w*|бер[еи]мо)\s+(\d{2,5})\b", segment)
            if interest:
                qty_raw = interest.group(1)
        if not qty_raw:
            # «браслети 300x30 500» без слова «штук» — беремо останнє число, що не розмір
            flat_sizes: set[str] = set(re.findall(r"(\d+)\s*мм", segment))
            for a, b in re.findall(r"(\d+)\s*[xх×]\s*(\d+)", segment):
                flat_sizes.add(a)
                flat_sizes.add(b)
            candidates = [n for n in re.findall(r"\b(\d{2,5})\b", segment) if n not in flat_sizes]
            if candidates:
                qty_raw = candidates[-1]
        if not qty_raw:
            continue
        qty = int(qty_raw)
        size_m = re.search(r"(\d+)\s*мм", segment)
        size_mm = size_m.group(1) if size_m else None
        lines.append({
            "product_type": h["product_type"],
            "quantity": qty,
            "size_mm": size_mm,
            "text": segment,
        })
    return lines


_AGE_CIRC = (
    (r"дитяч", "160", "дитячий"),
    (r"підлітк|подростк", "180", "підлітковий"),
    (r"доросл", "202", "дорослий"),
)
_COLOR_WORDS = (
    "синій", "синя", "синє", "сині",
    "червоний", "червона", "червоне", "червоні",
    "зелений", "зелена", "зелене", "зелені",
    "жовтий", "жовта", "жовте", "жовті",
    "чорний", "чорна", "чорне", "чорні",
    "білий", "біла", "біле", "білі",
    "помаранчевий", "помаранчева", "оранжевий",
    "фіолетовий", "фіолетова",
    "рожевий", "рожева",
    "блакитний", "блакитна",
    "сірий", "сіра",
    "коричневий", "салатовий", "бірюзовий", "бордовий", "бежевий",
)


def _extract_silicone_variant_lines(
    text: str, data: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Кілька позицій силікону: «50 шт синій дитячий, 50 шт червоний підлітковий…».
    Повертає (рядки, width_specified)."""
    data = data or {}
    lower = (text or "").lower()
    blob = f"{lower} {data.get('last_topic') or ''} {data.get('product_type') or ''}"
    if _product_family(blob) not in {"silicone", "other"} and not any(
        w in lower for w in ("дитяч", "підлітк", "доросл", "браслет", "силікон")
    ):
        return [], False
    if any(w in lower for w in ("кишеньк", "шнур", "кардхолдер", "чашк", "шапоч", "ретрактор")):
        return [], False
    if not any(w in lower for w in ("дитяч", "підлітк", "доросл")) and not re.search(
        r"\b(160|180|202)\b", lower
    ):
        return [], False

    width_specified = bool(re.search(r"\b(5|12|20)\s*мм", lower)) or bool(
        re.search(r"[*xх×]\s*(5|12|20)\b", lower)
    )
    width = "12"
    wm = re.search(r"\b(5|12|20)\s*мм", lower) or re.search(r"[*xх×]\s*(5|12|20)\b", lower)
    if wm:
        width = wm.group(1)

    chunks: list[tuple[int, str]] = []
    for m in re.finditer(
        r"(\d{1,5})\s*(?:шт\.?|штук[ауи]?)\s*(.*?)(?=(?:\d{1,5}\s*(?:шт|штук))|$)",
        lower,
    ):
        qty = int(m.group(1))
        rest = (m.group(2) or "").strip()
        if qty < 1:
            continue
        if not rest and not any(w in lower for w in ("дитяч", "підлітк", "доросл", "160", "180", "202")):
            continue
        chunks.append((qty, rest))

    lines: list[dict[str, Any]] = []
    for qty, rest in chunks:
        circ = None
        age = None
        for pat, circ_v, age_v in _AGE_CIRC:
            if re.search(pat, rest) or re.search(pat, lower):
                if re.search(pat, rest) or (len(chunks) == 1):
                    circ, age = circ_v, age_v
                    break
        if not circ:
            cm = re.search(r"\b(202|180|160)\b", rest)
            if cm:
                circ = cm.group(1)
                age = {"202": "дорослий", "180": "підлітковий", "160": "дитячий"}.get(circ)
        if not circ and not age:
            continue
        if not circ:
            circ = {"дитячий": "160", "підлітковий": "180", "дорослий": "202"}.get(age or "", "202")
        color = next((c for c in _COLOR_WORDS if c in rest), None)
        w_line = width
        wm2 = re.search(r"\b(5|12|20)\s*мм", rest) or re.search(r"[*xх×]\s*(5|12|20)\b", rest)
        if wm2:
            w_line = wm2.group(1)
        bits = [f"Силіконовий браслет {circ}×{w_line} мм"]
        extra = []
        if age:
            extra.append(age)
        if color:
            extra.append(color)
        name = bits[0] + (f" ({', '.join(extra)})" if extra else "")
        lines.append({
            "product_type": "силіконовий",
            "quantity": qty,
            "size_mm": w_line,
            "circ_mm": circ,
            "age": age,
            "color": color,
            "product_name": name,
            "text": f"силіконовий браслет {w_line} мм {qty} шт 1 колір",
        })

    # кілька вікових груп у одному «50 шт …, 50 шт …»
    if len(lines) >= 2:
        return lines, width_specified
    return lines, width_specified


def _sync_cart_fields(data: dict[str, Any]) -> None:
    cart = list(data.get("cart") or [])
    if not cart:
        return
    data["quantity"] = str(sum(int(r.get("quantity") or 0) for r in cart))
    data["total_amount"] = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
    if len(cart) == 1:
        data["product_type"] = cart[0].get("product_name") or data.get("product_type")
        data["unit_price"] = cart[0].get("unit_price")
        if cart[0].get("size_mm"):
            data["size_mm"] = cart[0]["size_mm"]
    else:
        data["product_type"] = "; ".join(
            f"{r.get('product_name')} × {r.get('quantity')}" for r in cart
        )
        data["unit_price"] = None
    data["quote_ready"] = True


def _build_cart_from_lines(data: dict[str, Any], lines: list[dict[str, Any]]) -> dict | None:
    """Повністю зібрати кошик з розпарсених позицій. Повертає останній успішний прорахунок."""
    cart: list[dict[str, Any]] = []
    last: dict | None = None
    for line in lines:
        result = _quote_calculate(
            str(line["product_type"]),
            int(line["quantity"]),
            size_mm=str(line["size_mm"]) if line.get("size_mm") else None,
            text=str(line.get("text") or line["product_type"]),
        )
        if not result.get("success"):
            continue
        if line.get("product_name"):
            result["product_name"] = line["product_name"]
        if line.get("circ_mm"):
            result["circ_mm"] = line["circ_mm"]
        if result.get("multi_quotes"):
            for q in result["multi_quotes"]:
                if q.get("success"):
                    size_mm = line.get("size_mm") or q.get("size_mm")
                    cart.append(_cart_line_from_quote(q, str(size_mm) if size_mm else None))
                    last = q
            continue
        size_mm = line.get("size_mm")
        if not size_mm and result.get("product_key"):
            m = re.search(r"(\d+)mm", str(result["product_key"]))
            if m:
                size_mm = m.group(1)
        cart.append(_cart_line_from_quote(result, str(size_mm) if size_mm else None))
        last = result
    if not cart or not last:
        return None
    data["cart"] = cart
    _sync_cart_fields(data)
    return last


def _format_silicone_variant_message(
    quotes: list[dict],
    *,
    width_specified: bool,
    cart: list | None = None,
) -> str:
    parts: list[str] = []
    if not width_specified:
        parts.append(
            "Який розмір по висоті вам потрібен — 5 мм, 12 мм чи 20 мм?\n"
            "Нижче вартість за найходовішим 12 мм, друк лого одним кольором:"
        )
    else:
        parts.append("З друком лого одним кольором:")
    total = 0.0
    for q in quotes:
        qty = q.get("quantity")
        name = (q.get("product_name") or "силіконовий браслет").strip()
        # «Силіконовий браслет 160×12 мм (дитячий, синій)» → «синій дитячий 160×12 мм»
        extra = ""
        m = re.search(r"(\d+×\d+\s*мм)\s*(?:\(([^)]+)\))?", name)
        if m:
            dims = m.group(1)
            bits = [b.strip() for b in (m.group(2) or "").split(",") if b.strip()]
            color = next((b for b in bits if b not in ("дитячий", "підлітковий", "дорослий")), "")
            age = next((b for b in bits if b in ("дитячий", "підлітковий", "дорослий")), "")
            extra = " ".join(x for x in (color, age, dims) if x)
        label = extra or name[0].lower() + name[1:]
        try:
            unit_n = float(q.get("unit_price") or 0)
        except (TypeError, ValueError):
            unit_n = 0.0
        unit = f"{unit_n:.2f}".replace(".", ",")
        parts.append(f"{qty} шт {label} — {unit} грн/шт")
        total += float(q.get("total_amount") or 0)
    if cart and len(cart) > len(quotes):
        parts.extend(["", "У замовленні зараз:"])
        total = 0.0
        for item in cart:
            parts.append(
                f"- {item.get('product_name')} × {item.get('quantity')} шт — "
                f"{format_money(item.get('total_amount'))} грн"
            )
            total += float(item.get("total_amount") or 0)
    parts.append(f"Разом: {format_money(total)} грн.")
    parts.extend(
        [
            "",
            "Оформлюємо? Якщо треба ще позицію в це замовлення — напишіть яка і скільки.",
        ]
    )
    return "\n".join(parts)


def _try_silicone_variant_order(data: dict[str, Any], text: str) -> tuple[str, dict] | None:
    lines, width_specified = _extract_silicone_variant_lines(text, data)
    if len(lines) < 2:
        return None
    keep: list[dict] = []
    if _wants_add_to_cart(text):
        keep = list(data.get("cart") or [])
    else:
        keep = [
            r
            for r in (data.get("cart") or [])
            if _product_family(str(r.get("product_name") or "")) != "silicone"
        ]
    tmp: dict[str, Any] = {}
    last = _build_cart_from_lines(tmp, lines)
    if not last:
        return None
    new_cart = list(tmp.get("cart") or [])
    data["cart"] = keep + new_cart
    _sync_cart_fields(data)
    data.pop("size_mm", None)
    if not width_specified:
        data["size_mm"] = "12"
        data["pending_width_confirm"] = True
    msg = _format_silicone_variant_message(
        new_cart, width_specified=width_specified, cart=data["cart"]
    )
    return msg, last


def _quote_calculate(
    product_type: str,
    quantity: int,
    *,
    size_mm: str | None = None,
    urgent: bool = False,
    text: str = "",
    data: dict[str, Any] | None = None,
) -> dict:
    """Прорахунок з урахуванням кількох варіантів друку в одному запиті."""
    calc_text = text or product_type
    pending = (data or {}).get("pending_print_colors") or []
    if pending and not PricingCalculator.detect_print_color_list(calc_text):
        bits = " та ".join(f"{c} колір" + ("и" if c != 1 else "") for c in pending)
        calc_text = f"{calc_text} {bits}".strip()
    multi = calculator.calculate_multi_variants(
        product_type, quantity, size_mm=size_mm, text=calc_text
    )
    if multi:
        return multi
    return calculator.calculate(
        product_type=product_type,
        quantity=quantity,
        size_mm=size_mm,
        urgent=urgent,
        text=calc_text,
    )


def _apply_product_hint(data: dict[str, Any], hint: str, *, clear_cart: bool = False) -> None:
    """Оновити товар у стані; скинути несумісні ціну/розмір при зміні типу."""
    old = str(data.get("product_type") or "")
    # clear_cart навіть якщо hint уже виставлений (_merge_order_hints раніше)
    if clear_cart:
        data.pop("cart", None)
    if hint == old:
        return
    old_l, new_l = old.lower(), hint.lower()
    data["product_type"] = hint
    if not old:
        return
    family_changed = _product_family(old_l) != _product_family(new_l)
    if family_changed or hint != old:
        data.pop("size_mm", None)
        data.pop("total_amount", None)
        data.pop("unit_price", None)
        data.pop("quote_ready", None)
        if family_changed:
            data.pop("quantity", None)


def _merge_order_hints(data: dict[str, Any], text: str) -> None:
    """Підтягнути товар/кількість з фрази клієнта (актуальне повідомлення важливіше за старе)."""
    # «ще світловідбиваючі…» — кошик не чіпаємо; запам'ятовуємо товар для наступної кількості
    if _wants_add_to_cart(text):
        prod = _extract_product_hint(text)
        if prod:
            data["pending_product_type"] = prod
        qty = _extract_qty(text)
        if qty:
            data["quantity"] = qty
        return
    # спочатку товар (може скинути qty), потім кількість з цього ж повідомлення
    prod = _extract_product_hint(text)
    if prod:
        data["last_topic"] = prod
        # «що таке RFID» — тема, не позиція замовлення
        if not _is_definition_question(text) and not _wants_catalog_overview(text):
            _apply_product_hint(data, prod, clear_cart=False)
            data.pop("pending_product_type", None)
    size_m = re.search(r"(\d+)\s*мм", (text or "").lower())
    if size_m:
        data["size_mm"] = size_m.group(1)
    ribbon_src = (text or "").lower()
    if "гладеньк" in ribbon_src:
        data["lanyard_ribbon"] = "гладенька"
    elif "репсов" in ribbon_src:
        data["lanyard_ribbon"] = "репсова"
    qty = _extract_qty(text)
    if qty:
        data["quantity"] = qty
    # slap: запам'ятати 1 і 4 кольори друку доки не прийде тираж
    colors = PricingCalculator.detect_print_color_list(text)
    blob = f"{prod or ''} {text or ''} {data.get('product_type') or ''}".lower()
    if colors and ("slap" in blob or "слеп" in blob):
        data["pending_print_colors"] = colors
    elif prod and _product_family(prod) not in {"slap", ""}:
        data.pop("pending_print_colors", None)


def _extract_edrpou(text: str) -> str | None:
    m = re.search(r"\b(\d{8,10})\b", text or "")
    return m.group(1) if m else None


async def _load_saved_profile(db: AsyncSession, chat_id: str) -> dict[str, Any]:
    """Дані клієнта/доставки з минулих замовлень (Client + DeliveryInfo)."""
    profile: dict[str, Any] = {}
    client = (
        await db.execute(select(Client).where(Client.chat_id == str(chat_id)))
    ).scalar_one_or_none()
    if client:
        for key in ("last_name", "first_name", "phone", "city"):
            val = getattr(client, key, None)
            if val:
                profile[key] = val
    if client:
        delivery = (
            await db.execute(select(DeliveryInfo).where(DeliveryInfo.client_id == client.id))
        ).scalar_one_or_none()
        if delivery:
            if delivery.delivery_type:
                profile["delivery_type"] = delivery.delivery_type
            if delivery.warehouse:
                profile["warehouse"] = delivery.warehouse
            if delivery.street_address:
                profile["street_address"] = delivery.street_address
            if delivery.city and not profile.get("city"):
                profile["city"] = delivery.city
            if delivery.recipient_phone and not profile.get("phone"):
                profile["phone"] = delivery.recipient_phone
            if delivery.recipient_last_name and not profile.get("last_name"):
                profile["last_name"] = delivery.recipient_last_name
            if delivery.recipient_first_name and not profile.get("first_name"):
                profile["first_name"] = delivery.recipient_first_name
            if delivery.recipient_patronymic:
                profile["recipient_patronymic"] = delivery.recipient_patronymic
    return profile


def _normalize_words(text: str) -> str:
    """Правильна орфографія для відображення: Київ, не КИЇв."""
    raw = (text or "").strip()
    if not raw:
        return raw
    lower = raw.lower().replace("ї", "ї")
    if lower in KNOWN_CITIES:
        return KNOWN_CITIES[lower]
    parts = []
    for word in raw.split():
        key = word.lower()
        if key in KNOWN_CITIES:
            parts.append(KNOWN_CITIES[key])
        elif word.isupper() or word.islower() or (word[:1].islower() and any(c.isupper() for c in word[1:])):
            parts.append(word[:1].upper() + word[1:].lower())
        else:
            parts.append(word[:1].upper() + word[1:] if word else word)
    return " ".join(parts)


def _clarify_product_reply(text: str, data: dict[str, Any]) -> str | None:
    """Товар назвали, тиражу немає — спитати наступний факт, не «не зрозумів»."""
    lower = (text or "").strip().lower()
    if not lower:
        return None
    if _extract_qty(text):
        return None
    if _is_definition_question(text) or _wants_catalog_overview(text):
        return None
    if any(
        w in lower
        for w in (
            "доставк",
            "нова пошт",
            "оплат",
            "liqpay",
            "лікпей",
            "рахунок",
            "єдрпоу",
            "менеджер",
            "строк",
            "термін",
            "означа",
            "не знаю",
            "макет",
            "розкаж",
            "що таке",
            "як робит",
            "з чого",
            "фестивал",
            "корпоратив",
            "концерт",
            "що краще",
            "що радите",
            "швидш",
            "термінов",
            "робите",
            "робиш",
            "чи є",
            "є у вас",
            "маєте",
            "виготовляєте",
            "продаєте",
            "виготовл",
            "цін",
            "варт",
            "кошту",
        )
    ):
        return None
    if (_looks_like_question(text) or "?" in text) and not _extract_product_hint(text):
        return None

    size = data.get("size_mm")
    size_m = re.search(r"(\d+)\s*мм", lower)
    if size_m:
        size = size_m.group(1)

    only_bracelet = bool(re.fullmatch(r"браслет[иі]?", lower))
    known = str(data.get("product_type") or data.get("pending_product_type") or "")
    if only_bracelet and not known:
        return "Які браслети рахувати — силіконові, світловідбиваючі, Tyvek чи RFID?"
    if only_bracelet and known:
        if "силікон" in known.lower():
            if size:
                return f"Силіконові {size} мм. Яка кількість?"
            return "Силіконові — 5, 12 чи 20 мм? І який тираж?"
        return f"Рахуємо «{known}». Яка кількість?"

    incoming = _extract_product_hint(text)
    hint = incoming or known or None
    # тираж уже є — не перепитуй, хай спрацює прорахунок / індивідуал
    if hint and data.get("quantity") and (not incoming or incoming == known):
        return None
    if not hint:
        if known:
            return None
        if any(w in lower for w in ("рахуй", "порахуй", "прорахуй", "порахув", "давайте")):
            return "Що саме рахувати — силікон, світловідбиваючі, чашки?"
        return None

    hint_l = str(hint).lower()
    if "чашк" in hint_l or "екочаш" in lower or "еко-чаш" in lower:
        return "Еко-чашки з лого робимо. Який тираж?"
    if _is_reflective_text(hint_l) or _is_reflective_text(lower):
        return "Світловідбиваючі. Який тираж і розмір — 300×30 чи 400×30?"
    if "rfid" in hint_l or "rfid" in lower:
        return "RFID рахуємо індивідуально. Який тираж і для чого браслети?"
    if "tyvek" in hint_l:
        return "Tyvek / контрольні. Який тираж?"
    if "slap" in hint_l:
        return "Slap-браслети. Яка кількість?"
    if "шнур" in hint_l:
        if _wants_lanyard_stock(lower):
            return LANYARD_COLORS_REPLY
        return LANYARD_PRINT_GUIDE
    if "кишеньк" in hint_l:
        return BADGE_POCKET_GUIDE
    if "кардхолдер" in hint_l or "картхолдер" in hint_l:
        return CARDHOLDER_GUIDE_REPLY
    if "телефон" in hint_l or "чохол" in hint_l:
        return PHONE_ACCESSORY_GUIDE
    if "ретрактор" in hint_l:
        return "Ретрактори (також кліп-рулетка / бейдж з витяжним тросиком). З друком чи без? Який тираж?"
    if "брелок" in hint_l:
        return "Брелоки з лого: з карабіном, чорним карабіном, кільцем або кольоровим карабіном — 20 чи 25 мм. Який варіант і тираж?"
    if "тканинн" in hint_l:
        return (
            "Тканинні браслети: стрічка 15 мм, довжина 350 мм. "
            "Фіксатори чорний або білий (на ціну не впливає). Який тираж?"
        )
    if "шапоч" in hint_l or "плаванн" in lower:
        start = calculator.starting_quote(hint or "шапочки", text=text or "")
        if start:
            return f"{start} Який тираж?"
        return "Шапочки для плавання. Яка кількість?"
    if "силікон" in hint_l or ("силікон" in lower and _is_silicone_bracelet_text(hint_l or lower)):
        if not size:
            return "Силіконові з лого — 5, 12 чи 20 мм? І який тираж?"
        return f"Силіконові {size} мм з друком лого. Яка кількість?"
    return f"{hint}. Яка кількість — порахую з прайсу?"


def _individual_quote_reply(name: str, qty: str) -> str:
    clean = str(name or "цю позицію").strip("«»\" ")
    return (
        f"На {clean} {qty} шт готової ціни немає — рахуємо індивідуально. "
        "Можу передати менеджеру, він прорахує."
    )


def _is_presence_nudge(text: str) -> bool:
    t = (text or "").strip().lower()
    if re.fullmatch(r"[?!.…]+", t or ""):
        return True
    return t in {
        "ау",
        "алло",
        "є?",
        "тут?",
        "ти тут",
        "ти тут?",
        "ви тут",
        "ви тут?",
        "а ви тут",
        "а ви тут?",
        "а у ви тут",
    }


def _wants_stored_quote(text: str) -> bool:
    if _is_definition_question(text) or _wants_catalog_overview(text):
        return False
    return _wants_price_or_quote(text)


def _presence_reply(data: dict[str, Any]) -> str:
    name = str(data.get("product_type") or data.get("pending_product_type") or "").strip()
    qty = str(data.get("quantity") or "").strip()
    if name and qty:
        if data.get("individual_quoted"):
            return (
                f"Так, я тут. По {name} {qty} шт готової ціни немає — індивідуально. "
                "Передаю менеджеру?"
            )
        return f"Так, я тут. По {name} {qty} шт зараз дорахую."
    if name:
        return f"Так, я тут. {name} — напишіть тираж, порахую."
    return "Так, я тут. Що порахувати?"


def _existence_reply(text: str) -> str | None:
    """«Робите RFID? / чашки є?» — спочатку так/ні, потім тираж."""
    lower = (text or "").strip().lower()
    if not any(
        w in lower
        for w in (
            "робите",
            "робиш",
            "чи є",
            "є у вас",
            "у вас є",
            "маєте",
            "виготовляєте",
            "продаєте",
            "які є",
            "які у вас",
        )
    ) and not re.search(r"(?:^|\s)є\s*\??\s*$", lower):
        return None
    hint = _extract_product_hint(text)
    if not hint and any(w in lower for w in ("чашк", "кухл")):
        hint = "еко-чашки"
    if not hint:
        return None
    # загальний запит по шнурках — повний FAQ, не «який тираж?»
    if "шнур" in hint.lower() or "бейдж" in lower:
        if not _extract_qty(text) and not re.search(r"\d+\s*мм", lower):
            faq = (calculator.templates or {}).get("lanyard_general")
            if faq:
                return faq
    return f"Так, {hint} робимо. Для чого і який тираж?"


def _declines_more_items(text: str) -> bool:
    """Клієнт лишає поточний прорахунок і не хоче другу позицію."""
    lower = (text or "").lower()
    if any(w in lower for w in ("пізніше", "ще подумаю", "не зараз", "не підход")):
        return False
    return any(
        p in lower
        for p in (
            "більше нічого",
            "більше не треба",
            "нічого більше",
            "інше не треба",
            "іншого не треба",
            "не треба товар",
            "не потрібно товар",
            "більше не потріб",
            "тільки це",
            "лише це",
            "і все",
            "та все",
            "нічого не потріб",
            "більше нічого не потріб",
        )
    )


def _is_restating_confirm(text: str) -> bool:
    """«я ж написав вище так» — це підтвердження, не нове замовлення."""
    lower = (text or "").lower()
    if _wants_add_to_cart(text) and not _declines_more_items(text):
        return False
    return any(
        p in lower
        for p in (
            "я ж написав",
            "я ж написала",
            "я вже написав",
            "я вже писав",
            "я ж сказав",
            "я ж казав",
            "вище так",
            "я ж вище",
            "ж написав вище",
        )
    )


def _same_product_as_quote(text: str, data: dict[str, Any]) -> bool:
    hint = _extract_product_hint(text)
    if not hint:
        return False
    known = str(data.get("product_type") or data.get("pending_product_type") or "")
    cart = " ".join(str(r.get("product_name") or "") for r in (data.get("cart") or []))
    fam = _product_family(hint)
    if fam and fam not in {"", "other"}:
        if fam == _product_family(known) or fam == _product_family(cart):
            return True
    return hint.lower() in known.lower() or hint.lower() in cart.lower()


def _wants_add_to_cart(text: str) -> bool:
    lower = (text or "").lower()
    if _declines_more_items(text):
        return False
    if _wants_remove_from_cart(text):
        return False
    # «вище» містить «ще» — це не «додай ще товар»
    if re.search(r"(?<![а-яіїєґa-z])ще(?![а-яіїєґa-z])", lower) and "вище" not in lower:
        return True
    return any(
        p in lower
        for p in (
            "а ще",
            "і ще",
            "також",
            "додай",
            "додати",
            "і світловідб",
            "і силікон",
            "ще товар",
            "ще браслет",
            "добав",
            "добавить",
        )
    )


def _wants_remove_from_cart(text: str) -> bool:
    """«прибери slap / викинь шапочки / без браслетів / прибери зі списку»."""
    lower = (text or "").lower()
    if not lower.strip():
        return False
    hard = (
        "прибер",
        "прибир",
        "видал",
        "викинь",
        "викин",
        "убери",
        "прибрати",
        "видалити",
        "скасуй позиц",
        "скасувати позиц",
        "прибери із",
        "прибери из",
        "прибери зі",
        "прибери з ",
        "зі списку",
        "із списку",
        "из списка",
        "з замовлен",
        "із замовлен",
        "з кошик",
        "з цього списк",
    )
    if any(m in lower for m in hard):
        return True
    soft = ("не треба", "не потріб", "без ")
    if any(m in lower for m in soft) and (
        _extract_product_hint(text)
        or any(w in lower for w in ("списк", "позиц", "їх ", "це ", "той ", "ті "))
    ):
        # «більше не треба товару … замовляю» — це підтвердження, не зняття
        if _declines_more_items(text) or any(
            w in lower for w in ("замовляю", "оформл", "беру", "і все", "та все", "тільки це")
        ):
            return False
        if any(w in lower for w in ("оформл", "платити", "оплат")) and not _extract_product_hint(text):
            return False
        return True
    return False


def _cart_row_matches_remove(row: dict[str, Any], text: str, hint: str | None) -> bool:
    name = str(row.get("product_name") or "").lower()
    key = str(row.get("product_key") or "").lower()
    blob = f"{name} {key}"
    lower = (text or "").lower()
    want_size = None
    sm = re.search(r"(\d+)\s*мм", lower)
    if sm:
        want_size = sm.group(1)
    row_size = str(row.get("size_mm") or "")
    if want_size:
        size_ok = (
            row_size == want_size
            or f"{want_size} мм" in name
            or f"{want_size}mm" in key
            or f"×{want_size}" in name
            or f"x{want_size}" in name
        )
        if not size_ok:
            return False
    if hint:
        fam = _product_family(hint)
        if fam and fam not in {"", "other"} and fam == _product_family(blob):
            return True
        if hint.lower() in name or hint.lower() in key:
            return True
    # явні маркери в тексті без окремого hint
    checks = (
        ("slap", ("slap", "слеп")),
        ("слеп", ("slap", "слеп")),
        ("шапоч", ("шапоч", "плаван")),
        ("чашк", ("чашк",)),
        ("шнур", ("шнур", "бейдж")),
        ("світл", ("світл", "відбив")),
        ("tyvek", ("tyvek", "контрол")),
        ("силікон", ("силікон",)),
        ("браслет", ("браслет", "силікон")),
        ("тканин", ("тканин",)),
        ("ретрактор", ("ретрактор",)),
        ("брелок", ("брелок",)),
        ("rfid", ("rfid",)),
        ("кишеньк", ("кишеньк",)),
        ("кардхолдер", ("кардхолдер",)),
    )
    for needle, needles in checks:
        if needle in lower and any(n in blob for n in needles):
            return True
    # «приберіть 20 мм» — лише за розміром, якщо вже відсіяли інші
    if want_size and size_ok:
        return True
    return False


def _remove_from_cart(data: dict[str, Any], text: str) -> tuple[list[dict], list[dict]]:
    """Повертає (видалені, залишок)."""
    cart = list(data.get("cart") or [])
    if not cart:
        return [], []
    hint = _extract_product_hint(text)
    lower = (text or "").lower()
    remove_all = any(
        w in lower
        for w in (
            "все прибер",
            "прибери все",
            "очисти",
            "очистити",
            "весь список",
            "все зі списку",
            "все із списку",
            "всі позиц",
            "всі товари",
        )
    )
    if remove_all:
        removed = cart
        data["cart"] = []
        for k in ("product_type", "quantity", "unit_price", "total_amount", "size_mm", "quote_ready"):
            data.pop(k, None)
        return removed, []

    kept: list[dict] = []
    removed: list[dict] = []
    for row in cart:
        if _cart_row_matches_remove(row, text, hint):
            removed.append(row)
        else:
            kept.append(row)

    # fallback: одна позиція і явне «прибери» без впізнаного товару → прибрати її
    # але НЕ якщо в тексті названо ІНШИЙ товар, якого в кошику немає
    if not removed and len(cart) == 1 and any(
        w in lower for w in ("прибер", "видал", "убери", "викинь", "викин")
    ):
        hinted_other = bool(hint) and not _cart_row_matches_remove(cart[0], text, hint)
        if not hinted_other:
            removed = cart
            kept = []

    data["cart"] = kept
    if kept:
        _sync_cart_fields(data)
        data["quote_ready"] = True
    else:
        for k in ("product_type", "quantity", "unit_price", "total_amount", "size_mm", "quote_ready"):
            data.pop(k, None)
    return removed, kept


def _format_cart_after_remove(removed: list[dict], kept: list[dict]) -> str:
    if not removed:
        return (
            "Не знайшов такої позиції в поточному списку. "
            "Напишіть що саме прибрати — наприклад «прибери slap» або «прибери шапочки»."
        )
    gone = ", ".join(str(r.get("product_name") or "позицію") for r in removed)
    if not kept:
        return (
            f"Прибрав: {gone}. Список зараз порожній. "
            "Напишіть, що порахувати далі."
        )
    lines = [f"Прибрав: {gone}. Залишилось:"]
    total = 0.0
    for row in kept:
        amt = float(row.get("total_amount") or 0)
        total += amt
        lines.append(
            f"- {row.get('product_name')} × {row.get('quantity')} шт — {format_money(amt)} грн"
        )
    lines.append(f"Разом: {format_money(total)} грн.")
    lines.append("Оформлюємо? Якщо треба ще щось змінити — напишіть.")
    return "\n".join(lines)


def _cart_line_from_quote(result: dict, size_mm: str | None = None) -> dict[str, Any]:
    return {
        "product_key": result.get("product_key"),
        "product_name": result.get("product_name") or result.get("product_type") or "Товар",
        "quantity": int(result.get("quantity") or 0),
        "unit_price": float(result.get("unit_price") or 0),
        "total_amount": float(result.get("total_amount") or 0),
        "production_days": result.get("production_days"),
        "size_mm": size_mm or result.get("size_mm"),
        "print_variant": result.get("print_variant"),
        "accessories": result.get("accessories"),
    }


def _apply_quote_to_cart(data: dict[str, Any], result: dict, text: str = "") -> list[dict]:
    """Оновити кошик: кожна позиція окремо; різні товари не зливаються в один рядок."""
    size_mm = result.get("size_mm") or data.get("size_mm")
    if not size_mm and result.get("product_key"):
        m = re.search(r"(\d+)mm", str(result["product_key"]))
        if m:
            size_mm = m.group(1)
    item = _cart_line_from_quote(result, str(size_mm) if size_mm else None)
    cart = list(data.get("cart") or [])
    correcting = _is_quote_correction(text) or "виправ" in (text or "").lower()

    def _same_line(row: dict) -> bool:
        if row.get("product_key") and item.get("product_key"):
            return (
                row.get("product_key") == item.get("product_key")
                and row.get("product_name") == item.get("product_name")
            )
        return row.get("product_name") == item.get("product_name")

    def _same_family(row: dict) -> bool:
        return _product_family(str(row.get("product_name") or "")) == _product_family(
            str(item.get("product_name") or "")
        )

    same_idx = next((i for i, row in enumerate(cart) if _same_line(row)), None)
    if correcting and same_idx is None:
        # виправлення ширини шнурка 15→20 — замінити рядок того ж сімейства
        same_idx = next((i for i, row in enumerate(cart) if _same_family(row)), None)
    # «я вище писав X» про ІНШИЙ товар (немає рядка того сімейства) — замінити кошик
    if correcting and same_idx is None and cart and _extract_product_hint(text):
        new_fam = _product_family(str(item.get("product_name") or ""))
        has_same_fam = any(
            _product_family(str(r.get("product_name") or "")) == new_fam for r in cart
        )
        if not has_same_fam:
            cart = [item]
            data["cart"] = cart
            data["product_type"] = item["product_name"]
            data["quantity"] = str(item["quantity"])
            data["total_amount"] = float(item.get("total_amount") or 0)
            data["unit_price"] = item.get("unit_price")
            if item.get("size_mm"):
                data["size_mm"] = item["size_mm"]
            data["quote_ready"] = True
            return cart

    different_product = bool(cart) and (
        item.get("product_key") != cart[-1].get("product_key")
        or item.get("product_name") != cart[-1].get("product_name")
    )
    append = _wants_add_to_cart(text) or (
        different_product and _extract_product_hint(text) is not None and not correcting
    ) or (different_product and same_idx is None and not correcting)

    if not cart:
        cart = [item]
    elif same_idx is not None and (correcting or not _wants_add_to_cart(text)):
        cart[same_idx] = item
    elif append or (different_product and not correcting):
        if same_idx is not None and _wants_add_to_cart(text):
            cart.append(item)
        elif same_idx is None:
            cart.append(item)
        else:
            cart[same_idx] = item
    else:
        if same_idx is not None:
            cart[same_idx] = item
        elif len(cart) == 1:
            cart = [item]
        else:
            cart.append(item)

    data["cart"] = cart
    data["product_type"] = item["product_name"] if len(cart) == 1 else "; ".join(
        f"{r['product_name']} × {r['quantity']}" for r in cart
    )
    data["quantity"] = str(item["quantity"]) if len(cart) == 1 else str(
        sum(int(r.get("quantity") or 0) for r in cart)
    )
    data["total_amount"] = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
    data["unit_price"] = item["unit_price"] if len(cart) == 1 else None
    if item.get("size_mm"):
        data["size_mm"] = item["size_mm"]
    data["quote_ready"] = True
    return cart


def _format_quote_reply(result: dict, data: dict[str, Any]) -> str:
    cart = list(data.get("cart") or [])
    name = str(result.get("product_name") or "")
    if "кишеньк" in name.lower() and not (cart and len(cart) > 1):
        qty = result.get("quantity")
        total = format_money(result.get("total_amount"))
        return (
            f"{name} — {qty} шт — {total} грн.\n\n"
            "Оформлюємо? Якщо треба ще позицію в це замовлення — напишіть яка і скільки."
        )
    return format_quote_message(result, with_confirm=True, cart=cart if len(cart) > 1 else None)


def _format_payment_message(data: dict[str, Any], url: str) -> str:
    cart = list(data.get("cart") or [])
    lines = ["Оплата онлайн через сервіс LiqPay:"]
    if cart:
        for row in cart:
            lines.append(
                f"{row.get('product_name')} × {row.get('quantity')} шт — "
                f"{format_money(row.get('total_amount'))} грн"
            )
        if len(cart) > 1:
            lines.append(f"Разом: {format_money(data.get('total_amount'))} грн")
    else:
        product = data.get("product_type") or "Замовлення"
        qty = data.get("quantity")
        total = format_money(data.get("total_amount"))
        qty_bit = f" × {qty} шт" if qty else ""
        lines.append(f"{product}{qty_bit} — {total} грн")
    lines.extend(
        [
            "",
            f"Посилання на оплату: {url}",
            "",
            "Після оплати я сам побачу надходження і напишу вам.",
        ]
    )
    return "\n".join(lines)


def _format_profile_confirm(profile: dict[str, Any]) -> str:
    lines = ["Бачу ваші дані з минулого разу:"]
    name_bits = [
        _normalize_words(profile["first_name"]) if profile.get("first_name") else None,
        _normalize_words(profile["last_name"]) if profile.get("last_name") else None,
    ]
    uniq = []
    for x in name_bits:
        if x and x not in uniq:
            uniq.append(x)
    if uniq:
        lines.append(f"- Ім'я та прізвище: {' '.join(uniq)}")
    if profile.get("phone"):
        lines.append(f"- Телефон: {profile['phone']}")
    if profile.get("city"):
        lines.append(f"- Місто: {_normalize_words(str(profile['city']))}")
    dtype = profile.get("delivery_type")
    if dtype == DELIVERY_TYPE_ADDRESS and profile.get("street_address"):
        addr = _normalize_words(str(profile["street_address"]))
        lines.append(f"- Доставка: Адреса — {addr}")
    elif profile.get("warehouse"):
        where = "Поштомат" if dtype == DELIVERY_TYPE_POSTOMAT else "Відділення"
        lines.append(f"- Доставка: {where} № {profile['warehouse']} Нової пошти")
    lines.append("")
    lines.append("Вони актуальні?")
    return "\n".join(lines)


def _apply_profile_to_data(data: dict[str, Any], profile: dict[str, Any]) -> None:
    for key in ("last_name", "first_name", "phone", "city"):
        if profile.get(key):
            data[key] = profile[key]
    if profile.get("delivery_type"):
        data["saved_delivery_type"] = profile["delivery_type"]
    if profile.get("warehouse"):
        data["saved_warehouse"] = profile["warehouse"]
    if profile.get("street_address"):
        data["saved_street_address"] = profile["street_address"]
    if profile.get("recipient_patronymic"):
        data["saved_patronymic"] = profile["recipient_patronymic"]


async def _try_restore_saved_profile(
    db: AsyncSession, state: ConversationState, data: dict[str, Any]
) -> ChatMessageResponse | None:
    profile = data.get("_profile") or await _load_saved_profile(db, state.chat_id)
    if not (profile.get("last_name") and profile.get("phone")):
        return None
    _apply_profile_to_data(data, profile)
    data.pop("_profile", None)
    state.step = "collect_crm"
    _save_data(state, data)
    await db.commit()
    missing = _missing_crm_fields(data)
    if missing:
        extra = _next_crm_question(data) or ""
        return ChatMessageResponse(
            response=f"Ок, контакти ті самі. {extra}".strip(),
            action="collect_order_data",
        )
    return await _step_collect_crm(db, state, data, text=".")


async def _begin_order_collection(
    db: AsyncSession, state: ConversationState, data: dict, chat_id: str
) -> ChatMessageResponse:
    """Старт оформлення: підтвердити старі дані або збирати нові."""
    data.pop("quote_ready", None)
    # товар/кількість з поточної розмови лишаємо; контакти — підтверджуємо з профілю
    profile = await _load_saved_profile(db, chat_id)
    if profile.get("last_name") and profile.get("phone"):
        data["_profile"] = profile
        state.step = "confirm_crm"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_format_profile_confirm(profile),
            action="confirm_client_data",
        )

    state.step = "collect_crm"
    _save_data(state, data)
    await db.commit()
    missing = _missing_crm_fields(data)
    if missing:
        return ChatMessageResponse(
            response=_next_crm_question(data) or ORDER_PROMPT,
            action="collect_order_data",
        )
    return ChatMessageResponse(response=ORDER_PROMPT, action="collect_order_data")


def _next_crm_question(data: dict[str, Any]) -> str:
    missing = _missing_crm_fields(data)
    if not missing:
        return ""
    key = missing[0]
    if key == "quantity":
        prod = data.get("product_type") or "товару"
        return f"Яка кількість потрібна для «{prod}»? Напишіть число, наприклад: 100"
    return CRM_FIELD_QUESTIONS.get(key, f"Уточніть: {key}")


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if _is_presence_nudge(t):
        return False
    if "?" in t:
        return True
    if _asks_where_or_what(t):
        return True
    anywhere = (
        "а які", "які є", "що є", "що у вас", "які у вас", "покажи", "розкажи",
        "хочу прорахувати", "хочу вибрати", "хочу подивитись", "які товари",
        "що пропонуєте", "які браслети", "який асортимент",
    )
    prefixes = (
        "де ", "як ", "що ", "чи ", "чому", "навіщо", "поясни", "підкаж",
        "а як", "а що", "а де", "а чи",
    )
    return any(s in t for s in anywhere) or any(t.startswith(s) for s in prefixes)


def _asks_where_or_what(text: str) -> bool:
    """Питання «де взяти / що це / не знаю» — вихід з поточного поля."""
    lower = (text or "").strip().lower()
    if not lower:
        return False
    markers = (
        "де знайт",
        "де взяти",
        "де подиви",
        "де шукати",
        "де його",
        "де цей",
        "як знайт",
        "як взяти",
        "що таке",
        "навіщо",
        "поясни",
        "підкаж",
        "не знаю",
        "а де",
        "а як ",
        "а що ",
    )
    return any(m in lower for m in markers)


def _is_question_about_field(text: str, field: str | None) -> bool:
    if not field:
        return False
    lower = (text or "").lower()
    aliases = {
        "edrpou": ("єдрпоу", "едрпоу", "edrpou", "код"),
        "person_name": ("ім'я", "імя", "прізвищ", "фимил", "ім’я"),
        "phone": ("телефон", "номер"),
        "city": ("міст", "город"),
        "ipn": ("іпн", "инн", "іпп"),
        "company_name": ("назв", "організац", "компані"),
        "company_address": ("адрес",),
        "quantity": ("кількіст", "тираж", "штук"),
        "product_type": ("товар", "браслет"),
        "delivery_type": ("достав", "відділен", "поштомат"),
        "delivery_field": ("відділен", "адрес", "поштомат"),
        "payment": ("оплат", "liqpay", "лікпей", "рахунок"),
        "confirm": ("дані", "актуаль"),
    }
    if not _asks_where_or_what(lower) and not _looks_like_question(text):
        return False
    if field == "edrpou":
        return True
    keys = aliases.get(field) or ()
    return any(k in lower for k in keys) or ("його" in lower and field == "edrpou")


def _field_help_reply(field: str | None, text: str) -> str | None:
    lower = (text or "").lower()
    mentions_edrpou = any(w in lower for w in ("єдрпоу", "едрпоу", "edrpou"))
    about_edrpou = field == "edrpou" and (
        _asks_where_or_what(text) or mentions_edrpou or "його" in lower
    )
    if about_edrpou or (mentions_edrpou and _asks_where_or_what(text)):
        return EDRPOU_WHERE_REPLY
    return None


def _wants_consult_not_order(text: str) -> bool:
    lower = text.lower()
    consult_signals = (
        "прорахувати", "розрахувати", "порахувати", "вибрати", "підібрати", "які є",
        "що є", "асортимент", "каталог", "прайс", "ціна", "коштує",
        "які у вас", "що у вас", "консультац",
    )
    order_only = (
        "оформити замовлення",
        "оформлюємо замовлення",
        "хочу оформити",
        "готовий замовити",
        "оформлюйте",
        "беріть замовлення",
    )
    if any(s in lower for s in order_only):
        return False
    if any(s in lower for s in consult_signals):
        return True
    # «прорахувати та замовити» — спочатку консультація
    if "замовити" in lower and any(s in lower for s in ("прораху", "вибра", "підібр", "товар")):
        return True
    return False


def _has_word(text: str, *words: str) -> bool:
    """Ціле слово: «ні» ≠ літера в «актуальні»."""
    lower = (text or "").lower()
    return any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", lower) for w in words)


def _is_profile_ok(text: str) -> bool:
    lower = (text or "").lower()
    if _is_profile_reject(text):
        return False
    return any(
        w in lower
        for w in (
            "актуальн",
            "ті сам",
            "ті ж",
            "ті дан",
            "тими ж",
            "без змін",
            "так само",
            "залишай",
            "не змін",
            "я ж пиш",
            "я ж кажу",
        )
    )


def _is_profile_reject(text: str) -> bool:
    lower = (text or "").lower().strip()
    if _declines_more_items(text) or _is_restating_confirm(text):
        return False
    if "актуальн" in lower and "не актуаль" not in lower and "неактуаль" not in lower:
        return False
    if _has_word(lower, "ні", "нет", "no"):
        return True
    return any(
        w in lower
        for w in ("не актуаль", "нові дан", "змінил", "інші дан", "ввести нові", "інші контакт")
    )


def _is_order_confirm(text: str) -> bool:
    lower = text.lower().strip()
    if _declines_more_items(text) or _is_restating_confirm(text):
        return True
    # «прибери slap» — не оформлення; «більше не треба … замовляю» — уже оброблено вище
    if _wants_remove_from_cart(text):
        return False
    if any(
        n in lower
        for n in ("не треба", "не хочу", "не зараз", "пізніше", "ще подумаю", "не підход")
    ):
        return False
    if _is_profile_reject(text) and "актуальн" not in lower:
        return False
    if lower in {
        "так",
        "да",
        "yes",
        "y",
        "ок",
        "ok",
        "добре",
        "давайте",
        "оформлюємо",
        "оформлюй",
        "замовити",
        "хочу замовити",
        "оформити",
        "оформити замовлення",
        "+",
        "все підходить",
        "все ок",
        "все добре",
    }:
        return True
    if any(
        p in lower
        for p in (
            "все підходить",
            "оформлюємо",
            "оформимо",
            "оформлювати",
            "можемо оформ",
            "давайте оформи",
            "хочу оформити",
            "готовий замовити",
            "беру",
            "замовляю",
        )
    ):
        if _declines_more_items(lower) or _is_restating_confirm(text):
            return True
        if _extract_product_hint(lower) or _extract_qty(lower):
            return False
        return True
    # «так все підходить» / «давайте тоді» / «го можна»
    if re.match(r"^(так|да|yes|давайте|давай|го|угу|ага|ок|ok)\b", lower) and len(lower) <= 80:
        if _extract_product_hint(lower) or _extract_qty(lower):
            return False
        if any(w in lower for w in ("вище", "писав", "силікон", "світловідб", "браслет", "штук")):
            return False
        return True
    if any(
        p in lower
        for p in (
            "давайте тоді",
            "давай тоді",
            "тоді давайте",
            "тоді давай",
            "оформляйте",
            "оформляєм",
            "згоден",
            "згодна",
            "домовились",
            "хай буде",
            "пішло",
            "можна оформ",
            "тоді можна",
            "ну можна",
            "все ок",
            "все нормально",
        )
    ):
        return True
    return False


def _strip_echo(reply: str, user_text: str) -> str:
    """Прибрати повторення відповіді клієнта з початку відповіді бота."""
    if not reply or not user_text:
        return reply
    r = reply.strip()
    u = user_text.strip()
    patterns = [
        f'Ви написали: "{u}"',
        f"Ви написали: «{u}»",
        f'Ви сказали: "{u}"',
        f"Ви сказали: «{u}»",
        f'"{u}"',
        f"«{u}»",
        u,
    ]
    for p in patterns:
        if r.lower().startswith(p.lower()):
            r = r[len(p):].lstrip(" \n.,:;!-—")
            break
    lines = r.splitlines()
    if lines and lines[0].strip().lower() == u.lower():
        r = "\n".join(lines[1:]).strip()
    return r or reply


def _is_explicit_order_start(text: str) -> bool:
    lower = text.lower().strip()
    if _wants_consult_not_order(text):
        return False
    exact = {
        "замовити",
        "хочу замовити",
        "оформити замовлення",
        "оформлюємо",
        "оформлюємо замовлення",
        "готовий замовити",
        "оформлюйте",
    }
    if lower in exact:
        return True
    return any(
        p in lower
        for p in (
            "оформити замовлення",
            "хочу замовити",
            "хочу оформити замовлення",
            "давайте оформимо",
            "оформлюємо замовлення",
        )
    )


def _parse_person_name(text: str) -> tuple[str | None, str | None]:
    """Не змінюємо написання клієнта."""
    return None, None


def _extract_warehouse_number(text: str) -> str:
    """Лише номер. Місто без цифр («Вінниця») — не відділення."""
    m = re.search(r"(\d+)\s*(?:відділен|отделен|post|п)?", (text or "").lower())
    if m:
        return m.group(1)
    digits = re.findall(r"\d+", text or "")
    return digits[0] if digits else ""


def _extract_city(text: str) -> str | None:
    lower = (text or "").lower()
    cities = {
        "київ": "Київ",
        "киев": "Київ",
        "львів": "Львів",
        "львов": "Львів",
        "харків": "Харків",
        "харьков": "Харків",
        "одеса": "Одеса",
        "одесса": "Одеса",
        "дніпро": "Дніпро",
        "днепр": "Дніпро",
        "запоріжжя": "Запоріжжя",
        "вінниця": "Вінниця",
        "полтава": "Полтава",
        "чернігів": "Чернігів",
        "черкаси": "Черкаси",
        "суми": "Суми",
        "миколаїв": "Миколаїв",
        "хмельницький": "Хмельницький",
        "рівне": "Рівне",
        "івано-франківськ": "Івано-Франківськ",
        "тернопіль": "Тернопіль",
        "луцьк": "Луцьк",
        "ужгород": "Ужгород",
        "кривий ріг": "Кривий Ріг",
    }
    for key, name in cities.items():
        if key in lower:
            return name
    m = re.search(r"(?:місто|город)\s+([А-Яа-яA-Za-zІіЇїЄєҐґ\-]+)", text or "", re.I)
    if m:
        return m.group(1).strip().capitalize()
    return None


def _extract_total_uah(text: str) -> float | None:
    """Витягнути «разом 36 000 грн» / «17000 грн»."""
    lower = (text or "").lower().replace("\u00a0", " ")
    patterns = (
        r"(?:разом|загалом|сума|всього)\s*[:\-]?\s*([\d\s]+(?:[.,]\d+)?)\s*грн",
        r"([\d\s]+(?:[.,]\d+)?)\s*грн\s*(?:разом|загалом)",
    )
    for p in patterns:
        m = re.search(p, lower)
        if m:
            raw = re.sub(r"\s+", "", m.group(1)).replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def _is_flow_aside(text: str) -> bool:
    """Коментар/уточнення серед сценарію, не відповідь на поле."""
    lower = text.lower().strip()
    markers = (
        "ви вже",
        "ти вже",
        "вже прорах",
        "вже порахув",
        "вище сум",
        "я вже",
        "все надав",
        "вже все",
        "вже дав",
        "зачекай",
        "почекай",
        "навіщо ще",
        "нащо ще",
        "я ж написав",
        "я ж сказав",
        "ввів вище",
        "я ввів",
        "я вже ввів",
        "я ж пиш",
        "ті дан",
        "актуальн",
        "ви не розумі",
    )
    return any(m in lower for m in markers)


def _has_saved_delivery(data: dict[str, Any]) -> bool:
    city = data.get("city")
    if data.get("saved_street_address") and city:
        return True
    if data.get("saved_warehouse") and city:
        return True
    return False


def _apply_crm_answer(data: dict[str, Any], text: str) -> None:
    """Спочатку key:value, інакше — відповідь на наступне порожнє поле (як написав клієнт)."""
    parsed = _parse_kv(text)
    data.update({k: v for k, v in parsed.items() if v})
    if parsed.get("first_name") and parsed.get("last_name"):
        pass
    elif parsed.get("first_name") and not data.get("last_name"):
        data["last_name"] = parsed["first_name"]
    _merge_order_hints(data, text)
    missing = _missing_crm_fields(data)
    if not missing:
        return
    if parsed and "person_name" not in missing:
        return
    if not text.strip():
        return

    key = missing[0]
    val = text.strip()

    if key == "email" or val.lower() in (
        "не потрібен",
        "не потрібний",
        "не треба",
        "немає",
        "нет",
        "no",
        "-",
        "пропустити",
    ):
        if key == "email" or "email" in missing:
            data["email"] = ""
            if key == "email":
                return

    if key == "product_type" and _looks_like_question(val):
        data["_abort_to_consult"] = val
        return

    if key == "person_name":
        low = val.lower()
        if (
            _is_profile_ok(val)
            or "актуальн" in low
            or "ви не розумі" in low
            or _looks_like_question(val)
        ):
            data["_restore_profile"] = True
            return
        fn, ln = _split_person_name(val)
        if fn:
            data["first_name"] = fn
            data["last_name"] = ln or fn
        return

    if key == "quantity":
        q = _extract_qty(val)
        if not q:
            digits = re.search(r"\b(\d{1,6})\b", val)
            if digits and not _looks_like_question(val) and len(val.strip()) < 40:
                q = digits.group(1)
        if q:
            data["quantity"] = q
        return

    if key == "product_type":
        hint = _extract_product_hint(val)
        data["product_type"] = hint or val
        return

    # зберігаємо РІВНО як написав клієнт
    data[key] = val


def _delivery_fields_needed(delivery_type: str) -> list[str]:
    base = ["recipient_last_name", "recipient_first_name", "recipient_phone", "city"]
    if delivery_type == DELIVERY_TYPE_ADDRESS:
        return base + ["recipient_patronymic", "street_address"]
    return base + ["warehouse"]


def _delivery_value(data: dict[str, Any], delivery: dict[str, Any], key: str) -> str | None:
    if delivery.get(key):
        return delivery.get(key)
    aliases = {
        "recipient_last_name": "last_name",
        "recipient_first_name": "first_name",
        "recipient_phone": "phone",
        "city": "city",
    }
    alias = aliases.get(key)
    if alias and data.get(alias):
        return data.get(alias)
    return None


def _next_delivery_question(data: dict[str, Any], delivery: dict[str, Any], delivery_type: str) -> str | None:
    needed = _delivery_fields_needed(delivery_type)
    questions = dict(DELIVERY_FIELD_ORDER)
    for key in needed:
        val = _delivery_value(data, delivery, key)
        if val:
            delivery[key] = val
            continue
        return questions.get(key, f"Уточніть: {key}")
    return None


def _apply_delivery_answer(delivery: dict[str, Any], data: dict[str, Any], delivery_type: str, text: str) -> None:
    parsed = _parse_kv(text)
    if parsed.get("last_name"):
        delivery["recipient_last_name"] = parsed["last_name"]
    if parsed.get("first_name"):
        delivery["recipient_first_name"] = parsed["first_name"]
    if parsed.get("patronymic"):
        delivery["recipient_patronymic"] = parsed["patronymic"]
    if parsed.get("phone"):
        delivery["recipient_phone"] = parsed["phone"]
    if parsed.get("city"):
        delivery["city"] = _normalize_words(parsed["city"])
        data["city"] = delivery["city"]
    if parsed.get("warehouse"):
        delivery["warehouse"] = parsed["warehouse"]
    if parsed.get("street_address"):
        delivery["street_address"] = parsed["street_address"]

    # з вільного тексту: «45 відділення Київ»
    city_hint = _extract_city(text)
    if city_hint:
        delivery["city"] = _normalize_words(city_hint)
        data["city"] = delivery["city"]
    if re.search(r"\d+", text) and (
        "відділен" in text.lower()
        or "поштомат" in text.lower()
        or delivery_type in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT)
    ):
        wh = _extract_warehouse_number(text)
        if wh and wh.isdigit():
            delivery["warehouse"] = wh

    # підтягнути вже відомі CRM-поля
    for key in _delivery_fields_needed(delivery_type):
        val = _delivery_value(data, delivery, key)
        if val:
            delivery[key] = val

    if parsed:
        return

    for key in _delivery_fields_needed(delivery_type):
        if delivery.get(key):
            continue
        raw = text.strip()
        if key == "warehouse":
            wh = _extract_warehouse_number(raw)
            if not wh:
                continue
            delivery[key] = wh
        elif key == "city":
            city = _extract_city(raw)
            if not city and _extract_warehouse_number(raw) and not re.search(r"[а-яіїєґa-z]{3,}", raw.lower()):
                continue
            delivery[key] = _normalize_words(city or raw)
            data["city"] = delivery[key]
        else:
            delivery[key] = raw
        break


async def process_message(db: AsyncSession, chat_id: str, text: str) -> ChatMessageResponse:
    text, screenshot_path = _strip_screenshot_marker(text.strip())
    lower = text.lower()
    state = await _get_state(db, chat_id)
    data = _load_data(state)
    # запам'ятовуємо товар/кількість з будь-якого повідомлення (як написав клієнт)
    _merge_order_hints(data, text)
    _save_data(state, data)
    await db.commit()

    leftover_after_invoice = bool(
        (data.get("invoice_pdf_path") or data.get("invoice_number"))
        and state.step in ("idle", None, "")
        and not _says_already_paid(text)
        and not _prefers_consult(text)
        and not _is_order_confirm(text)
        and (
            _says_already_provided(text)
            or _extract_city(text)
            or (
                len(text.split()) <= 4
                and any(w in lower for w in ("київ", "місто", "вул", "адреса", "тов", "фоп"))
            )
        )
    )
    if leftover_after_invoice:
        return ChatMessageResponse(
            response=(
                "Рахунок уже надіслав файлом. Як оплатите за реквізитами — "
                "киньте скрін квитанції сюди."
            ),
            action="invoice_created",
        )

    if screenshot_path and state.step in (
        "collect_invoice",
        "confirm_invoice_data",
        "collect_payment",
    ):
        file_text = _text_from_requisites_file(screenshot_path)
        merged = text
        if file_text and file_text not in (text or ""):
            merged = f"{text}\n{file_text}".strip()
        merged_l = merged.lower()
        looks_like_requisites = state.step in (
            "collect_invoice",
            "confirm_invoice_data",
        ) or any(
            w in merged_l for w in ("реквізит", "єдрпоу", "едрпоу", "фоп", "тов", "іпн", "ипн")
        )
        if looks_like_requisites:
            bare = (text or "").strip().lower() in {"", "[скрін]", "скрін", "скрин", "[скрин]"}
            if bare and not file_text:
                state.step = "collect_invoice"
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=(
                        "Отримав файл. З фото реквізити не прочитались. "
                        "Напишіть їх текстом: назва ФОП/ТОВ, ЄДРПОУ, ІПН (або «немає») і адреса. "
                        "Або надішліть PDF, де цей текст можна скопіювати. "
                        "Перед рахунком звірю дані і запитаю, чи все вірно."
                    ),
                    action="collect_invoice_data",
                )
            if state.step == "collect_payment":
                state.step = "collect_invoice"
            return await _step_collect_invoice(db, state, data, merged)

    if screenshot_path:
        if _screenshot_is_payment(text, data):
            return await _reply_payment_status(
                db,
                data,
                chat_id=chat_id,
                user_text=text,
                screenshot_path=screenshot_path,
            )
        # Скрін не про оплату (макет на погодження, правки, випадкове фото) —
        # не вигадувати «оплату за рахунком», а передати менеджеру на перегляд.
        reply = (
            "Дякую, отримав скрін. Передаю менеджеру — він перегляне і напише вам у цей чат. "
            "Якщо це правки/погодження макету — вкажіть, що саме потрібно змінити."
        )
        _save_data(state, data)
        await db.commit()
        return await _soft_manager_handoff(
            db, state.chat_id, text, reply, data, extra={"screenshot": screenshot_path}
        )

    if _says_already_paid(text):
        return await _reply_payment_status(
            db,
            data,
            chat_id=chat_id,
            user_text=text,
            screenshot_path=screenshot_path,
        )

    if _says_pay_link_broken(text):
        return await _reply_payment_link_issue(db, state, data)

    if _asks_after_pay_contact(text):
        return ChatMessageResponse(
            response=(
                "Так, я тут у чаті. Після оплати сам побачу надходження і напишу вам. "
                "Якщо треба — підключу менеджера."
            ),
            action="consult",
        )

    if _asks_if_bot(text):
        return ChatMessageResponse(response=REAL_MANAGER_REPLY, action="consult")

    if lower in ("/start", "старт"):
        state.step = "idle"
        _save_data(state, {})
        await db.commit()
        return ChatMessageResponse(response=_current_greeting(), action="greeting")

    if lower in ("привіт", "вітаю", "hello", "hi"):
        has_context = bool(
            data.get("product_type")
            or data.get("quantity")
            or data.get("cart")
            or (state.step and state.step != "idle")
        )
        if has_context:
            return ChatMessageResponse(
                response="Привіт. Продовжуємо з того, на чому зупинились — що потрібно?",
                action="consult",
            )
        return ChatMessageResponse(response=_current_greeting(), action="greeting")

    if lower in ("скасувати", "відміна", "/cancel", "скасувати замовлення"):
        state.step = "idle"
        _save_data(state, {})
        await db.commit()
        return ChatMessageResponse(
            response="Добре, оформлення скасовано. Якщо треба — порахуємо інше.",
            action="cancelled",
        )

    # --- «Дякую» → чекаємо макет (на всі категорії, не лише резинки) ---
    if _is_pure_thanks(text) and state.step not in (
        "collect_crm",
        "collect_delivery_type",
        "collect_delivery",
        "collect_payment",
        "collect_invoice",
        "confirm_invoice_data",
        "lookup_edrpou",
    ):
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=THANKS_VECTOR_REPLY, action="consult")

    # --- Запит менеджера (не плутати з «ти бот / ти людина?») ---
    if not _asks_if_bot(text) and any(
        w in lower
        for w in (
            "менеджер",
            "зв'язати",
            "звязати",
            "живий консультант",
            "оператор",
            "людина",
            "call me",
            "переключ",
            "передайте дмит",
            "передай дмит",
            "передайте йому",
        )
    ):
        ticket = await chat_history.create_manager_ticket(
            db, state.chat_id, text, mute_bot=True
        )
        return ChatMessageResponse(
            response=MANAGER_WAIT_REPLY,
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )

    # Клієнт хоче повернутись до підбору/прорахунку — виходимо зі сценарію
    scenario_steps = {
        "collect_crm",
        "collect_delivery_type",
        "collect_delivery",
        "collect_payment",
        "collect_invoice",
        "confirm_invoice_data",
        "confirm_saved_requisites",
        "lookup_edrpou",
    }

    # Глобальні відповіді на оплату / терміновість — до воронки і прорахунків
    if any(w in lower for w in ("наклад", "налож", "післяплат")) and (
        "пошт" in lower or "оплат" in lower or "можна" in lower or "чи " in lower or "?" in text
    ):
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                "Накладеним платежем на Новій пошті чи Укрпошті не відправляємо. "
                "Друковану продукцію запускаємо лише після повної оплати та погодження макету до друку. "
                "Можна карткою онлайн, рахунком для ФОП/ТОВ або готівкою в офісі."
            ),
            action="consult",
        )
    if any(w in lower for w in ("готівк", "кеш")) and any(
        w in lower for w in ("можна", "оплат", "чи ", "як ")
    ):
        url = (calculator.templates or {}).get("contacts_url") or "https://silicon.com.ua/kontakti"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                "Оплата готівкою можлива.\n"
                "Для запуску вашої роботи в друк вам потрібно приїхати до нас у офіс "
                "та оплатити замовлення готівкою.\n"
                "Передплата 100%.\n"
                f"Адреса та години роботи у розділі Контакти: {url}"
            ),
            action="consult",
        )
    if any(w in lower for w in ("швидш", "термінов")) and (
        "?" in text or "можна" in lower or "чи " in lower or "як " in lower
    ):
        reply = (
            "Термінове виготовлення залежить від завантаження виробництва. "
            "Вартість термінової роботи — ×2 до прайсу. "
            "Передам менеджеру уточнити можливість. "
            "Бажано вказати кінцеву дату для відправки перевізником або самовивозу."
        )
        _save_data(state, data)
        await db.commit()
        return await _soft_manager_handoff(
            db, state.chat_id, text, reply, data, extra={"urgent": True}
        )

    # Кріплення шнурка (1 чи 2 карабіни) після вже названих ширини і тиражу
    if state.step in ("idle", "await_order_confirm", None, "") or not state.step:
        fastened = _lanyard_fastening_calc(text, data)
        if fastened and fastened.get("success"):
            _apply_quote_to_cart(data, fastened, text)
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_format_quote_reply(fastened, data),
                action="await_order_confirm",
                data=fastened,
            )

    # Каталог сайту (карабіни / килимки / резинки / голограми) — раніше за воронку і прайс
    if state.step in ("idle", "await_order_confirm", None, "") or not state.step:
        if _catalog_guide_reply(text, data) and not _wants_invoice_requisites(text):
            return await _consult_now(db, state, data, text, action="consult")
        variant_early = _try_silicone_variant_order(data, text)
        if variant_early:
            msg, calc = variant_early
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=msg, action="await_order_confirm", data=calc
            )

    if (
        (state.step in ("idle", "await_order_confirm") or not state.step)
        and _prefers_consult(text)
        and not _extract_qty(text)
        and not _wants_invoice_requisites(text)
    ):
        return await _consult_now(db, state, data, text, action="consult")

    if state.step in scenario_steps and _wants_consult_not_order(text):
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        result = await consultant.consult(db, text, chat_id)
        if result.get("escalate_to_manager"):
            ticket = await chat_history.create_manager_ticket(
                db, chat_id, text, mute_bot=False
            )
            return ChatMessageResponse(
                response=_finalize_bot_reply(
                    result.get("response") or MANAGER_WAIT_REPLY
                ),
                action="manager_handoff",
                data={"ticket_id": ticket.id},
            )
        return ChatMessageResponse(
            response=_finalize_bot_reply(result["response"]), action="consult"
        )

    # --- Active scenario steps ---
    if state.step == "confirm_crm":
        if _is_profile_ok(text) or _is_order_confirm(text) or lower in (
            "актуально", "ті самі", "ті ж", "так само"
        ):
            restored = await _try_restore_saved_profile(db, state, data)
            if restored:
                return restored
            profile = data.pop("_profile", {}) or {}
            _apply_profile_to_data(data, profile)
            state.step = "collect_crm"
            _save_data(state, data)
            await db.commit()
            missing = _missing_crm_fields(data)
            if missing:
                return ChatMessageResponse(
                    response=_next_crm_question(data),
                    action="collect_order_data",
                )
            return await _step_collect_crm(db, state, data, text=".")
        if _is_profile_reject(text):
            data.pop("_profile", None)
            for k in ("last_name", "first_name", "phone", "city", "email"):
                data.pop(k, None)
            for k in (
                "saved_delivery_type",
                "saved_warehouse",
                "saved_street_address",
                "saved_patronymic",
            ):
                data.pop(k, None)
            state.step = "collect_crm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response="Добре, введемо нові. Напишіть ім'я та прізвище одним повідомленням.",
                action="collect_order_data",
            )
        if _is_interrupt_question(text, "confirm"):
            return await _answer_interrupt(
                db,
                state,
                data,
                text,
                "Ці контакти залишаємо, чи краще введемо нові?",
                "confirm_client_data",
                field="confirm",
            )
        return ChatMessageResponse(
            response="Ці контакти залишаємо, чи краще введемо нові?",
            action="confirm_client_data",
        )

    if state.step == "await_order_confirm":
        invoice_now = await _maybe_handle_edrpou_invoice(db, state, data, text, lower)
        if invoice_now:
            return invoice_now

        # Presence («???», «ви тут?») — до питань, щоб не йти в consult-fallback
        if _is_presence_nudge(text):
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_presence_reply(data), action="await_order_confirm"
            )

        # Прибрати позицію зі списку — раніше за «оформлюємо» / restating_same
        if _wants_remove_from_cart(text) and (data.get("cart") or data.get("product_type")):
            removed, kept = _remove_from_cart(data, text)
            reply = _format_cart_after_remove(removed, kept)
            if kept:
                state.step = "await_order_confirm"
                action = "await_order_confirm"
            else:
                state.step = "idle"
                action = "consult"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=reply, action=action)

        if data.get("pending_width_confirm"):
            wm = re.search(r"\b(5|12|20)\s*мм", lower)
            if wm and not _wants_remove_from_cart(text) and len(text) < 80:
                width = wm.group(1)
                rebuilt: list[dict[str, Any]] = []
                for row in list(data.get("cart") or []):
                    if _product_family(str(row.get("product_name") or "")) != "silicone":
                        rebuilt.append(row)
                        continue
                    name = str(row.get("product_name") or "")
                    new_name = re.sub(r"×\d+\s*мм", f"×{width} мм", name)
                    line = {
                        "product_type": "силіконовий",
                        "quantity": int(row.get("quantity") or 0),
                        "size_mm": width,
                        "product_name": new_name,
                        "text": f"силіконовий браслет {width} мм {row.get('quantity')} шт 1 колір",
                    }
                    rebuilt_tmp: dict[str, Any] = {}
                    last_one = _build_cart_from_lines(rebuilt_tmp, [line])
                    if last_one and rebuilt_tmp.get("cart"):
                        rebuilt.extend(rebuilt_tmp["cart"])
                    else:
                        rebuilt.append(row)
                data["cart"] = rebuilt
                data["size_mm"] = width
                data.pop("pending_width_confirm", None)
                _sync_cart_fields(data)
                state.step = "await_order_confirm"
                _save_data(state, data)
                await db.commit()
                msg = _format_silicone_variant_message(
                    [r for r in rebuilt if _product_family(str(r.get("product_name") or "")) == "silicone"],
                    width_specified=True,
                    cart=rebuilt,
                )
                return ChatMessageResponse(
                    response=msg, action="await_order_confirm", data={"size_mm": width}
                )

        variant = _try_silicone_variant_order(data, text)
        if variant:
            msg, calc = variant
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=msg, action="await_order_confirm", data=calc
            )

        # 1) Питання / виправлення — ЗАВЖДИ раніше за «оформлюємо» і перерахунок тиражу
        if _has_client_question(text, data) and not _is_order_confirm(text):
            if any(w in lower for w in ("швидш", "термінов")):
                reply = (
                    "Термінове виготовлення залежить від завантаження виробництва. "
                    "Вартість термінової роботи — ×2 до прайсу. "
                    "Передам менеджеру уточнити можливість. "
                    "Бажано вказати кінцеву дату для відправки перевізником або самовивозу."
                )
                _save_data(state, data)
                await db.commit()
                return await _soft_manager_handoff(
                    db, state.chat_id, text, reply, data, extra={"urgent": True}
                )
            if any(w in lower for w in ("наклад", "налож", "післяплат")):
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=(
                        "Накладеним платежем на Новій пошті чи Укрпошті не відправляємо. "
                        "Друковану продукцію запускаємо лише після повної оплати та погодження макету до друку. "
                        "Можна карткою онлайн, рахунком для ФОП/ТОВ або готівкою в офісі."
                    ),
                    action="await_order_confirm",
                )
            if any(w in lower for w in ("готівк", "кеш", "готівкою")):
                url = (calculator.templates or {}).get("contacts_url") or "https://silicon.com.ua/kontakti"
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=(
                        "Оплата готівкою можлива.\n"
                        "Для запуску вашої роботи в друк вам потрібно приїхати до нас у офіс "
                        "та оплатити замовлення готівкою.\n"
                        "Передплата 100%.\n"
                        f"Адреса та години роботи у розділі Контакти: {url}"
                    ),
                    action="await_order_confirm",
                )

            # виправлення ширини / прорахунку / «я вище писав інший товар»
            if _is_quote_correction(text) or (
                _extract_product_hint(text) and re.search(r"\d+\s*мм", lower)
            ):
                size_m = re.search(r"(\d+)\s*мм", lower)
                if size_m:
                    data["size_mm"] = size_m.group(1)
                hint = _extract_product_hint(text) or data.get("product_type")
                if hint:
                    # заміна товару («вище писав силікон») — чистити кошик;
                    # виправлення ширини шнурка — лишати інші позиції
                    replace_cart = False
                    if _is_quote_correction(text) and _extract_product_hint(text):
                        hint_fam = _product_family(str(hint))
                        cart_fams = {
                            _product_family(str(r.get("product_name") or ""))
                            for r in (data.get("cart") or [])
                        }
                        # нового сімейства в кошику немає → клієнт міняє товар
                        replace_cart = hint_fam not in cart_fams and hint_fam not in (
                            "",
                            "other",
                        )
                    _apply_product_hint(data, hint, clear_cart=replace_cart)
                qty = str(data.get("quantity") or "").strip()
                # якщо в повідомленні є тираж — брати його; інакше з кошика / стану
                q_msg = _extract_qty(text)
                if q_msg:
                    qty = q_msg
                elif data.get("cart"):
                    # оновлюємо останню позицію того ж сімейства
                    for row in reversed(list(data.get("cart") or [])):
                        if _product_family(str(row.get("product_name") or "")) == _product_family(
                            str(hint or "")
                        ):
                            qty = str(row.get("quantity") or qty)
                            break
                if qty and hint:
                    calc = _quote_calculate(
                        _product_for_calc(text, data, hint),
                        int(re.sub(r"\D", "", qty) or "0") or 0,
                        size_mm=str(data.get("size_mm")) if data.get("size_mm") else None,
                        text=text,
                    )
                    if calc.get("success") and int(re.sub(r"\D", "", qty) or "0"):
                        _apply_quote_to_cart(data, calc, f"виправ {text}")
                        state.step = "await_order_confirm"
                        _save_data(state, data)
                        await db.commit()
                        return ChatMessageResponse(
                            response=_format_quote_reply(calc, data),
                            action="await_order_confirm",
                            data=calc,
                        )

            # питання + тираж в одному повідомленні («200 шт які кольори фіксаторів?»)
            qty_part, q_part = _split_qty_and_question(text)
            follow = _product_followup_reply(q_part or text, data)
            if not follow:
                follow = _product_followup_reply(text, data)
            answer = follow
            if not answer:
                # для вартості/прорахунку — повний текст (розміри 350/450 мл не губити)
                consult_q = text if _wants_price_or_quote(text) else (q_part or text)
                result = await consultant.consult(db, consult_q, chat_id)
                answer = _strip_echo(result.get("response") or "", text)
                if answer == AIConsultant.FALLBACK_RESPONSE:
                    answer = _contextual_fallback(q_part or text, data)
                if result.get("escalate_to_manager"):
                    ticket = await chat_history.create_manager_ticket(
                        db, state.chat_id, text, mute_bot=False
                    )
                    _save_data(state, data)
                    await db.commit()
                    return ChatMessageResponse(
                        response=_finalize_bot_reply(answer or MANAGER_WAIT_REPLY),
                        action="manager_handoff",
                        data={"ticket_id": ticket.id},
                    )
            # якщо є тираж — після відповіді ще порахувати
            if qty_part and (data.get("product_type") or _extract_product_hint(text)):
                prod = _product_for_calc(text, data)
                calc = _quote_calculate(
                    prod,
                    int(qty_part),
                    size_mm=str(data.get("size_mm")) if data.get("size_mm") else None,
                    text=text,
                )
                if calc.get("success"):
                    _apply_quote_to_cart(data, calc, text)
                    quote_txt = _format_quote_reply(calc, data)
                    # «яка вартість … 100 шт» — достатньо прорахунку, без дубля
                    side_only = any(
                        w in lower
                        for w in (
                            "фіксатор", "колір фікс", "макет", "оплат", "достав",
                            "аксесуар", "карабін", "фастекс", "матеріал",
                        )
                    )
                    if _wants_price_or_quote(text) and not side_only:
                        reply = quote_txt
                    else:
                        reply = f"{answer.rstrip()}\n\n{quote_txt}"
                    state.step = "await_order_confirm"
                    _save_data(state, data)
                    await db.commit()
                    return ChatMessageResponse(
                        response=reply, action="await_order_confirm", data=calc
                    )
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=answer, action="await_order_confirm")

        if (
            _is_order_confirm(text)
            or _is_explicit_order_start(text)
            or _declines_more_items(text)
            or _is_restating_confirm(text)
        ):
            return await _begin_order_collection(db, state, data, chat_id)
        if _is_presence_nudge(text):
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=_presence_reply(data), action="await_order_confirm")
        follow = _product_followup_reply(text, data)
        if follow and not _ai_live() and not _wants_add_to_cart(text) and not _extract_qty(text):
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=follow, action="await_order_confirm")
        new_product = _extract_product_hint(text)
        new_qty = _extract_qty(text) or _extract_alt_qty(text)
        if _is_offtopic_question(text, data) and not new_qty and not _wants_add_to_cart(text):
            return await _consult_now(
                db, state, data, text, action="await_order_confirm"
            )
        if (
            _wants_stored_quote(text)
            and not new_qty
            and not _wants_add_to_cart(text)
            and (data.get("quantity") or data.get("product_type") or data.get("pending_product_type"))
        ):
            qty = str(data.get("quantity") or "").strip()
            if qty and (data.get("product_type") or data.get("pending_product_type")):
                state.step = "idle"
                _save_data(state, data)
                await db.commit()
                return await process_message(db, chat_id, f"{qty} штук")
            prod = _product_for_calc(text, data)
            start = calculator.starting_quote(prod, data.get("size_mm"), text=text)
            if start:
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=f"{start} Напишіть тираж — порахую суму.",
                    action="await_order_confirm",
                )
        if _is_side_question(text) and not new_qty and not _wants_add_to_cart(text):
            if any(w in lower for w in ("швидш", "термінов")):
                reply = (
                    "Термінове виготовлення залежить від завантаження виробництва. "
                    "Вартість термінової роботи — ×2 до прайсу. "
                    "Передам менеджеру уточнити можливість. "
                    "Бажано вказати кінцеву дату для відправки перевізником або самовивозу."
                )
                _save_data(state, data)
                await db.commit()
                return await _soft_manager_handoff(
                    db, state.chat_id, text, reply, data, extra={"urgent": True}
                )
            return await _consult_now(
                db,
                state,
                data,
                text,
                action="await_order_confirm",
                append="Якщо прорахунок підходить — оформлюємо?",
            )
        if _wants_add_to_cart(text) and not new_qty:
            hint = new_product or data.get("pending_product_type")
            if hint:
                data["pending_product_type"] = hint
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=f"Яка кількість для «{hint}»?",
                    action="await_order_confirm",
                )
            return ChatMessageResponse(
                response="Який товар додати в це ж замовлення і скільки штук?",
                action="await_order_confirm",
            )
        # новий товар/тираж замість «так» — перераховуємо, не зациклюємось на «Оформлюємо?»
        # виправлення ширини/позиції — НЕ оформлення
        restating_same = bool(
            new_product
            and _same_product_as_quote(text, data)
            and not new_qty
            and not _is_quote_correction(text)
            and not _wants_remove_from_cart(text)
            and not re.search(r"\d+\s*мм", lower)
            and not _has_client_question(text, data)
        )
        if restating_same:
            return await _begin_order_collection(db, state, data, chat_id)
        if (
            (new_product and not restating_same)
            or new_qty
            or _wants_consult_not_order(text)
            or (_is_profile_reject(text) and not _is_order_confirm(text))
            or any(w in lower for w in ("пізніше", "ще подумаю"))
        ):
            # якщо це питання з тиражем — уже обробили вище; не віддавати лише тираж у idle
            if (
                _has_client_question(text, data)
                and not _is_order_confirm(text)
                and not _is_quote_correction(text)
            ):
                return await _consult_now(
                    db, state, data, text, action="await_order_confirm"
                )
            state.step = "idle"
            _save_data(state, data)
            await db.commit()
            if any(w in lower for w in ("пізніше", "ще подумаю")) and not (
                new_product or new_qty
            ):
                return ChatMessageResponse(
                    response="Добре. Якщо треба — порахуємо іншу позицію.",
                    action="consult",
                )
            if new_qty and not new_product and not _wants_add_to_cart(text):
                return await process_message(db, chat_id, f"{new_qty} штук")
            return await process_message(db, chat_id, text)
        if _looks_like_question(text) or _is_side_question(text) or _asks_where_or_what(text):
            append = None
            if not _is_offtopic_question(text, data):
                append = "Якщо все підходить — оформлюємо?"
            return await _consult_now(
                db, state, data, text, action="await_order_confirm", append=append
            )
        # будь-яке живе формулювання — не вимагати «так», відповісти і м'яко продовжити
        if _wants_stored_quote(text) and data.get("quantity") and data.get("product_type"):
            state.step = "idle"
            _save_data(state, data)
            await db.commit()
            return await process_message(db, chat_id, f"{data['quantity']} штук")
        return await _consult_now(
            db,
            state,
            data,
            text,
            action="await_order_confirm",
            append="Якщо все підходить — оформлюємо?",
        )

    if state.step == "collect_crm":
        return await _step_collect_crm(db, state, data, text)
    if state.step == "collect_delivery_type":
        return await _step_delivery_type(db, state, data, text)
    if state.step == "collect_delivery":
        return await _step_collect_delivery(db, state, data, text)
    if state.step == "collect_payment":
        return await _step_collect_payment(db, state, data, text)
    if state.step == "collect_invoice":
        return await _step_collect_invoice(db, state, data, text)
    if state.step == "confirm_invoice_data":
        return await _step_confirm_invoice_data(db, state, data, text)
    if state.step == "confirm_saved_requisites":
        return await _step_confirm_saved_requisites(db, state, data, text)
    if state.step == "lookup_edrpou":
        return await _step_lookup_edrpou(db, state, data, text)

    # --- Intent: start order (лише явний старт, не «прорахувати та замовити») ---
    if _is_explicit_order_start(text) or (
        data.get("quote_ready") and _is_order_confirm(text)
    ):
        return await _begin_order_collection(db, state, data, chat_id)

    if any(
        w in lower
        for w in ("де мій рахунок", "де рахунок", "надішли рахунок", "рахунок pdf", "де pdf")
    ) or lower.strip() in ("рахунок", "pdf", "пдф"):
        if lower.strip() == "рахунок" and _saved_invoice_requisites(data):
            return await _offer_or_start_invoice(db, state, data, text)
        if data.get("invoice_pdf_path"):
            return await _issue_invoice_now(db, state, data)
        if _invoice_fields_ready(data):
            if data.get("requisites_confirmed"):
                return await _issue_invoice_now(db, state, data)
            return await _ask_requisites_confirm(db, state, data)

    # --- ЄДРПОУ / контрагент 1С / рахунок на реквізити ---
    edrpou_intent = await _maybe_handle_edrpou_invoice(db, state, data, text, lower)
    if edrpou_intent:
        return edrpou_intent

    # --- Прибрати позицію з кошика (idle / після прорахунку) ---
    if _wants_remove_from_cart(text) and (data.get("cart") or data.get("product_type")):
        removed, kept = _remove_from_cart(data, text)
        reply = _format_cart_after_remove(removed, kept)
        if kept:
            state.step = "await_order_confirm"
            action = "await_order_confirm"
        else:
            state.step = "idle"
            action = "consult"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=reply, action=action)

    # --- Питання + тираж в idle: спочатку відповідь, потім прорахунок ---
    if _has_client_question(text, data) and not _is_order_confirm(text):
        if any(w in lower for w in ("термінов", "швидш")) and not _extract_qty(text):
            reply = (
                "Термінове виготовлення залежить від завантаження виробництва. "
                "Вартість термінової роботи — ×2 до прайсу. "
                "Передам менеджеру уточнити можливість. "
                "Бажано вказати кінцеву дату для відправки перевізником або самовивозу."
            )
            _save_data(state, data)
            await db.commit()
            return await _soft_manager_handoff(
                db, state.chat_id, text, reply, data, extra={"urgent": True}
            )
        if _is_quote_correction(text):
            size_m = re.search(r"(\d+)\s*мм", lower)
            if size_m:
                data["size_mm"] = size_m.group(1)
            hint = _extract_product_hint(text) or data.get("product_type") or data.get("pending_product_type")
            qty = _extract_qty(text) or str(data.get("quantity") or "").strip()
            if hint and qty:
                calc = _quote_calculate(
                    _product_for_calc(text, data, str(hint)),
                    int(re.sub(r"\D", "", str(qty)) or "0"),
                    size_mm=str(data.get("size_mm")) if data.get("size_mm") else None,
                    text=text,
                )
                if calc.get("success"):
                    _apply_quote_to_cart(data, calc, f"виправ {text}")
                    state.step = "await_order_confirm"
                    _save_data(state, data)
                    await db.commit()
                    return ChatMessageResponse(
                        response=_format_quote_reply(calc, data),
                        action="await_order_confirm",
                        data=calc,
                    )
        qty_part, q_part = _split_qty_and_question(text)
        guide = _catalog_guide_reply(text, data) or _catalog_guide_reply(q_part or text, data)
        if guide:
            return await _consult_now(db, state, data, text, action="consult")
        follow = _product_followup_reply(q_part or text, data) or _product_followup_reply(text, data)
        if follow or qty_part:
            answer = follow
            if not answer:
                consult_q = text if _wants_price_or_quote(text) else (q_part or text)
                result = await consultant.consult(db, consult_q, chat_id)
                answer = _strip_echo(result.get("response") or "", text)
                if answer == AIConsultant.FALLBACK_RESPONSE:
                    answer = _contextual_fallback(q_part or text, data)
                if result.get("escalate_to_manager"):
                    # прайс уже в answer (напр. чашки 1 колір + note про 4) — не губити
                    if result.get("pricing"):
                        pricing = result["pricing"]
                        if pricing.get("multi_quotes"):
                            for q in pricing["multi_quotes"]:
                                if q.get("success"):
                                    _apply_quote_to_cart(data, q, text)
                        elif pricing.get("success"):
                            _apply_quote_to_cart(data, pricing, text)
                    _save_data(state, data)
                    await db.commit()
                    return await _soft_manager_handoff(
                        db, state.chat_id, text, answer, data
                    )
            if qty_part and (data.get("product_type") or data.get("pending_product_type") or _extract_product_hint(text)):
                prod = _product_for_calc(text, data)
                calc = _quote_calculate(
                    prod,
                    int(qty_part),
                    size_mm=str(data.get("size_mm")) if data.get("size_mm") else None,
                    text=text,
                )
                if calc.get("success"):
                    _apply_quote_to_cart(data, calc, text)
                    state.step = "await_order_confirm"
                    _save_data(state, data)
                    await db.commit()
                    quote_txt = _format_quote_reply(calc, data)
                    side_only = any(
                        w in lower
                        for w in (
                            "фіксатор", "колір фікс", "макет", "оплат", "достав",
                            "аксесуар", "карабін", "фастекс", "матеріал",
                        )
                    )
                    if _wants_price_or_quote(text) and not side_only:
                        reply = quote_txt
                    else:
                        reply = f"{(answer or '').rstrip()}\n\n{quote_txt}".strip()
                    return ChatMessageResponse(
                        response=reply, action="await_order_confirm", data=calc
                    )
            if answer:
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(response=answer, action="consult")

    # --- Кілька розмірів силікону в одному повідомленні (дитячий/підлітковий/дорослий) ---
    variant = _try_silicone_variant_order(data, text)
    if variant:
        msg, calc = variant
        state.step = "await_order_confirm"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=msg, action="await_order_confirm", data=calc
        )

    # --- Кілька товарів в одному повідомленні (силікон + світловідбиваючі тощо) ---
    order_lines = _extract_order_lines(text)
    if len(order_lines) >= 2:
        multi = _build_cart_from_lines(data, order_lines)
        if multi:
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_format_quote_reply(multi, data),
                action="await_order_confirm",
                data=multi,
            )

    # --- Каталог сайту (карабіни / килимки / резинки / голограми) раніше за прайс ---
    if state.step in ("idle", "await_order_confirm", None, "") or not state.step:
        if _catalog_guide_reply(text, data) and not _wants_invoice_requisites(text):
            return await _consult_now(db, state, data, text, action="consult")

    # --- Pricing shortcut ---
    qty_match = re.search(r"(\d+)\s*шт", lower) or (
        re.search(r"\b(\d+)\b", lower) if any(w in lower for w in ["ціна", "коштує", "прорахун", "розрахун", "прайс", "браслет"]) else None
    )
    size_match = re.search(r"(\d+)\s*мм", lower)
    if any(w in lower for w in ["ціна", "коштує", "прорахун", "розрахун", "прайс", "варт"]) and qty_match:
        hint = _extract_product_hint(text)
        product_type = _product_for_calc(text, data, hint)
        if not product_type:
            product_type = ""
        result = _quote_calculate(
            product_type or "невідомо",
            int(qty_match.group(1)),
            size_mm=size_match.group(1) if size_match else None,
            text=text,
        )
        if result.get("needs_manager"):
            note = result.get("manager_note") or result.get("error") or ""
            base = ""
            if result.get("multi_quotes"):
                base = format_quote_message(result, with_confirm=False)
            reply = f"{base}\n\n{note}".strip() if base else (note or MANAGER_WAIT_REPLY)
            # 1-колірні позиції можна додати в кошик, 4-колір — лише після макету
            if result.get("multi_quotes"):
                for q in result["multi_quotes"]:
                    if q.get("success"):
                        _apply_quote_to_cart(data, q, text)
            return await _soft_manager_handoff(
                db, state.chat_id, text, reply, data, extra=result
            )
        if result.get("success"):
            if size_match:
                data["size_mm"] = size_match.group(1)
            elif result.get("product_key"):
                m = re.search(r"(\d+)mm", str(result["product_key"]))
                if m:
                    data["size_mm"] = m.group(1)
            if result.get("multi_quotes"):
                # кілька варіантів друку — показуємо всі, у кошик не пхаємо всі одразу
                state.step = "await_order_confirm"
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response=format_quote_message(
                        result,
                        with_confirm=True,
                        cart=data.get("cart") if len(data.get("cart") or []) > 1 else None,
                    ),
                    action="await_order_confirm",
                    data=result,
                )
            _apply_quote_to_cart(data, result, text)
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            msg = _format_quote_reply(result, data)
            return ChatMessageResponse(response=msg, action="await_order_confirm", data=result)
        # невідомий товар / помилка — не глушити AI, йдемо далі в consult
        if _ai_live() or _catalog_guide_reply(text, data):
            pass
        else:
            return ChatMessageResponse(
                response=result.get("error", "Не вдалося розрахувати.")
            )

    # Не ловити «відділення» / «нова пошта» як старт доставки під час звичайної розмови —
    # лише явні фрази про оформлення доставки.
    if any(
        p in lower
        for p in (
            "оформити доставку",
            "дані для доставки",
            "хочу доставку",
            "створити ттн",
        )
    ):
        state.step = "collect_delivery_type"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=DELIVERY_TYPE_PROMPT,
            action="collect_delivery_data",
        )

    if any(w in lower for w in ["оплатити", "liqpay", "лікпей", "лиг", "ліг", "посилання на оплату"]):
        order_id = data.get("order_id")
        if order_id and data.get("total_amount"):
            pay = await order_service.create_payment(db, int(order_id))
            if pay.get("success") and pay.get("payment_url"):
                return ChatMessageResponse(
                    response=_format_payment_message(data, pay["payment_url"]),
                    action="payment_created",
                    data=pay,
                )
        return ChatMessageResponse(
            response=(
                "Оплата в нас онлайн через LiqPay. "
                "Спершу оформлюємо замовлення — напишіть «замовити», і я все зберу."
            ),
            action="payment_info",
        )

    # «1000 штук» / «силіконові 100 штук» — рахуємо самі (товар з повідомлення важливіший за старий стан)
    qty_only = _extract_qty(text)
    hint_now = _extract_product_hint(text) or data.get("pending_product_type")
    if not hint_now:
        hint_now = data.get("last_topic")
    if isinstance(hint_now, str):
        hint_now = hint_now
    else:
        hint_now = None
    cart_names = " ".join(str(r.get("product_name") or "") for r in (data.get("cart") or [])).lower()
    hint_fam = _product_family(hint_now) if hint_now else ""
    cart_fams = {_product_family(str(r.get("product_name") or "")) for r in (data.get("cart") or [])}
    same_in_cart = bool(hint_now) and (
        (hint_fam not in {"", "other"} and hint_fam in cart_fams)
        or ("силікон" in hint_now.lower() and "силікон" in cart_names)
        or (_is_reflective_text(hint_now) and _is_reflective_text(cart_names))
        or hint_now.lower() in cart_names
    )
    adding_line = bool(data.get("cart")) and bool(hint_now) and (
        _wants_add_to_cart(text) or (bool(data.get("pending_product_type")) and not same_in_cart) or not same_in_cart
    )
    # виправлення («я вище писав силіконові…») — замінити кошик, не додавати
    correcting = hint_now and any(
        w in lower for w in ("вище", "писав", "не те", "неправильно", "помилк", "виправ")
    )
    if hint_now and correcting:
        adding_line = False

    if hint_now and not adding_line:
        _apply_product_hint(data, hint_now, clear_cart=bool(correcting))
        if qty_only:
            data["quantity"] = qty_only
    elif qty_only:
        data["quantity"] = qty_only

    if qty_only and (data.get("product_type") or hint_now or data.get("cart")) and not any(
        w in lower for w in ("ціна", "коштує", "прорахун", "прайс")
    ):
        size_mm = data.get("size_mm")
        size_m = re.search(r"(\d+)\s*мм", lower)
        fam_now = _product_family(text) or _product_family(str(hint_now or ""))
        if size_m and fam_now not in {"pocket", "cardholder"}:
            size_mm = size_m.group(1)
            data["size_mm"] = size_mm

        if hint_now and _is_reflective_text(str(hint_now)):
            product_for_calc = "світловідбиваючий"
            size_mm = None
        elif hint_now and _is_silicone_bracelet_text(str(hint_now)):
            product_for_calc = "силіконовий"
        elif _is_reflective_text(lower) and not hint_now:
            product_for_calc = "світловідбиваючий"
            size_mm = None
        elif _is_reflective_text(str(data.get("product_type") or "")) and not hint_now:
            product_for_calc = "світловідбиваючий"
            size_mm = None
        else:
            product_for_calc = _product_for_calc(text, data, hint_now)
            if _is_silicone_bracelet_text(product_for_calc):
                product_for_calc = "силіконовий"
            elif "силікон" in product_for_calc.lower() and not _is_silicone_bracelet_text(product_for_calc):
                pass

        calc = _quote_calculate(
            product_for_calc,
            int(qty_only),
            size_mm=str(size_mm) if size_mm else None,
            text=text,
            data=data,
        )
        if calc.get("success"):
            data.pop("pending_print_colors", None)
            if calc.get("needs_manager"):
                note = calc.get("manager_note") or calc.get("error") or ""
                base = format_quote_message(calc, with_confirm=False) if calc.get("multi_quotes") else ""
                reply = f"{base}\n\n{note}".strip() if base else note
                if calc.get("multi_quotes"):
                    for q in calc["multi_quotes"]:
                        if q.get("success"):
                            _apply_quote_to_cart(data, q, text)
                data["pending_escalate"] = True
                _save_data(state, data)
                await db.commit()
                return await _soft_manager_handoff(
                    db, state.chat_id, text, reply, data, extra=calc
                )
            cart_text = text if (adding_line or _wants_add_to_cart(text)) else text
            if adding_line and not _wants_add_to_cart(cart_text):
                cart_text = f"ще {text}"
            if calc.get("multi_quotes"):
                # кілька варіантів — не пхати всі в кошик, лише показати
                state.step = "await_order_confirm"
                _save_data(state, data)
                await db.commit()
                msg = format_quote_message(calc, with_confirm=True, cart=data.get("cart") if len(data.get("cart") or []) > 1 else None)
                return ChatMessageResponse(
                    response=msg,
                    action="await_order_confirm",
                    data=calc,
                )
            # виправлення ширини — замінити рядок сімейства, НЕ чистити весь кошик
            _apply_quote_to_cart(data, calc, cart_text)
            data.pop("pending_product_type", None)
            state.step = "await_order_confirm"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_format_quote_reply(calc, data),
                action="await_order_confirm",
                data=calc,
            )
        if calc.get("error"):
            err = calc["error"]
            if "Невідомий тип" in err:
                name = hint_now or data.get("product_type") or "цю позицію"
                err = _individual_quote_reply(str(name), str(qty_only))
            data["individual_quoted"] = True
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=err, action="consult")
        name = hint_now or data.get("product_type") or "цю позицію"
        data["individual_quoted"] = True
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_individual_quote_reply(str(name), str(qty_only)),
            action="consult",
        )

    if _is_presence_nudge(text):
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=_presence_reply(data), action="consult")

    if (
        _wants_stored_quote(text)
        and data.get("quantity")
        and (data.get("product_type") or data.get("pending_product_type") or data.get("last_topic"))
        and not qty_only
        and not _extract_product_hint(text)
    ):
        if data.get("individual_quoted"):
            name = data.get("product_type") or data.get("last_topic") or "цю позицію"
            return ChatMessageResponse(
                response=(
                    f"У прайсі готової ціни за штуку на {name} немає — це індивідуальний прорахунок. "
                    "Можу передати менеджеру, він порахує під ваш тираж."
                ),
                action="consult",
            )
        return await process_message(db, chat_id, f"{data['quantity']} штук")

    # GPT живий — питання/пояснення йдуть у ChatGPT + базу знань, не в шаблони воронки
    if not _ai_live():
        wants_price = any(w in lower for w in ("цін", "варт", "кошту", "по чому", "почому"))
        if wants_price and not qty_only and not consultant._wants_assortment(text):
            if not any(w in lower for w in ("строк", "термін", "скільки днів")):
                prod = _product_for_calc(text, data)
                if prod:
                    start = calculator.starting_quote(prod, data.get("size_mm"), text=text)
                    if start:
                        _save_data(state, data)
                        await db.commit()
                        return ChatMessageResponse(
                            response=f"{start} Напишіть тираж — порахую суму.",
                            action="consult",
                        )

        reco = _event_reco(text)
        if reco:
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=reco, action="consult")

        exist = _existence_reply(text)
        if exist:
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=exist, action="consult")

        follow = _product_followup_reply(text, data)
        if follow:
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=follow, action="consult")

        clarify = _clarify_product_reply(text, data)
        if clarify:
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=clarify, action="consult")

    result = await consultant.consult(db, text, chat_id)
    reply = _strip_echo(result.get("response") or "", text)
    if reply == AIConsultant.FALLBACK_RESPONSE:
        reply = _contextual_fallback(text, data)
    if result.get("escalate_to_manager"):
        ticket = await chat_history.create_manager_ticket(
            db, state.chat_id, text, mute_bot=False
        )
        return ChatMessageResponse(
            response=_finalize_bot_reply(reply or MANAGER_WAIT_REPLY),
            action="manager_handoff",
            data={"ticket_id": ticket.id},
        )

    # огляд асортименту/прайсу — не плутати з конкретним прорахунком
    if result.get("source") == "assortment":
        return ChatMessageResponse(response=reply, action="consult")

    # детермінований / AI прорахунок з прайсу
    pricing = result.get("pricing")
    if pricing and pricing.get("success"):
        if pricing.get("product_key"):
            m = re.search(r"(\d+)mm", str(pricing["product_key"]))
            if m:
                data["size_mm"] = m.group(1)
        _apply_quote_to_cart(data, pricing, text)
        state.step = "await_order_confirm"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_format_quote_reply(pricing, data),
            action="await_order_confirm",
            data=pricing,
        )

    # після конкретного прорахунку в тексті — завжди вмикаємо сценарій оформлення
    lower_reply = reply.lower()
    is_catalog_dump = bool(
        (
            re.search(r"(?m)^\s*1[\.\)]\s+", reply)
            and re.search(r"(?m)^\s*[23][\.\)]\s+", reply)
        )
        or "основний асортимент" in lower_reply
        or ("асортимент" in lower_reply and "орієнтовн" in lower_reply)
    )
    price_hits = len(re.findall(r"\d+[.,]\d+\s*грн|\d+\s*грн", lower_reply))
    # лише реальний прорахунок «На N шт … разом», не орієнтир «від N шт — X грн/шт»
    looks_like_quote = (
        not is_catalog_dump
        and price_hits >= 1
        and price_hits <= 8
        and "грн" in lower_reply
        and (
            "разом" in lower_reply
            or bool(re.search(r"на\s+\d+\s*шт\s*,", lower_reply))
        )
    )
    if looks_like_quote:
        qty = re.search(r"(\d+)\s*шт", lower) or re.search(r"(\d+)\s*шт", lower_reply)
        if qty and not data.get("quantity"):
            data["quantity"] = qty.group(1)
        size_m = re.search(r"(\d+)\s*мм", lower) or re.search(r"(\d+)\s*мм", lower_reply)
        if size_m:
            data["size_mm"] = size_m.group(1)
        extracted = _extract_total_uah(reply)
        if extracted:
            data["total_amount"] = extracted
        # якщо товар відомий — перерахувати і зберегти точну суму з прайсу
        if data.get("product_type") and data.get("quantity"):
            try:
                qn = int(re.sub(r"\D", "", str(data["quantity"])) or "0")
            except ValueError:
                qn = 0
            if qn:
                recalc = _quote_calculate(
                    str(data["product_type"]),
                    qn,
                    size_mm=str(data["size_mm"]) if data.get("size_mm") else None,
                    text=text,
                )
                if recalc.get("success"):
                    _apply_quote_to_cart(data, recalc, text)
                    reply = _format_quote_reply(recalc, data)
        _merge_order_hints(data, text)
        data["quote_ready"] = True
        state.step = "await_order_confirm"
        _save_data(state, data)
        await db.commit()
        if "оформлю" not in reply.lower() and "підходить" not in reply.lower():
            reply = f"{reply.rstrip()}\n\n{ORDER_CONFIRM_PROMPT}"
        return ChatMessageResponse(response=reply, action="await_order_confirm")

    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(response=reply, action="consult")


async def _step_collect_crm(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    missing_now = _missing_crm_fields(data)
    field = missing_now[0] if missing_now else None
    # прибрати позицію під час збору контактів — спочатку змінити кошик
    if _wants_remove_from_cart(text) and (data.get("cart") or data.get("product_type")):
        removed, kept = _remove_from_cart(data, text)
        reply = _format_cart_after_remove(removed, kept)
        if kept:
            state.step = "await_order_confirm"
            # контакти ще не зібрані — після підтвердження знову попросимо
            for k in ("first_name", "last_name", "phone"):
                data.pop(k, None)
            action = "await_order_confirm"
        else:
            state.step = "idle"
            for k in ("first_name", "last_name", "phone"):
                data.pop(k, None)
            action = "consult"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=reply, action=action)
    if (
        _is_profile_ok(text)
        or "актуальн" in (text or "").lower()
        or "ви не розумі" in (text or "").lower()
    ):
        restored = await _try_restore_saved_profile(db, state, data)
        if restored:
            return restored
    if field and _is_interrupt_question(text, field):
        return await _answer_interrupt(
            db,
            state,
            data,
            text,
            _next_crm_question(data) or ORDER_PROMPT,
            "collect_order_data",
            field=field,
        )
    _apply_crm_answer(data, text)
    if data.pop("_restore_profile", None):
        restored = await _try_restore_saved_profile(db, state, data)
        if restored:
            return restored
    if data.pop("_abort_to_consult", None):
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        result = await consultant.consult(db, text, state.chat_id)
        if result.get("escalate_to_manager"):
            ticket = await chat_history.create_manager_ticket(
                db, state.chat_id, text, mute_bot=False
            )
            return ChatMessageResponse(
                response=_finalize_bot_reply(
                    result.get("response") or MANAGER_WAIT_REPLY
                ),
                action="manager_handoff",
                data={"ticket_id": ticket.id},
            )
        return ChatMessageResponse(
            response=_finalize_bot_reply(result["response"]), action="consult"
        )

    missing = _missing_crm_fields(data)
    if missing:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_next_crm_question(data),
            action="collect_order_data",
            data={"missing": missing, "collected": data},
        )

    try:
        qty = int(re.search(r"\d+", str(data["quantity"])).group())  # type: ignore[union-attr]
    except (AttributeError, ValueError, TypeError):
        data.pop("quantity", None)
        _save_data(state, data)
        await db.commit()
        prod = data.get("product_type") or "товару"
        return ChatMessageResponse(
            response=f"Потрібно число штук для «{prod}». Наприклад: 100 або 300",
            action="collect_order_data",
        )

    if data.get("city"):
        data["city"] = _normalize_words(str(data["city"]))
    data["first_name"] = _normalize_words(str(data["first_name"]))
    data["last_name"] = _normalize_words(str(data["last_name"]))

    cart = list(data.get("cart") or [])
    size_mm = data.get("size_mm")
    if not size_mm:
        m = re.search(r"(\d+)\s*мм", str(data.get("product_type") or ""))
        if m:
            size_mm = m.group(1)

    if not cart:
        pricing = _quote_calculate(
            str(data["product_type"]),
            qty,
            size_mm=str(size_mm) if size_mm else None,
            text=str(data.get("product_type") or ""),
        )
        if not pricing.get("success") and "силікон" in str(data["product_type"]).lower():
            pricing = _quote_calculate(
                str(data["product_type"]),
                qty,
                size_mm=str(size_mm or "12"),
                text=str(data.get("product_type") or ""),
            )
        if pricing.get("success"):
            _apply_quote_to_cart(data, pricing, "")
            cart = list(data.get("cart") or [])

    if cart:
        product_label = "; ".join(f"{r['product_name']} × {r['quantity']}" for r in cart)
        qty = sum(int(r.get("quantity") or 0) for r in cart)
        total = round(sum(float(r.get("total_amount") or 0) for r in cart), 2)
        unit_price = cart[0].get("unit_price") if len(cart) == 1 else None
        days = max((int(r.get("production_days") or 0) for r in cart), default=None) or None
    else:
        product_label = data.get("product_type")
        unit_price = data.get("unit_price")
        total = data.get("total_amount")
        days = None
        if data.get("total_amount"):
            try:
                total = float(data["total_amount"])
            except (TypeError, ValueError):
                pass

    client_payload = {
        "first_name": data["first_name"],
        "last_name": data["last_name"],
        "phone": data["phone"],
        "email": data.get("email") or None,
        "city": data.get("city") or None,
        "channel": "telegram",
        "order": {
            "title": product_label,
            "product_type": product_label,
            "quantity": qty,
            "comment": data.get("comment") or data.get("logo"),
        },
    }
    reg = await order_service.register_client_in_crm(db, state.chat_id, client_payload)
    data["product_type"] = product_label

    order = await order_service.create_order(
        db,
        state.chat_id,
        {
            "product_type": product_label,
            "quantity": qty,
            "unit_price": unit_price,
            "total_amount": total,
            "production_days": days,
            "comment": data.get("comment"),
            "params": {"logo": data.get("logo"), "size_mm": size_mm, "cart": cart},
            "client": client_payload,
        },
    )
    data["order_id"] = order.id
    data["client_id"] = reg.get("client_id")
    data["total_amount"] = total
    data["unit_price"] = unit_price

    # якщо доставка вже підтверджена з профілю — одразу LiqPay
    if _has_saved_delivery(data):
        dtype = data.get("saved_delivery_type") or DELIVERY_TYPE_WAREHOUSE
        data["delivery_type"] = dtype
        draft: dict[str, Any] = {}
        if data.get("saved_warehouse"):
            draft["warehouse"] = data["saved_warehouse"]
        if data.get("saved_street_address"):
            draft["street_address"] = data["saved_street_address"]
        if data.get("saved_patronymic"):
            draft["recipient_patronymic"] = data["saved_patronymic"]
        data["delivery_draft"] = draft
        state.step = "collect_delivery"
        _save_data(state, data)
        await db.commit()
        return await _step_collect_delivery(db, state, data, text="ок")

    state.step = "collect_delivery_type"
    _save_data(state, data)
    await db.commit()

    price_note = ""
    if total:
        price_note = f" Разом {total:g} грн — ця сума піде в оплату."
    else:
        price_note = ""

    return ChatMessageResponse(
        response=f"Ок.{price_note}\n\n{DELIVERY_TYPE_PROMPT}",
        action="collect_delivery_data",
        data={"order_id": order.id, "crm": reg},
    )


async def _step_delivery_type(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    lower = text.lower().strip()
    if _is_interrupt_question(text, "delivery_type"):
        return await _answer_interrupt(
            db, state, data, text, DELIVERY_TYPE_PROMPT, "collect_delivery_data",
            field="delivery_type",
        )

    # клієнт каже «я вже все дав» / «ви вже порахували»
    if _is_flow_aside(text):
        if _has_saved_delivery(data) and data.get("order_id"):
            dtype = data.get("saved_delivery_type") or DELIVERY_TYPE_WAREHOUSE
            data["delivery_type"] = dtype
            draft: dict[str, Any] = {}
            if data.get("saved_warehouse"):
                draft["warehouse"] = data["saved_warehouse"]
            if data.get("saved_street_address"):
                draft["street_address"] = data["saved_street_address"]
            data["delivery_draft"] = draft
            state.step = "collect_delivery"
            _save_data(state, data)
            await db.commit()
            return await _step_collect_delivery(db, state, data, text="ок")
        if data.get("total_amount"):
            return ChatMessageResponse(
                response=(
                    f"Так, сума {format_money(data['total_amount'])} грн уже зафіксована. "
                    f"{DELIVERY_TYPE_PROMPT}"
                ),
                action="collect_delivery_data",
            )
        return ChatMessageResponse(
            response=f"Зрозумів. {DELIVERY_TYPE_PROMPT}",
            action="collect_delivery_data",
        )

    city_hint = _extract_city(text)
    if city_hint:
        data["city"] = _normalize_words(city_hint)
    wh_hint = None
    # не плутати вибір меню «1/2/3» з номером відділення
    if lower not in ("1", "2", "3") and re.search(r"\d+", lower):
        wh_hint = _extract_warehouse_number(text)

    if lower in ("1", "відділення", "отделение", "warehouse") or "відділен" in lower:
        dtype = DELIVERY_TYPE_WAREHOUSE
    elif lower in ("2", "поштомат", "postomat") or "поштомат" in lower:
        dtype = DELIVERY_TYPE_POSTOMAT
    elif (
        lower in ("3", "адресна", "адресна доставка", "адреса", "address")
        or "адрес" in lower
    ):
        dtype = DELIVERY_TYPE_ADDRESS
    elif any(w in lower for w in ("пошт", "нп", "нова пошта")):
        dtype = DELIVERY_TYPE_WAREHOUSE
    elif wh_hint and ("відділен" in lower or "поштомат" in lower or city_hint):
        dtype = DELIVERY_TYPE_POSTOMAT if "поштомат" in lower else DELIVERY_TYPE_WAREHOUSE
    else:
        return ChatMessageResponse(
            response="На відділення, у поштомат чи за адресою зручніше?",
            action="collect_delivery_data",
        )

    data["delivery_type"] = dtype
    data["delivery_draft"] = {}
    draft = {}
    if data.get("saved_warehouse") and dtype in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT):
        draft["warehouse"] = data["saved_warehouse"]
    if wh_hint and dtype in (DELIVERY_TYPE_WAREHOUSE, DELIVERY_TYPE_POSTOMAT):
        draft["warehouse"] = wh_hint
        data["saved_warehouse"] = wh_hint
    if data.get("saved_street_address") and dtype == DELIVERY_TYPE_ADDRESS:
        draft["street_address"] = data["saved_street_address"]
        if data.get("saved_patronymic"):
            draft["recipient_patronymic"] = data["saved_patronymic"]
    state.step = "collect_delivery"
    q = _next_delivery_question(data, draft, dtype)
    data["delivery_draft"] = draft
    _save_data(state, data)
    await db.commit()
    if not q:
        return await _step_collect_delivery(db, state, data, text="ок")
    prompt = q or (ADDRESS_PROMPT if dtype == DELIVERY_TYPE_ADDRESS else WAREHOUSE_PROMPT)
    return ChatMessageResponse(
        response=f"Ок. {prompt}",
        action="collect_delivery_data",
        data={"delivery_type": dtype},
    )


async def _step_collect_delivery(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    delivery_type = data.get("delivery_type", DELIVERY_TYPE_WAREHOUSE)
    delivery = data.get("delivery_draft") or {}

    if text.strip().lower() != "ок" and _is_interrupt_question(text, "delivery_field"):
        next_q = _next_delivery_question(data, delivery, str(delivery_type)) or DELIVERY_TYPE_PROMPT
        return await _answer_interrupt(
            db, state, data, text, next_q, "collect_delivery_data", field="delivery_field"
        )

    if _is_flow_aside(text) and text.strip().lower() != "ок":
        next_q = _next_delivery_question(data, delivery, str(delivery_type))
        if not next_q and data.get("order_id"):
            return await _step_collect_delivery(db, state, data, text="ок")
        total = data.get("total_amount")
        extra = f" Сума {float(total):g} грн уже в замовленні." if total else ""
        return ChatMessageResponse(
            response=f"Зрозумів.{extra} {next_q or DELIVERY_TYPE_PROMPT}",
            action="collect_delivery_data",
            data={"delivery_type": delivery_type},
        )

    _apply_delivery_answer(delivery, data, str(delivery_type), text)
    data["delivery_draft"] = delivery

    next_q = _next_delivery_question(data, delivery, str(delivery_type))
    if next_q:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=next_q,
            action="collect_delivery_data",
            data={"delivery_type": delivery_type},
        )

    payload = {
        "delivery_type": delivery_type,
        "recipient_last_name": delivery.get("recipient_last_name") or data.get("last_name"),
        "recipient_first_name": delivery.get("recipient_first_name") or data.get("first_name"),
        "recipient_patronymic": delivery.get("recipient_patronymic"),
        "recipient_phone": delivery.get("recipient_phone") or data.get("phone"),
        "city": delivery.get("city") or data.get("city"),
        "warehouse": delivery.get("warehouse"),
        "street_address": delivery.get("street_address"),
        "weight_kg": 1.0,
        "description": (
            f"{data.get('product_type') or 'Замовлення Silicon'}"
            f" × {data.get('quantity') or '?'} шт"
            f" (#{data.get('order_id', '')})"
        ),
    }
    payload["recipient_name"] = build_recipient_name(payload)

    ok, errors = validate_delivery_payload(payload)
    if not ok:
        return ChatMessageResponse(
            response=errors[0] if errors else "Уточніть дані доставки.",
            action="collect_delivery_data",
            data={"errors": errors},
        )

    delivery_row = await order_service.save_delivery_info(db, state.chat_id, payload)
    data["delivery_id"] = delivery_row.id
    data.pop("delivery_draft", None)
    order_id = data.get("order_id")
    total = data.get("total_amount")

    # одразу LiqPay з прорахованою сумою
    if order_id:
        result = await db.execute(select(Order).where(Order.id == int(order_id)))
        order = result.scalar_one_or_none()
        if order:
            if total and not order.total_amount:
                order.total_amount = float(total)
                await db.commit()
            elif order.total_amount and not total:
                total = float(order.total_amount)
                data["total_amount"] = total
            # якщо суми все ще немає — спробувати перерахувати з прайсу
            if not total and order.product_type and order.quantity:
                size_mm = data.get("size_mm")
                recalc = _quote_calculate(
                    str(order.product_type),
                    int(order.quantity),
                    size_mm=str(size_mm) if size_mm else None,
                    text=str(order.product_type),
                )
                if recalc.get("success"):
                    total = float(recalc["total_amount"])
                    order.total_amount = total
                    order.unit_price = recalc.get("unit_price")
                    if recalc.get("product_name"):
                        order.product_type = recalc["product_name"]
                    data["total_amount"] = total
                    await db.commit()

    # після доставки — вибір: LiqPay або рахунок PDF
    state.step = "collect_payment"
    _save_data(state, data)
    await db.commit()
    if order_id and total:
        return ChatMessageResponse(
            response=PAYMENT_PROMPT,
            action="collect_payment",
            data={"order_id": order_id, "total_amount": total, "delivery_id": delivery_row.id},
        )
    return ChatMessageResponse(
        response=(
            "Замовлення прийняв. Для цієї позиції потрібен індивідуальний прорахунок — "
            "після підтвердження суми запропоную оплату карткою або за рахунком."
        ),
        action="order_submitted",
        data={"delivery_id": delivery_row.id},
    )


async def _step_collect_payment(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Вибір: LiqPay (1) або рахунок на реквізити / ЄДРПОУ (2)."""
    lower = text.lower().strip()
    order_id = data.get("order_id")
    if _says_already_paid(text):
        return await _reply_payment_status(
            db, data, chat_id=state.chat_id, user_text=text
        )
    if _says_pay_link_broken(text):
        return await _reply_payment_link_issue(db, state, data)
    if lower.rstrip("?.!") in {"оплата", "оплатити"} or lower in {
        "оплата?",
        "як оплатити",
        "як оплата",
        "оплату?",
    }:
        return ChatMessageResponse(
            response="Карткою онлайн через LiqPay, рахунок для ФОП/ТОВ або готівкою в офісі. Як зручніше?",
            action="collect_payment",
        )
    if any(w in lower for w in ("наклад", "налож", "післяплат")):
        return ChatMessageResponse(
            response=(
                "Накладеним платежем на Новій пошті чи Укрпошті не відправляємо. "
                "Друковану продукцію запускаємо лише після повної оплати та погодження макету. "
                "Карткою, рахунком ФОП/ТОВ або готівкою в офісі — як зручніше?"
            ),
            action="collect_payment",
        )
    if any(w in lower for w in ("готівк", "кеш", "готівкою", "в офіс")):
        url = (calculator.templates or {}).get("contacts_url") or "https://silicon.com.ua/kontakti"
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                "Оплата готівкою можлива.\n"
                "Для запуску вашої роботи в друк вам потрібно приїхати до нас у офіс та оплатити замовлення готівкою.\n"
                "Передплата 100%.\n"
                f"Адреса та години роботи у розділі Контакти: {url}"
            ),
            action="cash_payment_info",
        )
    if _is_interrupt_question(text, "payment"):
        return await _answer_interrupt(
            db, state, data, text, PAYMENT_PROMPT, "collect_payment", field="payment"
        )

    wants_invoice = lower in {"2"} or any(
        w in lower
        for w in (
            "рахунок",
            "рахунк",
            "реквізит",
            "єдрпоу",
            "edrpou",
            "едрпоу",
            "безготів",
            "фоп",
            "тов",
        )
    )
    if wants_invoice:
        return await _offer_or_start_invoice(db, state, data, text)

    wants_pay = any(
        w in lower
        for w in (
            "1",
            "liqpay",
            "лікпей",
            "лиг",
            "ліг",
            "картк",
            "картою",
            "онлайн",
            "карткою",
            "по карт",
            "так",
            "оплат",
            "посилан",
        )
    )
    if not wants_pay:
        return ChatMessageResponse(
            response=PAYMENT_PROMPT,
            action="collect_payment",
        )

    if not order_id:
        state.step = "idle"
        await db.commit()
        return ChatMessageResponse(
            response="Спочатку оформлюємо замовлення — напишіть «замовити».",
            action="payment_info",
        )

    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order and not order.total_amount and data.get("total_amount"):
        order.total_amount = float(data["total_amount"])
        await db.commit()

    if not order or not order.total_amount:
        state.step = "idle"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response="Поки немає підтвердженої суми — після прорахунку надішлю посилання LiqPay.",
            action="order_submitted",
        )

    pay = await order_service.create_payment(db, int(order_id))
    state.step = "idle"
    _save_data(state, data)
    await db.commit()
    if pay.get("already_paid"):
        return ChatMessageResponse(
            response="Бачу вашу оплату. Беремо замовлення в роботу.",
            action="payment_confirmed",
            data=pay,
        )
    if not pay.get("success"):
        return ChatMessageResponse(
            response="Не вийшло сформувати LiqPay зараз. Можу виставити рахунок за ЄДРПОУ — напишіть «рахунок».",
            action="order_submitted",
        )
    url = pay.get("payment_url") or ""
    if not url.startswith("http"):
        return ChatMessageResponse(
            response="Не вийшло сформувати LiqPay зараз. Можу виставити рахунок за ЄДРПОУ — напишіть «рахунок».",
            action="order_submitted",
        )
    pay_text = _format_payment_message(data, url)
    pay_text += "\n\nЯкщо потрібен рахунок для ФОП/ТОВ — напишіть ЄДРПОУ або «рахунок»."
    return ChatMessageResponse(
        response=pay_text,
        action="payment_created",
        data=pay,
    )


async def _maybe_handle_edrpou_invoice(
    db: AsyncSession,
    state: ConversationState,
    data: dict,
    text: str,
    lower: str,
) -> ChatMessageResponse | None:
    """Пошук контрагента в 1С за ЄДРПОУ/назвою або старт рахунку на реквізити."""
    mentions_edrpou = any(
        w in lower for w in ("єдрпоу", "едрпоу", "edrpou", "єрдпу", "ердпу", "єрдпоу")
    )
    wants_lookup = mentions_edrpou or any(
        w in lower for w in ("контрагент", "знайти фоп", "пошук фоп", "перевір фоп", "знайти тов")
    )
    wants_invoice = _wants_invoice_requisites(text)

    if not wants_lookup and not wants_invoice:
        return None

    edrpou = _extract_edrpou(text)

    # питання «де знайти ЄДРПОУ» — відповідь, не старт збору коду
    if mentions_edrpou and not edrpou and _asks_where_or_what(text):
        return ChatMessageResponse(
            response=EDRPOU_WHERE_REPLY,
            action="consult",
        )

    if wants_lookup or (mentions_edrpou and edrpou):
        if not edrpou:
            # якщо є назва компанії після ключових слів — шукаємо за назвою
            name_guess = None
            for sep in ("назвою", "назва", "компані", "фоп", "тов"):
                if sep in lower:
                    parts = re.split(sep, text, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) > 1 and parts[1].strip():
                        name_guess = parts[1].strip(" :—-.")
                        break
            if name_guess and len(name_guess) >= 3:
                found = await order_service.onec.find_counterparty(name=name_guess)
                if found:
                    return ChatMessageResponse(
                        response=(
                            f"У 1С є контрагент: {found.get('name')}, "
                            f"ЄДРПОУ {found.get('edrpou') or '—'}."
                        ),
                        action="edrpou_found",
                        data=found,
                    )
                return ChatMessageResponse(
                    response=(
                        f"За назвою «{name_guess}» у 1С не знайшов. "
                        "Надішліть код ЄДРПОУ — перевірю ще раз."
                    ),
                    action="edrpou_not_found",
                )
            state.step = "lookup_edrpou"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response="Напишіть код ЄДРПОУ (8–10 цифр) — перевірю контрагента в 1С.",
                action="collect_edrpou",
            )
        found = await order_service.onec.find_counterparty(edrpou=edrpou)
        if found:
            data["edrpou"] = edrpou
            data["company_name"] = found.get("name")
            data["onec_counterparty_id"] = found.get("id")
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=(
                    f"Знайшов у 1С: {found.get('name')}, ЄДРПОУ {found.get('edrpou')}. "
                    "Дублікат не створюю. Потрібен рахунок на цю компанію?"
                ),
                action="edrpou_found",
                data=found,
            )
        data["edrpou"] = edrpou
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                f"За ЄДРПОУ {edrpou} у 1С контрагента немає. "
                "Можу створити при виставленні рахунку — напишіть назву ФОП/ТОВ або «рахунок»."
            ),
            action="edrpou_not_found",
        )

    if wants_invoice:
        return await _offer_or_start_invoice(db, state, data, text)
    return None


async def _step_lookup_edrpou(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    edrpou = _extract_edrpou(text)
    if not edrpou:
        if _is_interrupt_question(text, "edrpou") or _asks_where_or_what(text):
            return await _answer_interrupt(
                db, state, data, text, "", "collect_edrpou", field="edrpou"
            )
        return ChatMessageResponse(
            response="Потрібен код ЄДРПОУ — 8 або 10 цифр.",
            action="collect_edrpou",
        )
    found = await order_service.onec.find_counterparty(edrpou=edrpou)
    state.step = "idle"
    if found:
        data["edrpou"] = edrpou
        data["company_name"] = found.get("name")
        data["onec_counterparty_id"] = found.get("id")
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                f"Знайшов у 1С: {found.get('name')}, ЄДРПОУ {found.get('edrpou')}. "
                "Дублікат не створюю. Потрібен рахунок?"
            ),
            action="edrpou_found",
            data=found,
        )
    data["edrpou"] = edrpou
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(
        response=(
            f"За ЄДРПОУ {edrpou} у 1С немає. "
            "Напишіть назву компанії — створю контрагента при рахунку, без дублікатів."
        ),
        action="edrpou_not_found",
    )


def _skip_optional(val: str) -> bool:
    return val.lower().strip() in (
        "немає",
        "нема",
        "немає.",
        "нет",
        "no",
        "-",
        "пропустити",
        "skip",
        "відсутній",
        "відсутній іпн",
        "без іпн",
    )


def _is_ipn_absent(text: str) -> bool:
    """«немає», «непає» та інші варіанти «ІПН немає» — платник не є платником ПДВ."""
    raw = (text or "").strip().lower()
    if not raw or len(raw) > 40:
        return False
    if _skip_optional(raw):
        return True
    compact = re.sub(r"[\s.]+", "", raw)
    compact = compact.replace("є", "е").replace("ї", "і").replace("'", "")
    if compact.startswith("нема") or compact.startswith("непа") or compact.startswith("безіпн"):
        return True
    if "неплатник" in compact or compact in {"нет", "no", "-", "відсутній", "відсутнии"}:
        return True
    cleaned = re.sub(r"(?i)іпн|ипн|інн|инн|ipn", "", raw).strip(" :—-.")
    return bool(cleaned) and cleaned != raw and _is_ipn_absent(cleaned)


def _is_ipn_skip(text: str) -> bool:
    return _is_ipn_absent(text)


def _numeric_ipn(value: str | None, edrpou: str | None = None) -> str:
    """ІПН лише якщо це код платника ПДВ, не текст «немає» і не копія ЄДРПОУ."""
    raw = str(value or "").strip()
    if not raw or _is_ipn_absent(raw):
        return ""
    digits = re.sub(r"\D", "", raw)
    if not re.fullmatch(r"\d{10,12}", digits):
        return ""
    if edrpou and digits == re.sub(r"\D", "", str(edrpou)):
        return ""
    return digits


def _invoice_fields_ready(data: dict) -> bool:
    return bool(
        data.get("company_name")
        and re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or ""))
        and "ipn" in data
        and data.get("company_address")
    )


def _format_requisites_confirm(data: dict) -> str:
    lines = [
        "Перевіряю реквізити перед рахунком:",
        f"- Назва: {data.get('company_name')}",
        f"- ЄДРПОУ: {data.get('edrpou')}",
    ]
    ipn = _numeric_ipn(data.get("ipn"), data.get("edrpou"))
    if ipn:
        lines.append(f"- ІПН: {ipn}")
    lines.append(f"- Адреса: {data.get('company_address')}")
    lines.append("Все вірно по ваших даних?")
    return "\n".join(lines)


def _saved_invoice_requisites(data: dict) -> dict[str, Any] | None:
    saved = data.get("saved_invoice_requisites")
    if not isinstance(saved, dict):
        return None
    if not saved.get("company_name") or not re.fullmatch(r"\d{8,10}", str(saved.get("edrpou") or "")):
        return None
    if not str(saved.get("company_address") or "").strip():
        return None
    return saved


def _format_saved_requisites_offer(saved: dict) -> str:
    lines = [
        "Вам підготувати рахунок за такими реквізитами?",
        f"- Назва: {saved.get('company_name')}",
        f"- ЄДРПОУ: {saved.get('edrpou')}",
    ]
    ipn = _numeric_ipn(saved.get("ipn"), saved.get("edrpou"))
    if ipn:
        lines.append(f"- ІПН: {ipn}")
    lines.append(f"- Адреса: {saved.get('company_address')}")
    lines.append("Якщо потрібен інший платник — надішліть його реквізити текстом, PDF або фото.")
    return "\n".join(lines)


def _remember_invoice_requisites(data: dict) -> None:
    if not data.get("company_name") or not data.get("edrpou") or not data.get("company_address"):
        return
    data["saved_invoice_requisites"] = {
        "company_name": data.get("company_name"),
        "edrpou": data.get("edrpou"),
        "ipn": _numeric_ipn(data.get("ipn"), data.get("edrpou")),
        "company_address": data.get("company_address"),
        "phone": data.get("phone") or "",
        "onec_counterparty_id": data.get("onec_counterparty_id") or "",
    }
    data["invoice_items_key"] = _invoice_items_key(data)


def _apply_saved_requisites(data: dict, saved: dict) -> None:
    data["company_name"] = saved.get("company_name")
    data["edrpou"] = saved.get("edrpou")
    data["ipn"] = _numeric_ipn(saved.get("ipn"), saved.get("edrpou"))
    data["company_address"] = saved.get("company_address")
    if saved.get("phone") and not data.get("phone"):
        data["phone"] = saved.get("phone")
    if saved.get("onec_counterparty_id"):
        data["onec_counterparty_id"] = saved.get("onec_counterparty_id")


def _price_for_invoice(data: dict, text: str) -> None:
    """Рахунок із товаром і тиражем у тому ж повідомленні має мати суму з прайсу."""
    try:
        total = float(data.get("total_amount") or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total > 0:
        return
    qty_raw = _extract_qty(text) or str(data.get("quantity") or "")
    qty_digits = re.sub(r"\D", "", str(qty_raw))
    hint = _extract_product_hint(text) or data.get("product_type")
    if not qty_digits or not hint:
        return
    size_m = re.search(r"(\d+)\s*мм", (text or "").lower())
    size = size_m.group(1) if size_m else (str(data.get("size_mm")) if data.get("size_mm") else None)
    calc = _quote_calculate(str(hint), int(qty_digits), size_mm=size, text=text or str(hint), data=data)
    if calc.get("success") and float(calc.get("total_amount") or 0) > 0:
        _apply_quote_to_cart(data, calc, text or str(hint))


async def _offer_or_start_invoice(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    _price_for_invoice(data, text)
    saved = _saved_invoice_requisites(data)
    has_new = bool(_extract_edrpou(text) or re.search(r"(?i)\b(тов|тзов|фоп|пп)\b", text or ""))
    if saved and not has_new:
        state.step = "confirm_saved_requisites"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=_format_saved_requisites_offer(saved),
            action="confirm_saved_requisites",
        )
    state.step = "collect_invoice"
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(response=INVOICE_PROMPT, action="collect_invoice_data")


def _confirms_requisites(text: str) -> bool:
    lower = (text or "").lower().strip().strip(".!")
    if any(w in lower for w in ("не вірно", "невірно", "не так", "помил", "виправ", "неправильно")):
        return False
    if "все вірно" in lower or "все правильно" in lower or "підтвердж" in lower:
        return True
    return lower in {
        "так",
        "вірно",
        "так вірно",
        "так, все вірно",
        "ок",
        "окей",
        "добре",
        "да",
        "yes",
    }


def _text_from_requisites_file(path: str | None) -> str:
    """Текст із PDF-реквізитів. Фото без OCR сюди не читається."""
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file() or file_path.suffix.lower() != ".pdf":
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:4]).strip()
    except Exception:
        logger.exception("requisites pdf read failed")
        return ""


async def _ask_requisites_confirm(
    db: AsyncSession, state: ConversationState, data: dict
) -> ChatMessageResponse:
    data.pop("requisites_confirmed", None)
    state.step = "confirm_invoice_data"
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(
        response=_format_requisites_confirm(data),
        action="confirm_invoice_data",
    )


def _invoice_next_prompt(data: dict) -> str:
    if not data.get("company_name"):
        return "Напишіть назву організації (ФОП/ТОВ)."
    if not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
        return "Напишіть код ЄДРПОУ — 8 цифр для ТОВ або 10 для ФОП."
    if not data.get("company_address"):
        return "Напишіть індекс та повну адресу реєстрації."
    return ""


def _invoice_aside_reply(text: str, data: dict) -> str | None:
    """Питання/шум у сценарії рахунку — не записувати як поле."""
    lower = (text or "").strip().lower()
    if not lower:
        return _invoice_next_prompt(data)
    if _wants_invoice_requisites(text) and not _extract_edrpou(text):
        if not data.get("company_name"):
            return INVOICE_PROMPT
        return "RETRY"
    if lower in {
        "рахунок",
        "pdf",
        "пдф",
        "рахунок pdf",
        "надішли рахунок",
        "скинь рахунок",
    }:
        return "RETRY"
    if re.fullmatch(r"[?!.…]+", lower) or lower in {
        "ось",
        "ось він",
        "готово",
        "ок",
        "ok",
        "так",
        "добре",
        "а ви тут",
        "а у ви тут",
        "ау",
        "алло",
    }:
        return _invoice_next_prompt(data) or "RETRY"
    if any(
        w in lower
        for w in (
            "де знайт",
            "де взяти",
            "де подиви",
            "де шукати",
            "як знайт",
            "де його",
        )
    ) or (
        "не знаю" in lower
        and not _extract_edrpou(text)
        and not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or ""))
    ):
        return (
            EDRPOU_WHERE_REPLY
        )
    if any(
        w in lower
        for w in (
            "що далі",
            "що ще потріб",
            "що від мене",
            "де мій рахунок",
            "де рахунок",
            "де мій pdf",
            "рахунок де",
        )
    ):
        return "RETRY"
    if _is_flow_aside(text) and not _extract_edrpou(text):
        return _invoice_next_prompt(data) or "RETRY"
    return None


def _invoice_items_key(data: dict) -> str:
    parts = []
    for row in _invoice_items_from_data(data):
        parts.append(f"{row.get('product')}|{row.get('qty')}|{row.get('amount')}")
    return "||".join(parts)


def _invoice_items_from_data(data: dict) -> list[dict[str, Any]]:
    cart = list(data.get("cart") or [])
    items: list[dict[str, Any]] = []
    for row in cart:
        items.append({
            "product": row.get("product_name") or "Товар Silicon",
            "qty": row.get("quantity") or 1,
            "amount": row.get("total_amount") or 0,
            "unit_price": row.get("unit_price"),
        })
    if not items:
        items.append({
            "product": data.get("product_type") or "Замовлення Silicon",
            "qty": data.get("quantity") or 1,
            "amount": data.get("total_amount") or 0,
            "unit_price": data.get("unit_price"),
        })
    return items


def _safe_local_invoice_pdf(data: dict, order_id: int | None) -> tuple[str, Path] | None:
    try:
        from app.services.invoice_pdf import save_invoice_pdf
    except Exception:
        logger.exception("invoice_pdf import failed")
        return None

    number = str(data.get("invoice_number") or f"INV-{order_id or 'TMP'}")
    try:
        total = float(data.get("total_amount") or 0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        path = save_invoice_pdf(
            invoice_number=number,
            buyer={
                "company_name": data.get("company_name"),
                "edrpou": data.get("edrpou"),
                "ipn": data.get("ipn") or "",
                "company_address": data.get("company_address"),
                "phone": data.get("phone") or "",
                "contract": "Рахунок",
            },
            items=_invoice_items_from_data(data),
            total_amount=total,
            order_id=order_id,
        )
        return number, path
    except Exception:
        logger.exception("Local invoice PDF failed")
        return None


async def _ensure_order_for_invoice(
    db: AsyncSession, state: ConversationState, data: dict
) -> int | None:
    if data.get("order_id"):
        try:
            order_id = int(data["order_id"])
        except (TypeError, ValueError):
            data.pop("order_id", None)
        else:
            try:
                wanted = float(data.get("total_amount") or 0)
            except (TypeError, ValueError):
                wanted = 0.0
            if wanted > 0:
                result = await db.execute(select(Order).where(Order.id == order_id))
                order = result.scalar_one_or_none()
                if order and float(order.total_amount or 0) <= 0:
                    order.total_amount = wanted
                    if data.get("unit_price") is not None:
                        try:
                            order.unit_price = float(data["unit_price"])
                        except (TypeError, ValueError):
                            pass
                    await db.commit()
            return order_id
    _sync_cart_fields(data)
    cart = list(data.get("cart") or [])
    product = str(data.get("product_type") or (
        cart[0].get("product_name") if cart else "Замовлення Silicon"
    ))[:120]
    qty = 1
    try:
        qty = int(re.sub(r"\D", "", str(data.get("quantity") or "")) or "1")
    except ValueError:
        qty = 1
    if cart:
        qty = sum(int(r.get("quantity") or 0) for r in cart) or qty
    try:
        order = await order_service.create_order(
            db,
            state.chat_id,
            {
                "product_type": product,
                "quantity": qty or 1,
                "unit_price": data.get("unit_price"),
                "total_amount": data.get("total_amount") or 0,
                "params": {"cart": cart, "size_mm": data.get("size_mm")},
                "client": {
                    "company_name": data.get("company_name"),
                    "edrpou": data.get("edrpou"),
                    "channel": "telegram",
                    "first_name": (data.get("first_name") or data.get("company_name") or "Клієнт")[:120],
                    "last_name": (data.get("last_name") or "Silicon")[:120],
                },
            },
        )
        data["order_id"] = order.id
        return order.id
    except Exception:
        logger.exception("create_order for invoice failed")
        return None


async def _issue_invoice_now(
    db: AsyncSession, state: ConversationState, data: dict
) -> ChatMessageResponse:
    """Спочатку локальний PDF, потім 1С — клієнт завжди отримує рахунок."""
    try:
        return await _issue_invoice_now_inner(db, state, data)
    except Exception:
        logger.exception("invoice issue failed")
        local = _safe_local_invoice_pdf(data, data.get("order_id") if str(data.get("order_id") or "").isdigit() else None)
        _save_data(state, data)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        if local:
            number, path = local
            return ChatMessageResponse(
                response=_invoice_pdf_client_message(number),
                action="invoice_created",
                data={"invoice_pdf_path": str(path)},
                documents=[ChatDocument(filename=path.name, path=str(path))],
            )
        return ChatMessageResponse(
            response=(
                f"Рахунок для {data.get('company_name') or 'організації'}, "
                f"ЄДРПОУ {data.get('edrpou') or '—'}, "
                f"сума {data.get('total_amount') or 0} грн. "
                "PDF зараз не зібрався — напишіть адресу ще раз або «рахунок»."
            ),
            action="collect_invoice_data",
        )


async def _issue_invoice_now_inner(
    db: AsyncSession, state: ConversationState, data: dict
) -> ChatMessageResponse:
    if data.get("invoice_pdf_path") and Path(str(data["invoice_pdf_path"])).exists():
        saved = _saved_invoice_requisites(data) or {}
        same_buyer = (
            str(data.get("edrpou") or "") == str(saved.get("edrpou") or "")
            and str(data.get("company_address") or "").strip()
            == str(saved.get("company_address") or "").strip()
        )
        same_items = _invoice_items_key(data) == str(data.get("invoice_items_key") or "")
        if same_buyer and same_items:
            state.step = "idle"
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(
                response=_invoice_pdf_client_message(data.get("invoice_number"), resend=True),
                action="invoice_created",
                data={"invoice_pdf_path": data["invoice_pdf_path"]},
                documents=[
                    ChatDocument(
                        filename=data.get("invoice_pdf_filename")
                        or Path(str(data["invoice_pdf_path"])).name,
                        path=str(data["invoice_pdf_path"]),
                    )
                ],
            )

    if not float(data.get("total_amount") or 0):
        _price_for_invoice(
            data,
            f"{data.get('product_type') or ''} {data.get('size_mm') or ''} мм {data.get('quantity') or ''} штук",
        )
    order_id = await _ensure_order_for_invoice(db, state, data)
    extra: dict[str, Any] = {}
    local = _safe_local_invoice_pdf(data, order_id)
    if not local:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                f"Реквізити прийняв: {data.get('company_name')}, ЄДРПОУ {data.get('edrpou')}. "
                "Не вдалося зібрати PDF. Напишіть «рахунок» — спробую ще раз."
            ),
            action="collect_invoice_data",
        )
    number, pdf_path = local
    data["invoice_number"] = number
    data["invoice_pdf_path"] = str(pdf_path)
    data["invoice_pdf_filename"] = pdf_path.name

    if order_id:
        try:
            inv = await order_service.create_invoice(
                db,
                int(order_id),
                {
                    "company_name": data.get("company_name"),
                    "edrpou": data.get("edrpou"),
                    "ipn": data.get("ipn") or "",
                    "company_address": data.get("company_address"),
                    "onec_counterparty_id": data.get("onec_counterparty_id") or "",
                    "items": _invoice_items_from_data(data),
                },
            )
            extra = {k: v for k, v in inv.items() if k != "raw"}
            if inv.get("counterparty_id"):
                data["onec_counterparty_id"] = inv["counterparty_id"]
            returned = str(inv.get("invoice_pdf_path") or "")
            if inv.get("invoice_number"):
                data["invoice_number"] = str(inv["invoice_number"])
            if returned and Path(returned).exists():
                data["invoice_pdf_path"] = returned
                data["invoice_pdf_filename"] = inv.get("invoice_pdf_filename") or Path(returned).name
                pdf_path = Path(returned)
                number = data["invoice_number"]
            else:
                rebuilt = _safe_local_invoice_pdf(data, order_id)
                if rebuilt:
                    number, pdf_path = rebuilt
                    data["invoice_number"] = number
                    data["invoice_pdf_path"] = str(pdf_path)
                    data["invoice_pdf_filename"] = pdf_path.name
        except Exception:
            logger.exception("1C invoice failed, keeping local PDF")

    _remember_invoice_requisites(data)
    state.step = "idle"
    _save_data(state, data)
    await db.commit()
    return ChatMessageResponse(
        response=_invoice_pdf_client_message(number),
        action="invoice_created",
        data={
            "invoice_number": number,
            "invoice_pdf_path": str(pdf_path),
            "invoice_pdf_filename": pdf_path.name,
            **extra,
        },
        documents=[ChatDocument(filename=pdf_path.name, path=str(pdf_path))],
    )


def _harvest_invoice_fields(data: dict, text: str) -> None:
    """Витягнути назву / ЄДРПОУ / ІПН / адресу з вільного тексту, не плутаючи ІПН з ЄДРПОУ."""
    parsed = _parse_kv(text)
    for k in ("company_name", "edrpou", "ipn", "company_address", "street_address"):
        if parsed.get(k):
            if k == "street_address" and not data.get("company_address"):
                data["company_address"] = parsed[k]
            elif k != "street_address":
                data[k] = parsed[k]
    labeled_ipn = re.search(r"(?:іпн|ипн|інн|инн|ipn)\s*[:.—–-]?\s*(\d{8,12})", text, re.I)
    labeled_edrpou = re.search(
        r"(?:єдрпоу|едрпоу|edrpou)\s*[:.—–-]?\s*(\d{8,10})", text, re.I
    )
    if labeled_ipn:
        data["ipn"] = labeled_ipn.group(1)
    elif re.search(
        r"(?:іпн|ипн|інн|инн|ipn)\s*[:.—–-]?\s*(?:немає|нема|непа|відсутн|без)",
        text,
        re.I,
    ):
        data["ipn"] = ""
    if labeled_edrpou:
        data["edrpou"] = labeled_edrpou.group(1)
    if not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
        for line in text.splitlines():
            token = line.strip()
            if re.fullmatch(r"\d{8}|\d{10}", token) and not token.startswith("0"):
                data["edrpou"] = token
                break
    if not data.get("company_name"):
        name_m = re.search(r"((?:тов|тзов|фоп|пп)\.?\s+[^\n,;]+)", text, re.I)
        if name_m:
            data["company_name"] = re.sub(r"\s+", " ", name_m.group(1)).strip()
    if not data.get("company_address"):
        addr = _extract_full_address(text)
        if addr:
            data["company_address"] = addr
    if (
        "ipn" not in data
        and data.get("company_name")
        and re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or ""))
        and data.get("company_address")
        and not _numeric_ipn(_extract_loose_ipn(text), data.get("edrpou"))
    ):
        data["ipn"] = ""
    elif "ipn" in data:
        data["ipn"] = _numeric_ipn(data.get("ipn"), data.get("edrpou"))

    name_m = re.search(r"((?:тов|тзов|фоп|пп)\.?\s+[^\n,;]+)", text, re.I)
    fresh_name = re.sub(r"\s+", " ", name_m.group(1)).strip(" .,;") if name_m else ""
    fresh_addr = _extract_full_address(text) or ""
    fresh_code = ""
    if labeled_edrpou:
        fresh_code = labeled_edrpou.group(1)
    else:
        for line in text.splitlines():
            token = line.strip()
            if re.fullmatch(r"\d{8}|\d{10}", token) and not token.startswith("0"):
                fresh_code = token
                break
    replacing = bool(fresh_code and (fresh_name or fresh_addr or not data.get("edrpou")))
    if fresh_name:
        data["company_name"] = fresh_name
    if replacing and fresh_code:
        if fresh_code != str(data.get("edrpou") or ""):
            data.pop("onec_counterparty_id", None)
            data.pop("_edrpou_checked", None)
            data.pop("invoice_pdf_path", None)
            data.pop("invoice_number", None)
            data.pop("invoice_pdf_filename", None)
        data["edrpou"] = fresh_code
    if fresh_addr:
        if fresh_addr != str(data.get("company_address") or "").strip():
            data.pop("invoice_pdf_path", None)
            data.pop("invoice_number", None)
            data.pop("invoice_pdf_filename", None)
        data["company_address"] = fresh_addr
    if fresh_name and fresh_code and fresh_addr:
        data["ipn"] = _numeric_ipn(_extract_loose_ipn(text), fresh_code)


async def _step_collect_invoice(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Збір: назва → ЄДРПОУ → ІПН → адреса → PDF-рахунок (навіть без повного CRM)."""
    if _says_already_provided(text):
        prompt = _invoice_next_prompt(data)
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(
            response=(
                prompt
                or "Бачу попереднє повідомлення. Якщо всі реквізити вже були — напишіть ЄДРПОУ ще раз, 8 або 10 цифр."
            ),
            action="collect_invoice_data",
        )
    _harvest_invoice_fields(data, text)
    aside = _invoice_aside_reply(text, data)
    edrpou_in_text = None
    if not re.search(r"(?:іпн|ипн|інн|инн|ipn)\s*[:.—–-]?\s*\d{8,12}", text, re.I):
        edrpou_in_text = _extract_edrpou(text)
    elif re.search(r"(?:єдрпоу|едрпоу|edrpou)", text, re.I):
        edrpou_in_text = _extract_edrpou(text)
    if aside == "RETRY" or (aside and edrpou_in_text):
        if edrpou_in_text and not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
            data["edrpou"] = edrpou_in_text
            aside = None
        elif aside == "RETRY":
            if _invoice_fields_ready(data):
                return await _ask_requisites_confirm(db, state, data)
            prompt = _invoice_next_prompt(data)
            _save_data(state, data)
            await db.commit()
            return ChatMessageResponse(response=prompt, action="collect_invoice_data")
    if aside:
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=aside, action="collect_invoice_data")

    parsed = _parse_kv(text)
    for k in ("company_name", "edrpou", "ipn", "company_address", "street_address"):
        if parsed.get(k):
            if k == "street_address" and not data.get("company_address"):
                data["company_address"] = parsed[k]
            elif k != "street_address":
                data[k] = parsed[k]

    val = text.strip()

    if val and not parsed:
        if _is_ipn_skip(val) and "ipn" not in data:
            data["ipn"] = ""
        elif (edrpou_in_text or re.fullmatch(r"\d{8,10}", val)) and not re.fullmatch(
            r"\d{8,10}", str(data.get("edrpou") or "")
        ):
            data["edrpou"] = edrpou_in_text or val
        elif not data.get("company_name"):
            data["company_name"] = val
        elif not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
            data["edrpou"] = edrpou_in_text or (val if re.fullmatch(r"\d{8,10}", val) else None)
            if not data.get("edrpou"):
                _save_data(state, data)
                await db.commit()
                return ChatMessageResponse(
                    response="Потрібен саме код ЄДРПОУ — 8 цифр для ТОВ або 10 для ФОП.",
                    action="collect_invoice_data",
                )
        elif (
            "ipn" not in data
            and val != str(data.get("edrpou") or "")
            and (_is_ipn_absent(val) or re.fullmatch(r"\d{10,12}", val))
        ):
            data["ipn"] = _numeric_ipn(val, data.get("edrpou"))
        elif not data.get("company_address") and not re.fullmatch(r"\d{8,12}", val):
            data["company_address"] = _extract_full_address(val) or val
    elif edrpou_in_text and not re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or "")):
        data["edrpou"] = edrpou_in_text

    if (
        "ipn" not in data
        and data.get("company_name")
        and re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or ""))
        and data.get("company_address")
        and _extract_full_address(text)
    ):
        data["ipn"] = _numeric_ipn(_extract_loose_ipn(text), data.get("edrpou"))

    prompt = _invoice_next_prompt(data)
    if prompt:
        if (
            data.get("edrpou")
            and re.fullmatch(r"\d{8,10}", str(data.get("edrpou") or ""))
            and not data.get("_edrpou_checked")
            and "ipn" not in data
        ):
            try:
                found = await order_service.onec.find_counterparty(edrpou=str(data["edrpou"]))
            except Exception:
                found = None
            data["_edrpou_checked"] = True
            if found:
                data["onec_counterparty_id"] = found.get("id")
                if found.get("name") and data.get("company_name") in (None, "", found.get("name")):
                    data["company_name"] = found.get("name")
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=prompt, action="collect_invoice_data")

    _save_data(state, data)
    await db.commit()
    if _invoice_fields_ready(data):
        return await _ask_requisites_confirm(db, state, data)
    return ChatMessageResponse(
        response=_invoice_next_prompt(data),
        action="collect_invoice_data",
    )


async def _step_confirm_invoice_data(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Перед PDF — клієнт підтверджує прописані в чаті реквізити."""
    lower = (text or "").lower()
    if _confirms_requisites(text) and _invoice_fields_ready(data):
        data["requisites_confirmed"] = True
        return await _issue_invoice_now(db, state, data)
    if lower.strip() in {"ні", "нет", "no"} or any(
        w in lower for w in ("не вірно", "невірно", "не так", "виправ", "помил")
    ):
        return ChatMessageResponse(
            response=(
                "Добре, рахунок ще не формую. Напишіть, що змінити: "
                "назву, ЄДРПОУ, ІПН чи адресу — або надішліть виправлені реквізити текстом чи PDF."
            ),
            action="confirm_invoice_data",
        )
    state.step = "collect_invoice"
    return await _step_collect_invoice(db, state, data, text)


async def _step_confirm_saved_requisites(
    db: AsyncSession, state: ConversationState, data: dict, text: str
) -> ChatMessageResponse:
    """Повторне замовлення: той самий платник, без нового контрагента."""
    saved = _saved_invoice_requisites(data)
    if _confirms_requisites(text) and saved:
        _apply_saved_requisites(data, saved)
        data["requisites_confirmed"] = True
        if _invoice_items_key(data) != str(data.get("invoice_items_key") or ""):
            data.pop("invoice_pdf_path", None)
            data.pop("invoice_number", None)
            data.pop("invoice_pdf_filename", None)
        return await _issue_invoice_now(db, state, data)
    if _extract_edrpou(text) or re.search(r"(?i)\b(тов|тзов|фоп|пп)\b", text or ""):
        state.step = "collect_invoice"
        return await _step_collect_invoice(db, state, data, text)
    lower = (text or "").lower()
    if lower.strip() in {"ні", "нет", "no"} or any(w in lower for w in ("інш", "інший плат", "нові реквізит")):
        state.step = "collect_invoice"
        _save_data(state, data)
        await db.commit()
        return ChatMessageResponse(response=INVOICE_PROMPT, action="collect_invoice_data")
    return ChatMessageResponse(
        response=_format_saved_requisites_offer(saved or {}),
        action="confirm_saved_requisites",
    )


def _extract_loose_ipn(text: str) -> str:
    labeled = re.search(r"(?:іпн|ипн|інн|инн|ipn)\s*[:.—–-]?\s*(\d{10,12})", text or "", re.I)
    return labeled.group(1) if labeled else ""


def _extract_full_address(text: str) -> str | None:
    """Повна адреса, включно з індексом і областю, не лише «вул. …»."""
    mark = re.compile(
        r"(індекс|\b\d{5}\b|обл\.|область|р-н|район|вул\.|вулиця|просп|бульв|буд\.|"
        r"\bм\.|місто|\bс\.|село|селище|офіс)",
        re.I,
    )
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    picked: list[str] = []
    for ln in lines:
        if re.fullmatch(r"\d{8,12}", ln) or _is_ipn_absent(ln):
            continue
        if re.match(r"(?i)^(тов|тзов|фоп|пп)\b", ln) and not mark.search(ln):
            continue
        found = mark.search(ln)
        if not found:
            continue
        chunk = ln
        if re.search(r"(?i)іпн|ипн|інн|єдрпоу|едрпоу|edrpou|скрін|тов|фоп|тзов", ln):
            chunk = ln[found.start():]
        chunk = re.sub(
            r"(?i)(?:іпн|ипн|інн|инн|ipn)\s*[:.—–-]?\s*(?:непа\w*|нема\w*|відсут\w*|\d{8,12})?",
            "",
            chunk,
        )
        chunk = re.sub(r"\s+", " ", chunk).strip(" ,")
        if chunk:
            picked.append(chunk)
    if not picked:
        return None
    return re.sub(r"\s+", " ", " ".join(picked)).strip(" ,")
