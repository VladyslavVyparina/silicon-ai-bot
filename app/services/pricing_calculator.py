import json
import re
from pathlib import Path

from app.config import get_settings

settings = get_settings()


def format_money(value: float | int | str | None) -> str:
    """1150.0 → «1150», 11.5 → «11.5» (без зайвих нулів)."""
    if value is None:
        return "0"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    text = f"{n:.2f}".rstrip("0").rstrip(".")
    return text


def format_quote_message(result: dict, *, with_confirm: bool = True, cart: list | None = None) -> str:
    """Формулювання прорахунку для клієнта."""
    name = (result.get("product_name") or "товар").strip()
    soft = name[0].lower() + name[1:] if name else name
    qty = result.get("quantity")
    unit = format_money(result.get("unit_price"))
    total = format_money(result.get("total_amount"))
    days = result.get("production_days") or settings.default_production_days
    lead = result.get("production_note") or f"Строк близько {days} робочих днів."

    extras_note = (result.get("extras_note") or "").strip()
    extras_total = float(result.get("extras_total") or 0)
    if extras_note and extras_total:
        base_total = format_money(result.get("base_total") or result.get("total_amount"))
        parts = [
            f"На {qty} шт, {soft}: {unit} грн/шт, разом {base_total} грн.",
            extras_note,
            f"Разом з доплатами: {total} грн.",
            lead,
        ]
    else:
        parts = [
            f"На {qty} шт, {soft}: {unit} грн/шт, разом {total} грн.",
            lead,
        ]
        if extras_note:
            parts.insert(1, extras_note)
    multi = result.get("multi_quotes") or []
    if multi:
        parts = []
        for q in multi:
            n = (q.get("product_name") or "товар").strip()
            soft_n = n[0].lower() + n[1:] if n else n
            parts.append(
                f"На {q.get('quantity')} шт, {soft_n}: "
                f"{format_money(q.get('unit_price'))} грн/шт, разом "
                f"{format_money(q.get('total_amount'))} грн."
            )
        note = (result.get("manager_note") or result.get("error") or "").strip()
        if note:
            parts.append(note)
        else:
            parts.append(lead if isinstance(lead, str) else f"Строк близько {days} робочих днів.")
    elif result.get("manager_note"):
        parts.append(str(result["manager_note"]).strip())

    if cart and len(cart) > 1:
        parts.extend(["", "У замовленні зараз:"])
        for item in cart:
            parts.append(
                f"- {item.get('product_name')} × {item.get('quantity')} шт — "
                f"{format_money(item.get('total_amount'))} грн"
            )
        parts.append(f"Разом: {format_money(sum(float(i.get('total_amount') or 0) for i in cart))} грн.")
    if with_confirm:
        parts.extend(
            [
                "",
                "Оформлюємо? Якщо треба ще позицію в це замовлення — напишіть яка і скільки.",
            ]
        )
    return "\n".join(parts)


class PricingCalculator:
    """Розрахунок вартості та термінів виготовлення брендованої продукції."""

    def __init__(self, pricing_path: str | None = None):
        path = Path(pricing_path or settings.pricing_data_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent.parent / path
        with open(path, encoding="utf-8") as f:
            self._data = json.load(f)

    @property
    def templates(self) -> dict:
        return self._data.get("templates") or {}

    @staticmethod
    def _is_other_than_silicone_bracelet(lower: str) -> bool:
        return any(
            w in lower
            for w in (
                "шапоч",
                "плаванн",
                "чашк",
                "екочаш",
                "еко-чаш",
                "шнур",
                "бейдж",
                "килим",
                "брелок",
                "голограм",
                "ретрактор",
                "кліп рулетк",
                "витяжн",
                "атласн",
                "тканинн",
                "slap",
                "слеп",
                "tyvek",
                "контрол",
                "rfid",
                "світловідб",
                "відбивн",
                "рефлект",
                "кишеньк",
                "кишеня",
                "кардхолдер",
                "картхолдер",
            )
        )

    @staticmethod
    def detect_print_variant(text: str) -> str | None:
        """blank | 1color | color | 1+1 | 2+2 | 3+3 | 4+4 | None."""
        lower = (text or "").lower()
        is_cap = any(w in lower for w in ("шапоч", "плаванн"))
        is_slap = "slap" in lower or "слеп" in lower
        # 4+4 лише для шапочок; для slap «4 кольори» — це print_colors
        if re.search(r"\b4\s*\+\s*4\b|друк\s*4\+4", lower) or (
            is_cap and re.search(r"4\s*кольор", lower)
        ):
            return "4+4"
        if re.search(r"\b3\s*\+\s*3\b|друк\s*3\+3", lower) or (
            is_cap and re.search(r"3\s*кольор", lower)
        ):
            return "3+3"
        if re.search(r"\b2\s*\+\s*2\b|друк\s*2\+2", lower) or (
            is_cap and re.search(r"2\s*кольор", lower)
        ):
            return "2+2"
        if re.search(r"\b1\s*\+\s*1\b|друк\s*1\+1", lower):
            return "1+1"
        if is_slap and re.search(r"\d\s*(?:колір|кольор)", lower):
            if re.search(r"1\s*колір", lower) and not re.search(r"[2-4]\s*(?:колір|кольор)", lower):
                return "1color"
            return None
        if any(
            w in lower
            for w in (
                "кольоров",
                "колірним лого",
                "кольоровим лого",
                "повноколір",
                "full color",
                "fullcolor",
                "кілька кольор",
                "декілька кольор",
            )
        ):
            return "color"
        if any(w in lower for w in ("без друку", "без лого", "без логотип")):
            return "blank"
        if any(w in lower for w in ("1 колір", "1 цвет", "одноколір", "одним кольором", "чорним")):
            return "1color"
        return None

    _NUM_WORDS = {
        "один": 1, "одне": 1, "одного": 1,
        "два": 2, "дві": 2, "двох": 2,
        "три": 3, "трьох": 3,
        "чотири": 4, "чотирьох": 4,
        "п'ять": 5, "пять": 5, "п’ять": 5,
    }

    @classmethod
    def detect_layout_count(cls, text: str) -> int:
        """Скільки різних макетів у тиражі (для доплати за кожен наступний)."""
        lower = (text or "").lower()
        m = re.search(r"(\d+)\s*(?:різн\w+\s+)?макет", lower)
        if m:
            return max(1, int(m.group(1)))
        wm = re.search(r"(один|одне|два|дві|двох|три|трьох|чотири|чотирьох|п'ять|пять)\s+(?:різн\w+\s+)?макет", lower)
        if wm:
            return cls._NUM_WORDS.get(wm.group(1), 1)
        return 1

    @staticmethod
    def detect_size_variants_count(text: str) -> int:
        """Скільки різних розмірів силіконових браслетів у тиражі."""
        lower = (text or "").lower()
        combos = set(re.findall(r"(202|180|160)\s*[*xх×]\s*(5|12|20)", lower))
        if len(combos) >= 2:
            return len(combos)
        circs = set(re.findall(r"\b(202|180|160)\b", lower))
        if len(circs) >= 2:
            return len(circs)
        return 1

    @staticmethod
    def detect_segmented(text: str) -> dict | None:
        """Сегментні силіконові браслети: прив'язка друку до сегментів."""
        lower = (text or "").lower()
        if not re.search(r"сегмент|сигмент", lower):
            return None
        tie = 0
        if re.search(r"прив.?язк", lower) or "привяз" in lower:
            if re.search(r"двох\s+сегмент|до\s+двох|2\s+сегмент", lower):
                tie = 2
            elif re.search(r"одн\w*\s+сегмент|до\s+одного|1\s+сегмент", lower):
                tie = 1
        return {"tie": tie}

    @staticmethod
    def detect_print_colors(text: str) -> int | None:
        lower = (text or "").lower()
        found = [
            min(4, max(1, int(x)))
            for x in re.findall(r"(\d)\s*(?:колір|кольор)", lower)
        ]
        if len(found) >= 2:
            return max(found)
        if found:
            return found[0]
        if "кольоров" in lower and "1 колір" not in lower:
            return None
        return None

    @staticmethod
    def detect_print_color_list(text: str) -> list[int]:
        """Усі згадані кількості кольорів друку (для slap 1 і 4 тощо)."""
        lower = (text or "").lower()
        found = [
            min(4, max(1, int(x)))
            for x in re.findall(r"(\d)\s*(?:колір|кольор)", lower)
        ]
        out: list[int] = []
        for c in found:
            if c not in out:
                out.append(c)
        if not out and re.search(
            r"кольоров|повноколір|кілька\s+кольор|декілька\s+кольор", lower
        ):
            return [4]
        return out

    @staticmethod
    def detect_lanyard_accessories(text: str) -> dict:
        lower = (text or "").lower()
        two_carab = bool(
            re.search(r"двом[аи]\s+карабін|2\s*карабін|два\s+карабін|карабін(?:ами|и)?\s*2", lower)
        )
        fastex = "фастекс" in lower
        safety = "роз'єм" in lower or "розєм" in lower or "безпек" in lower
        retractor = "ретрактор" in lower and "шнур" in lower
        loop = "петл" in lower
        return {
            "second_carabiner": two_carab,
            "fastex": fastex,
            "safety_break": safety,
            "sewn_retractor": retractor,
            "loop": loop,
        }

    def _unit_from_tiers(self, tiers: list, quantity: int) -> float | None:
        for tier in tiers:
            min_qty = tier["min_qty"]
            max_qty = tier["max_qty"]
            if quantity >= min_qty and (max_qty is None or quantity <= max_qty):
                return float(tier["price_per_unit"])
        return None

    def _resolve_product_key(
        self,
        product_type: str,
        size_mm: str | None = None,
        *,
        print_variant: str | None = None,
        text: str = "",
    ) -> str | None:
        products = self._data["products"]
        if product_type in products:
            return product_type
        lower = f"{product_type or ''} {text or ''}".lower()
        if not size_mm:
            m = re.search(r"(\d+)\s*мм", lower)
            if m:
                size_mm = m.group(1)
        size_mm = str(size_mm) if size_mm else None
        variant = print_variant or self.detect_print_variant(lower)

        # Retractor before lanyard/badge (слово «бейдж» не має тягнути шнурок)
        if "кишеньк" in lower or "кишеня" in lower or "кишені" in lower:
            if re.search(r"\ba6\b", lower) or "118" in lower:
                return "badge_pocket_a6"
            if re.search(r"\ba5\b", lower) or "162" in lower:
                return "badge_pocket_a5"
            dim_map = {
                frozenset(("65", "115")): "badge_pocket_65x115",
                frozenset(("100", "85")): "badge_pocket_100x85",
                frozenset(("83", "135")): "badge_pocket_83x135",
                frozenset(("115", "100")): "badge_pocket_115x100",
                frozenset(("96", "150")): "badge_pocket_96x150",
            }
            for a, b in re.findall(r"(\d+)\s*[*xх×]\s*(\d+)", lower):
                key = dim_map.get(frozenset((a, b)))
                if key:
                    return key
            return None
        if "кардхолдер" in lower or "картхолдер" in lower:
            if any(w in lower for w in ("без друку", "без лого")):
                return "cardholder_plastic_blank"
            if variant == "blank":
                return "cardholder_plastic_blank"
            return "cardholder_plastic_blank"

        if any(w in lower for w in ("ретрактор", "кліп рулетк", "витяжн")):
            wants_print = (
                variant in ("color", "1color")
                or "з друком" in lower
                or "друком" in lower
                or "з лого" in lower
                or "лого" in lower
            )
            wants_blank = variant == "blank" or "без друку" in lower
            if "пришив" in lower or (size_mm == "20" and "55" not in lower):
                if wants_blank and not wants_print:
                    return "retractor_sewn_20_blank"
                return "retractor_sewn_20_print" if wants_print else "retractor_sewn_20_blank"
            if wants_blank and not wants_print:
                return "retractor_55_blank"
            if wants_print:
                return "retractor_55_print"
            # «ретрактор для бейджа» без уточнення — орієнтир з друком, якщо питають вартість виготовлення з друком
            return "retractor_55_print" if "варт" in lower or "цін" in lower else "retractor_55_blank"

        # Keychains
        if "брелок" in lower:
            if "чорн" in lower:
                return "brelok_black_carabiner"
            if "кольор" in lower and "карабін" in lower:
                return "brelok_color_carabiner"
            if "кільц" in lower or "кільце" in lower:
                return "brelok_ring"
            return "brelok_carabiner"

        # Swim caps
        if any(w in lower for w in ("шапоч", "плаванн")):
            mapping = {
                "4+4": "swim_cap_4plus4",
                "3+3": "swim_cap_3plus3",
                "2+2": "swim_cap_2plus2",
                "1+1": "swim_cap_1plus1",
            }
            if variant in mapping:
                return mapping[variant]
            if variant == "color" or (self.detect_print_colors(lower) or 0) >= 4:
                return "swim_cap_4plus4"
            return "swim_cap_1plus1"

        # Eco cups
        if any(w in lower for w in ("чашк", "екочаш", "еко-чаш")):
            if "450" in lower:
                return "eco_cup_450_print"
            return "eco_cup_350_print"

        # Reflective
        if any(w in lower for w in ("світловідб", "відбивн", "рефлект", "reflective", "reflect")):
            size400 = "400" in lower
            if variant == "color":
                return "reflective_400_color" if size400 else "reflective_300_color"
            return "reflective_400_print" if size400 else "reflective_300_print"

        # Tyvek / control
        if any(w in lower for w in ("tyvek", "контрол")):
            if variant == "blank" or "без друку" in lower:
                return "tyvek_blank"
            if variant == "color":
                return "tyvek_color"
            return "tyvek_print"

        # Slap
        if "slap" in lower or "слеп" in lower:
            if "255" in lower:
                return "slap_255_print"
            return "slap_215_print"

        # Fabric
        if "тканинн" in lower:
            return "fabric_bracelet_15mm"

        # Lanyards (after retractor!)
        if any(w in lower for w in ("шнур", "бейдж")) and "брелок" not in lower:
            width = size_mm if size_mm in ("15", "20", "25", "30", "40") else None
            if not width:
                for w in ("40", "30", "25", "20", "15"):
                    if re.search(rf"{w}\s*мм", lower):
                        width = w
                        break
            width = width or "15"
            key = (self._data.get("lanyard_width_to_key") or {}).get(width) or f"lanyard_{width}mm"
            if key in products:
                return key

        # keyword fallback (skip silicone size map for non-silicone)
        for key, product in products.items():
            for kw in product.get("keywords") or []:
                if kw and kw in lower:
                    if key.startswith("silicone_bracelet") and size_mm:
                        mapped = self._data.get("size_to_product_key", {}).get(size_mm)
                        if mapped:
                            return mapped
                    if key.startswith("lanyard_") and size_mm:
                        mapped = (self._data.get("lanyard_width_to_key") or {}).get(size_mm)
                        if mapped:
                            return mapped
                    return key

        if self._is_other_than_silicone_bracelet(lower):
            return None

        if any(w in lower for w in ("силікон", "silicone", "браслет")):
            mapped = self._data.get("size_to_product_key", {}).get(size_mm or "12")
            return mapped or "silicone_bracelet_12mm"
        return None

    def _display_name(self, product: dict, product_key: str, accessories: dict | None = None) -> str:
        name = product.get("name") or product_key
        accessories = accessories or {}
        if not product_key.startswith("lanyard_"):
            return name
        width = product.get("width_mm") or ""
        parts = ["Шнурок для бейджа з логотипом"]
        if accessories.get("loop") and not accessories.get("second_carabiner"):
            parts.append("і петлею")
        elif accessories.get("second_carabiner"):
            parts.append("і двома карабінами")
        else:
            parts.append("і карабіном")
        extras = []
        if accessories.get("fastex"):
            extras.append("фастексом")
        if accessories.get("safety_break"):
            extras.append("роз'ємом безпеки")
        if accessories.get("sewn_retractor"):
            extras.append("ретрактором")
        if extras:
            # «з логотипом, карабіном, фастексом»
            base = f"Шнурок для бейджа з логотипом, карабіном, {', '.join(extras)}"
            if accessories.get("second_carabiner"):
                base = f"Шнурок для бейджа з логотипом, двома карабінами, {', '.join(extras)}"
            return f"{base}, {width} мм"
        return f"{' '.join(parts)}, {width} мм"

    def starting_quote(self, product_type: str, size_mm: str | None = None, text: str = "") -> str | None:
        """Короткий орієнтир без тиражу — не підміняти товар."""
        blob = f"{product_type or ''} {text or ''}"
        lower = blob.lower()
        # загальний запит на шнурки без ширини — повний FAQ, не 15 мм за замовчуванням
        if any(w in lower for w in ("шнур", "бейдж")) and not any(
            w in lower for w in ("ретрактор", "кліп рулетк", "витяжн", "брелок")
        ):
            if not size_mm and not re.search(r"\d+\s*мм", lower):
                faq = (self.templates or {}).get("lanyard_general")
                if faq:
                    return faq
        key = self._resolve_product_key(product_type, size_mm, text=blob)
        if not key:
            return None
        product = self._data["products"][key]
        if key.startswith("slap_"):
            return self._slap_starting_quote(product, blob)
        if key.startswith("silicone_bracelet"):
            # повна таблиця тиражів; при запиті строку — додати термін
            lines = [f"{product['name']} (друк лого 1 колір), грн/шт:"]
            for t in product["tiers"]:
                hi = t.get("max_qty")
                span = f"{t['min_qty']}–{hi}" if hi else f"{t['min_qty']}+"
                lines.append(f"{span}: {format_money(t['price_per_unit'])}")
            if any(w in lower for w in ("термін", "строк", "коли буде", "скільки днів")):
                lines.append(
                    "Строк зазвичай 5–7 робочих днів. Від тиражу може збільшуватись. "
                    "Індивідуальний / кольоровий друк — за макетом."
                )
            lines.append("Напишіть тираж — порахую точну суму.")
            return "\n".join(lines)
        if key.startswith("fabric_"):
            return (
                f"{product['name']}: від {product['tiers'][0]['min_qty']} шт — "
                f"{format_money(product['tiers'][0]['price_per_unit'])} грн/шт. "
                "Стрічка 15 мм, довжина 350 мм. Фіксатори чорний/білий (на ціну не впливає). "
                "Друк лого на зовнішній стороні; внутрішня — фон або без друку."
            )
        if key.startswith("tyvek_"):
            return (
                "Контрольні браслети Tyvek. Строк 2–3 робочі дні. "
                "1 колір = чорний друк на будь-якому кольорі браслета; "
                "кольоровий друк за прайсом — лише на білих. "
                "https://silicon.com.ua/kontrolni-braslety-tyvek "
                "Напишіть тираж і варіант друку — порахую."
            )
        tier = product["tiers"][0]
        unit = format_money(tier["price_per_unit"])
        min_qty = tier["min_qty"]
        extra = (product.get("note") or "").strip()
        msg = f"{product['name']}: від {min_qty} шт — {unit} грн/шт."
        if extra:
            msg = f"{msg} {extra}"
        return msg

    def _slap_starting_quote(self, product: dict, text: str) -> str:
        """Slap: таблиця кольорів друку або обидва варіанти з запиту."""
        tier = product["tiers"][0]
        base = float(tier["price_per_unit"])
        min_qty = int(tier["min_qty"])
        extra = float(product.get("extra_color_uah") or 10)
        site = "https://silicon.com.ua/silikonovi-slep-braslety"
        colors = self.detect_print_color_list(text)

        def _color_word(n: int) -> str:
            if n == 1:
                return "1 колір друку"
            if n == 4:
                return "4 кольори друку"
            return f"{n} кольори друку"

        lines: list[str] = []
        if len(colors) >= 2:
            lines.append(
                f"Slap-браслет 215×25 мм з друком лого. Одноколірний — за прайсом; "
                f"макс. 4 кольори, +{format_money(extra)} грн/шт за кожен наступний."
            )
            for c in colors:
                price = base + extra * (c - 1)
                lines.append(
                    f"{_color_word(c)}: від {min_qty} шт — {format_money(price)} грн/шт."
                )
        elif len(colors) == 1 and colors[0] > 1:
            c = colors[0]
            price = base + extra * (c - 1)
            lines.append(
                f"Slap-браслет 215×25 мм з друком {_color_word(c)}: "
                f"від {min_qty} шт — {format_money(price)} грн/шт."
            )
            lines.append(
                f"+{format_money(extra)} грн/шт за кожен колір понад 1 (макс. 4)."
            )
        elif len(colors) == 1 and colors[0] == 1:
            lines.append(
                f"Slap-браслет 215×25 мм з друком 1 колір: "
                f"від {min_qty} шт — {format_money(base)} грн/шт."
            )
        else:
            lines.append(
                f"Slap-браслет 215×25 мм. Одноколірний друк лого — за прайсом. "
                f"Макс. 4 кольори друку, +{format_money(extra)} грн/шт за кожен наступний."
            )
            lines.append(f"Орієнтир на {min_qty} шт:")
            for c in (1, 2, 3, 4):
                price = base + extra * (c - 1)
                lines.append(f"- {_color_word(c)}: {format_money(price)} грн/шт")

        lines.append(f"Кольори самих браслетів: {site}")
        lines.append("Напишіть тираж — порахую суму.")
        return "\n".join(lines)

    def get_unit_price(self, product_key: str, quantity: int, size_mm: str | None = None) -> float | None:
        product = self._data["products"].get(product_key)
        if not product:
            return None
        widths = product.get("widths")
        if widths and size_mm and str(size_mm) in widths:
            return self._unit_from_tiers(widths[str(size_mm)], quantity)
        return self._unit_from_tiers(product["tiers"], quantity)

    def calculate(
        self,
        product_type: str,
        quantity: int,
        size_mm: str | None = None,
        urgent: bool = False,
        *,
        print_variant: str | None = None,
        print_colors: int | None = None,
        accessories: dict | None = None,
        text: str = "",
    ) -> dict:
        blob = f"{product_type or ''} {text or ''}"
        accessories = accessories or self.detect_lanyard_accessories(blob)
        print_variant = print_variant or self.detect_print_variant(blob)
        if print_colors is None:
            print_colors = self.detect_print_colors(blob)

        product_key = self._resolve_product_key(
            product_type, size_mm, print_variant=print_variant, text=text
        )
        if not product_key:
            return {
                "success": False,
                "error": "Невідомий тип виробу. Зверніться до менеджера для індивідуального прорахунку.",
            }

        product = self._data["products"][product_key]
        width_for_brelok = size_mm
        if product_key.startswith("brelok_") and not width_for_brelok:
            m = re.search(r"(20|25)\s*мм", blob.lower())
            width_for_brelok = m.group(1) if m else "20"

        unit_price = self.get_unit_price(product_key, quantity, width_for_brelok)
        if unit_price is None:
            return {
                "success": False,
                "error": f"Мінімальний тираж для {product['name']} — {product['tiers'][0]['min_qty']} шт.",
            }

        # Slap: +10 грн за кожен наступний колір (макс 4)
        extra_color = float(product.get("extra_color_uah") or 0)
        max_colors = int(product.get("max_print_colors") or 1)
        colors = print_colors or 1
        if extra_color and colors > 1:
            colors = min(colors, max_colors)
            unit_price = round(unit_price + extra_color * (colors - 1), 2)

        # Сегментні силіконові браслети: базово +2 грн/шт; прив'язка друку +2/+4
        segmented = self.detect_segmented(blob) if product_key.startswith("silicone_bracelet") else None
        if segmented is not None:
            seg_add = 2.0
            if segmented["tie"] == 1:
                seg_add += 2.0
            elif segmented["tie"] >= 2:
                seg_add += 4.0
            unit_price = round(unit_price + seg_add, 2)

        # Lanyard accessories
        surcharge = 0.0
        lanyard_note = ""
        name_acc = dict(accessories)
        if product_key.startswith("lanyard_"):
            acc_cfg = product.get("accessories") or {}
            if accessories.get("second_carabiner"):
                add = float(acc_cfg.get("second_carabiner") or 6)
                surcharge += add
                lanyard_note = (
                    f"З одним карабіном {format_money(unit_price)} грн/шт, "
                    f"додатковий звичайний карабін +{format_money(add)} грн/шт."
                )
            if accessories.get("fastex"):
                surcharge += float(acc_cfg.get("fastex") or 12)
            if accessories.get("safety_break"):
                surcharge += float(acc_cfg.get("safety_break") or 12)
            if accessories.get("sewn_retractor"):
                add = acc_cfg.get("sewn_retractor")
                if add is None:
                    return {
                        "success": False,
                        "error": "Пришивний ретрактор ставимо лише на стрічку 20 мм.",
                    }
                surcharge += float(add)
            unit_price = round(unit_price + surcharge, 2)

        if urgent:
            unit_price = round(unit_price * 2, 2)

        total = round(unit_price * quantity, 2)
        base_total = total

        # Доплати за кілька макетів / розмірів у тиражі (додаються до загальної суми)
        extras_total = 0.0
        extras_notes: list[str] = []
        if lanyard_note:
            extras_notes.append(lanyard_note)
        layout_count = self.detect_layout_count(blob)
        if layout_count > 1:
            if product_key.startswith("swim_cap"):
                per_layout = 300.0
            elif product_key.startswith(
                ("silicone_bracelet", "eco_cup", "reflective", "slap_", "rfid")
            ):
                per_layout = 200.0
            else:
                per_layout = 0.0
            if per_layout:
                add = per_layout * (layout_count - 1)
                extras_total += add
                extras_notes.append(
                    f"{layout_count} різні макети у тиражі: +{format_money(add)} грн "
                    f"(+{format_money(per_layout)} грн за кожен наступний макет)."
                )
        if product_key.startswith("silicone_bracelet"):
            size_count = self.detect_size_variants_count(blob)
            if size_count > 1:
                add = 200.0 * (size_count - 1)
                extras_total += add
                extras_notes.append(
                    f"{size_count} різні розміри у тиражі: +{format_money(add)} грн "
                    "(+200 грн за кожен наступний розмір)."
                )
        if extras_total:
            total = round(total + extras_total, 2)
        prod_days = product["production_days"]["urgent" if urgent else "standard"]
        display = self._display_name(product, product_key, name_acc)
        if product_key.startswith("slap_") and colors > 1:
            display = re.sub(r"1 колір", f"{colors} кольор" + ("и" if colors < 5 else "ів"), display)
            display = display.replace("з друком 1 колір", f"з друком {colors} кольор" + ("и" if colors < 5 else "ів"))
            if f"{colors} кольор" not in display:
                display = f"{product['name'].split(' з друком')[0]} з друком {colors} кольор" + (
                    "и" if colors < 5 else "ів"
                )
        if segmented is not None:
            tie = segmented["tie"]
            seg_word = (
                "сегментний браслет, друк без прив'язки до сегмента"
                if tie == 0
                else f"сегментний браслет, друк з прив'язкою до {'двох сегментів' if tie >= 2 else 'одного сегмента'}"
            )
            display = f"{display} ({seg_word})"
            if quantity < 100:
                extras_notes.append("Сегментні браслети виготовляємо від 100 шт.")

        payload = {
            "success": True,
            "product_key": product_key,
            "product_name": display,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total,
            "currency": "UAH",
            "production_days": prod_days,
            "production_note": product.get("production_note") or "",
            "urgent": urgent,
            "branding": product.get("branding", ""),
            "print_variant": print_variant,
            "print_colors": colors,
            "size_mm": size_mm or product.get("width_mm"),
            "accessories": name_acc,
            "extras_note": " ".join(extras_notes) if extras_notes else "",
            "base_total": base_total,
            "extras_total": round(extras_total, 2),
        }
        if urgent and not payload["production_note"]:
            payload["production_note"] = (
                "Термінове виготовлення (×2 до прайсу) — після підтвердження завантаження виробництва менеджером."
            )
        payload["message"] = format_quote_message(payload, with_confirm=False)
        return payload

    def calculate_multi_variants(
        self,
        product_type: str,
        quantity: int,
        size_mm: str | None = None,
        text: str = "",
    ) -> dict | None:
        """Якщо в запиті кілька варіантів друку — порахувати всі."""
        lower = f"{product_type or ''} {text or ''}".lower()
        variants: list[str] = []

        # swim: 1+1 and 4+4 etc. — не плутати «1» з «10 шт»
        found_caps = re.findall(r"([1-4])\s*\+\s*\1", lower)
        if any(w in lower for w in ("шапоч", "плаванн")) and found_caps:
            for n in found_caps:
                variants.append(f"{n}+{n}")
        elif any(w in lower for w in ("шапоч", "плаванн")):
            wants_1p1 = bool(
                re.search(r"\b1\s*\+\s*1\b|одноколір|1\s*колір", lower)
            )
            wants_4p4 = bool(re.search(r"\b4\s*\+\s*4\b|4\s*кольор", lower))
            if wants_1p1 and wants_4p4:
                variants = ["1+1", "4+4"]
            elif wants_4p4:
                variants = ["4+4"]
            elif wants_1p1:
                variants = ["1+1"]

        # cups 350 and 450, and/or 1 color and 4 color
        if any(w in lower for w in ("чашк", "екочаш", "еко-чаш")):
            sizes = []
            if "350" in lower:
                sizes.append("350")
            if "450" in lower:
                sizes.append("450")
            if not sizes:
                sizes = ["350"]
            wants_1 = "1 колір" in lower or "одноколір" in lower or "1 цвет" in lower
            wants_4 = "4 кольор" in lower or "кольоров" in lower
            if wants_4 and not wants_1:
                return {
                    "success": False,
                    "needs_manager": True,
                    "error": (
                        "Кольоровий друк на еко-чашці прораховуємо лише за макетом у векторі. "
                        "Надішліть макет (AI, EPS, SVG або PDF) — підключу менеджера."
                    ),
                }
            quotes = []
            for s in sizes:
                if wants_1 or not wants_4:
                    q = self.calculate(
                        f"еко-чашка {s} мл",
                        quantity,
                        text=f"еко-чашка {s} мл 1 колір",
                        print_variant="1color",
                    )
                    if q.get("success"):
                        quotes.append(q)
            if wants_4:
                return {
                    "success": True,
                    "needs_manager": True,
                    "multi_quotes": quotes,
                    "manager_note": (
                        "Для кольорового / 4-колірного друку на чашці потрібен векторний макет — "
                        "без нього прорахувати неможливо. Надішліть макет, підключу менеджера."
                    ),
                    "product_name": quotes[0]["product_name"] if quotes else "Еко-чашка",
                    "quantity": quantity,
                    "unit_price": quotes[0]["unit_price"] if quotes else 0,
                    "total_amount": quotes[0]["total_amount"] if quotes else 0,
                    "production_days": quotes[0]["production_days"] if quotes else 10,
                    "production_note": quotes[0].get("production_note", "") if quotes else "",
                }
            if len(quotes) > 1:
                out = dict(quotes[0])
                out["multi_quotes"] = quotes
                out["message"] = format_quote_message(out, with_confirm=False)
                return out

        # reflective / tyvek / slap: 1 color AND color / кілька кольорів друку
        wants_1 = bool(re.search(r"1\s*колір|одноколір|одним\s+кольор", lower))
        wants_color = bool(
            re.search(
                r"кольоров|повноколір|багатоколір|кілька\s+кольор|4\s*кольор|декілька\s+кольор",
                lower,
            )
        )
        slap_colors = self.detect_print_color_list(lower) if (
            "slap" in lower or "слеп" in lower
        ) else []
        fam = None
        if any(w in lower for w in ("світловідб", "відбивн", "рефлект", "reflective")):
            fam = "reflective"
        elif any(w in lower for w in ("tyvek", "контрол")):
            fam = "tyvek"
        elif "slap" in lower or "слеп" in lower:
            fam = "slap"

        if fam == "slap" and len(slap_colors) >= 2:
            quotes = []
            for c in slap_colors:
                q = self.calculate(
                    product_type, quantity, size_mm, text=text, print_colors=c
                )
                if q.get("success"):
                    quotes.append(q)
            if len(quotes) >= 2:
                out = dict(quotes[0])
                out["multi_quotes"] = quotes
                out["message"] = format_quote_message(out, with_confirm=False)
                return out
        if fam and wants_1 and wants_color:
            quotes = []
            if fam == "slap":
                q1 = self.calculate(product_type, quantity, size_mm, text=text, print_colors=1)
                colors = self.detect_print_colors(lower) or 4
                q2 = self.calculate(product_type, quantity, size_mm, text=text, print_colors=colors)
                if q1.get("success"):
                    quotes.append(q1)
                if q2.get("success"):
                    quotes.append(q2)
            else:
                q1 = self.calculate(
                    product_type, quantity, size_mm, text=text, print_variant="1color"
                )
                q2 = self.calculate(
                    product_type, quantity, size_mm, text=text, print_variant="color"
                )
                if q1.get("success"):
                    quotes.append(q1)
                if q2.get("success"):
                    quotes.append(q2)
            if len(quotes) >= 2:
                out = dict(quotes[0])
                out["multi_quotes"] = quotes
                out["message"] = format_quote_message(out, with_confirm=False)
                return out

        if variants:
            quotes = []
            for v in variants:
                q = self.calculate(product_type, quantity, size_mm, text=text, print_variant=v)
                if q.get("success"):
                    quotes.append(q)
            if len(quotes) >= 2:
                out = dict(quotes[0])
                out["multi_quotes"] = quotes
                out["message"] = format_quote_message(out, with_confirm=False)
                return out
            if len(quotes) == 1:
                return quotes[0]
        return None
