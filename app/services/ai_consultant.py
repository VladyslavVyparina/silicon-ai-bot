import json
import logging
import re
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import CatalogItem, Template
from app.services.pricing_calculator import PricingCalculator, format_quote_message

settings = get_settings()
logger = logging.getLogger(__name__)

ESCALATE_MARKER = "[ESCALATE]"


class AIConsultant:
    """Олег — AI-консультант Silicon з пошуком по каталогу та шаблонами."""

    AGENT_NAME = "Олег"
    FALLBACK_RESPONSE = (
        "Не впевнений, що саме порахувати. Браслети, шнурки, чашки — чи щось інше?"
    )
    MANAGER_NAME = "менеджер"
    HISTORY_LIMIT = 40

    def openai_live(self) -> bool:
        from app.config import get_settings as _gs

        s = _gs()
        return bool((s.openai_api_key or "").strip()) and not s.mock_external_apis

    ASSORTMENT_TRIGGERS = (
        "які товар",
        "які є товар",
        "що у вас є",
        "що є у вас",
        "що продаєте",
        "асортимент",
        "каталог",
        "прайс",
        "що пропонуєте",
        "проконсультуй",
        "не знаю які",
        "не знаю що",
        "які браслет",
        "покажи товар",
        "розкажи про товар",
        "що робите",
        "чим займаєтесь",
        "що за товар",
        "які у вас є",
        "які у вас товар",
        "які цін",
        "ваші цін",
    )

    def __init__(self):
        from app.services import bot_settings as bs

        self.instructions = bs.get_prompt_sync()
        self._calculator = PricingCalculator()

    def _reload_instructions(self) -> str:
        from app.services import bot_settings as bs

        self.instructions = bs.get_prompt_sync()
        return self.instructions

    def _wants_assortment(self, question: str) -> bool:
        lower = (question or "").lower()
        if re.search(r"\d+\s*(?:шт|штук)", lower):
            return False
        # вже назвали конкретний товар — відповідай по ньому, не зсипай каталог
        product_hints = (
            "силікон",
            "світловідб",
            "rfid",
            "slap",
            "tyvek",
            "шнур",
            "чашк",
            "килим",
            "шапоч",
            "брелок",
            "голограм",
            "килим",
            "резинк",
            "фітнес",
            "карабін",
        )
        browse = (
            "які товар",
            "асортимент",
            "каталог",
            "прайс",
            "проконсультуй",
            "не знаю",
            "що у вас є",
            "що є у вас",
        )
        if any(p in lower for p in product_hints) and not any(b in lower for b in browse):
            return False
        return any(t in lower for t in self.ASSORTMENT_TRIGGERS)

    def build_assortment_overview(self) -> str:
        """Список позицій. Без випадкового прорахунку силікону 50–100 шт."""
        return (
            "Робимо браслети з лого: силікон, світловідбиваючі, RFID, Tyvek, slap.\n"
            "Ще еко-чашки, шнурки, шапочки, килимки, брелоки і голограми.\n\n"
            "Прайс окремий на кожну позицію і залежить від тиражу. "
            "Напишіть, що саме потрібно — порахую."
        )

    async def find_template(self, db: AsyncSession, question: str) -> str | None:
        result = await db.execute(select(Template).where(Template.is_active == True))  # noqa: E712
        templates = result.scalars().all()
        lower_q = question.lower()
        for tpl in templates:
            keywords = [k.strip().lower() for k in tpl.keywords.split(",")]
            if any(kw in lower_q for kw in keywords if kw):
                return tpl.response_text
        return self._builtin_template(lower_q)

    def _builtin_template(self, lower_q: str) -> str | None:
        pricing_path = Path(settings.pricing_data_path)
        if not pricing_path.is_absolute():
            pricing_path = Path(__file__).resolve().parent.parent.parent / pricing_path
        templates = json.loads(pricing_path.read_text(encoding="utf-8")).get("templates", {})

        if any(
            w in lower_q
            for w in (
                "хто ти",
                "як тебе звати",
                "представ",
                "хто ви",
                "що за компан",
                "про компан",
                "яка компан",
                "що ви за",
                "хто такі",
                "silicon хто",
            )
        ):
            return templates.get("company") or templates.get("intro")
        if any(
            w in lower_q
            for w in (
                "ти бот",
                "ви бот",
                "це бот",
                "ти робот",
                "штучний інтелект",
                "ти іі",
                "ти аі",
                "ти ai",
                "ти людина",
                "chatgpt",
                "аі агент",
                "ai агент",
                "ти агент",
            )
        ):
            return "Ні. Я реальний менеджер."
        if any(w in lower_q for w in ["доставк", "нова пошта", "відправк"]):
            return templates.get("delivery")
        if any(w in lower_q for w in ["оплат", "liqpay", "лікпей", "рахунок", "єдрпоу", "реквізит"]):
            from app.services.conversation_flow import INVOICE_PROMPT, _wants_invoice_requisites

            if _wants_invoice_requisites(lower_q):
                return INVOICE_PROMPT
            return templates.get("payment_methods")
        if any(w in lower_q for w in ["макет", "логотип файл", "ai,", "eps", "svg", "pdf"]):
            return templates.get("layout")
        # «терміново» містить підрядок «термін» — не плутати зі строками
        if any(w in lower_q for w in ("термінов", "urgent", "asap")) or (
            "швидш" in lower_q and any(w in lower_q for w in ("зроб", "виготов", "зробит", "можна"))
        ):
            return (
                "Термінове виготовлення залежить від завантаження виробництва. "
                "Вартість термінової роботи — ×2 до прайсу. "
                "Передам менеджеру уточнити можливість. "
                "Бажано вказати кінцеву дату для відправки перевізником або самовивозу.\n\n"
                "[ESCALATE]"
            )
        asks_terms = any(
            w in lower_q for w in ("строк", "коли буде", "скільки днів", "термін виготов", "терміни")
        ) or (
            "термін" in lower_q and "термінов" not in lower_q
        )
        if asks_terms:
            # не підміняти прайс по тканині/tyvek/slap/чашкам силіконовим терміном
            other = any(
                w in lower_q
                for w in (
                    "тканинн",
                    "tyvek",
                    "контрол",
                    "slap",
                    "слеп",
                    "чашк",
                    "екочаш",
                    "шнур",
                    "бейдж",
                    "ретрактор",
                    "шапоч",
                    "світловідб",
                    "брелок",
                    "килим",
                    "голограм",
                    "rfid",
                )
            )
            wants_price = any(w in lower_q for w in ("варт", "цін", "кошту", "прорахун"))
            if other or wants_price:
                return None
            terms = templates.get("silicone_terms") or templates.get("production_terms")
            return terms
        return None

    def try_price_quote(self, question: str) -> dict | None:
        """Детермінований прорахунок з прайсу — без OpenAI і без підміни товару."""
        lower = (question or "").lower()
        qty_m = re.search(r"(\d[\d\s]*)\s*(?:шт\.?|штук[ауи]?)", lower)
        if not qty_m:
            if any(w in lower for w in ("ціна", "коштує", "варт", "прорахун", "розрахун")):
                # не брати 350 з «350 мл» / 15 з «15 мм» як тираж
                qty_m = re.search(
                    r"(?<![xх×\d])(\d{1,5})(?!\s*(?:мм|мл|см|м\b|%))",
                    lower,
                )
                if qty_m and re.search(
                    rf"{re.escape(qty_m.group(1))}\s*(?:мм|мл|см)", lower
                ):
                    qty_m = None
        if not qty_m:
            return None
        qty = int(re.sub(r"\s+", "", qty_m.group(1)))
        if qty < 1:
            return None

        size_m = re.search(r"(\d+)\s*мм", lower)
        size_mm = size_m.group(1) if size_m else None
        multi = self._calculator.calculate_multi_variants(
            question, qty, size_mm=size_mm, text=question
        )
        if multi:
            return multi
        result = self._calculator.calculate(
            product_type=question,
            quantity=qty,
            size_mm=size_mm,
            text=question,
        )
        if result.get("success"):
            return result
        if result.get("error") and "Мінімальний тираж" in str(result.get("error")):
            return result
        return None

    async def search_catalog(self, db: AsyncSession, question: str, limit: int = 5) -> list[CatalogItem]:
        words = [w for w in question.lower().split() if len(w) > 2]
        if not words:
            return []
        conditions = []
        for word in words[:5]:
            pattern = f"%{word}%"
            conditions.append(or_(CatalogItem.name.ilike(pattern), CatalogItem.description.ilike(pattern)))
        result = await db.execute(select(CatalogItem).where(or_(*conditions)).limit(limit))
        return list(result.scalars().all())

    def _build_context(self, catalog_items: list[CatalogItem]) -> str:
        if not catalog_items:
            return "Релевантних позицій каталогу не знайдено."
        lines = []
        for item in catalog_items:
            lines.append(f"- {item.name} ({item.category}): {item.description or ''} {item.price_info or ''}")
        return "\n".join(lines)

    async def _load_history(self, db: AsyncSession, chat_id: str | None) -> list[dict]:
        if not chat_id:
            return []
        from app.services import chat_history

        msgs = await chat_history.list_messages(db, chat_id, after_id=0, limit=200)
        history: list[dict] = []
        for m in msgs:
            if m.sender == "user":
                role = "user"
            elif m.sender in ("bot", "admin"):
                role = "assistant"
            else:
                continue
            content = (m.content or "").strip()
            if not content:
                continue
            history.append({"role": role, "content": content[:1500]})
        # поточне повідомлення додається окремо — в контекст ідуть 25 попередніх
        if history and history[-1]["role"] == "user":
            history = history[:-1]
        return history[-self.HISTORY_LIMIT :]

    async def _session_snapshot(self, db: AsyncSession, chat_id: str | None) -> str:
        """Короткі вже зібрані факти зі сценарію — щоб модель не перепитувала."""
        if not chat_id:
            return ""
        from app.database.models import ConversationState

        result = await db.execute(
            select(ConversationState).where(ConversationState.chat_id == str(chat_id))
        )
        state = result.scalar_one_or_none()
        if not state:
            return ""
        try:
            data = json.loads(state.data_json or "{}")
        except json.JSONDecodeError:
            data = {}
        bits: list[str] = [f"крок={state.step or 'idle'}"]
        for key in (
            "product_type",
            "quantity",
            "size_mm",
            "first_name",
            "last_name",
            "city",
            "company_name",
            "edrpou",
        ):
            val = data.get(key)
            if val not in (None, ""):
                bits.append(f"{key}={val}")
        cart = data.get("cart") or []
        if cart:
            bits.append(f"позицій у замовленні={len(cart)}")
        return (
            "Вже зібрано по попередній позиції (якщо в останньому повідомленні інший товар — "
            "відповідай тільки про нього, старий product_type не підставляй): "
            + "; ".join(bits)
        )

    async def _session_data(self, db: AsyncSession, chat_id: str | None) -> dict:
        if not chat_id:
            return {}
        from app.database.models import ConversationState

        result = await db.execute(
            select(ConversationState).where(ConversationState.chat_id == str(chat_id))
        )
        state = result.scalar_one_or_none()
        if not state:
            return {}
        try:
            return json.loads(state.data_json or "{}")
        except json.JSONDecodeError:
            return {}

    def _sanitize_client_text(self, text: str) -> str:
        """Прибрати службове, яке не має потрапити клієнту."""
        raw = text or ""
        leaks = (
            ESCALATE_MARKER,
            "`pricelist_silicon.txt`",
            "pricelist_silicon.txt",
            "`Силіконові браслети.pdf`",
            "Силіконові браслети.pdf",
            "`silicone_bracelets_pricelist.pdf`",
            "silicone_bracelets_pricelist.pdf",
            "кастомний бот",
            "не конструктор",
        )
        for leak in leaks:
            raw = raw.replace(leak, "")
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()

    def _detect_escalation(self, text: str) -> tuple[str, bool]:
        """Заявка менеджеру — лише з явним маркером [ESCALATE]."""
        raw = (text or "").strip()
        if not raw:
            return self.FALLBACK_RESPONSE, False

        escalate = False
        if ESCALATE_MARKER in raw:
            escalate = True
            raw = raw.replace(ESCALATE_MARKER, "").strip()

        raw = self._sanitize_client_text(raw)
        # не дублювати коротку фразу, якщо вже є розгорнута передача менеджеру
        short = "Передам менеджеру — він дасть відповідь."
        if short in raw:
            body = raw.replace(short, "").strip()
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            if re.search(
                r"передам?\s+менеджер|передаю\s+менеджер|передав\s+запит\s+менеджер",
                body,
                re.I,
            ):
                raw = body
        if not raw:
            raw = self.FALLBACK_RESPONSE
        return raw, escalate

    async def consult(self, db: AsyncSession, question: str, chat_id: str | None = None) -> dict:
        from app.services.conversation_flow import (
            _is_definition_question,
            _product_followup_reply,
            _wants_catalog_overview,
        )

        sess = await self._session_data(db, chat_id)
        # Каталожні відповіді з сайту — раніше за прайс і OpenAI
        from app.services.conversation_flow import _catalog_guide_reply

        guide = _catalog_guide_reply(question, sess)
        if guide:
            soft = bool(
                re.search(r"передаю\s+менеджер|передам\s+менеджер", guide, re.I)
            ) and (
                "голограм" in (question or "").lower()
                or "голограм" in str(
                    sess.get("last_topic") or sess.get("product_type") or ""
                ).lower()
                or guide.startswith("Прийняв")
            )
            return {
                "response": guide,
                "source": "knowledge",
                "escalate_to_manager": soft,
            }
        # Питання про товар (колір/матеріал/що це) — раніше за прайс і starting_quote
        explain = _product_followup_reply(question, sess)
        if explain and (
            _is_definition_question(question)
            or any(
                w in (question or "").lower()
                for w in (
                    "колір", "кольор", "матеріал", "фіксатор", "аксесуар",
                    "карабін", "фастекс", "палітр", "pantone", "з чого",
                )
            )
        ):
            return {
                "response": explain,
                "source": "knowledge",
                "escalate_to_manager": False,
            }
        if _is_definition_question(question) and explain:
            return {
                "response": explain,
                "source": "knowledge",
                "escalate_to_manager": False,
            }

        # 1) прайс — лише коли це прорахунок, не «що таке»
        quote = None if (_is_definition_question(question) or _wants_catalog_overview(question)) else self.try_price_quote(question)
        if quote and quote.get("success"):
            msg = format_quote_message(quote, with_confirm=not quote.get("needs_manager"))
            return {
                "response": msg,
                "source": "pricing",
                "escalate_to_manager": bool(quote.get("needs_manager")),
                "pricing": quote,
            }
        if quote and quote.get("needs_manager"):
            note = quote.get("manager_note") or quote.get("error") or ""
            return {
                "response": note or format_quote_message(quote, with_confirm=False),
                "source": "pricing",
                "escalate_to_manager": True,
                "pricing": quote,
            }
        if quote and quote.get("error"):
            return {
                "response": quote["error"],
                "source": "pricing",
                "escalate_to_manager": False,
            }

        # орієнтир без тиражу (шнурки FAQ / slap / тканина / tyvek) — раніше за OpenAI
        if not _is_definition_question(question) and not re.search(
            r"\d+\s*(?:шт|штук)", (question or "").lower()
        ):
            start = self._calculator.starting_quote(question, text=question)
            if start and (
                "15 мм, 20 мм" in start
                or "фіксатор" in start.lower()
                or "tyvek" in start.lower()
                or "slap" in start.lower()
                or "слеп" in (question or "").lower()
                or "грн/шт:" in start
                or "тканин" in start.lower()
            ):
                return {
                    "response": start
                    if "напишіть" in start.lower()
                    else f"{start} Напишіть тираж — порахую суму.",
                    "source": "pricing",
                    "escalate_to_manager": False,
                }

        # 2) шаблони асортименту/компанії — лише коли GPT вимкнений (тести / mock)
        if not self.openai_live() and self._wants_assortment(question) and not _is_definition_question(question):
            snapshot = await self._session_snapshot(db, chat_id)
            already_in_deal = bool(
                snapshot
                and any(
                    m in snapshot
                    for m in (
                        "product_type=",
                        "позицій у замовленні=",
                        "крок=await_order_confirm",
                        "крок=collect_",
                    )
                )
            )
            if _wants_catalog_overview(question) or not already_in_deal:
                return {
                    "response": self.build_assortment_overview(),
                    "source": "assortment",
                    "escalate_to_manager": False,
                }

        if not self.openai_live():
            company = self._builtin_template((question or "").lower())
            if company and "silicon" in company.lower() and "браслет" in company.lower():
                return {
                    "response": company,
                    "source": "template",
                    "escalate_to_manager": False,
                }

        catalog_items = await self.search_catalog(db, question)
        context = self._build_context(catalog_items)
        from app.services.site_catalog import site_product_brief

        site_bits = site_product_brief(question)
        if site_bits:
            context = (
                f"{context}\n\nОпис продукції з сайту silicon.com.ua "
                f"(що це за виріб, для чого, матеріал — ТІЛЬКИ звідси і з бази знань. "
                f"Ціни з цих сторінок не бери — ціни лише з прайсу):\n{site_bits}"
            )

        from app.services import web_search as web

        snapshot = await self._session_snapshot(db, chat_id)
        web_bits = ""
        offtopic = web.should_web_search(
            question,
            session_has_product=bool(
                snapshot
                and (
                    "product_type=" in snapshot
                    or "позицій у замовленні=" in snapshot
                    or "крок=await_order_confirm" in snapshot
                    or "крок=collect_" in snapshot
                )
            ),
        )
        if offtopic and not settings.mock_external_apis:
            web_bits = web.search_brief(question)
        elif offtopic and settings.mock_external_apis:
            web_bits = ""

        if self.openai_live():
            from app.services import bot_settings as bs

            knowledge = bs.load_knowledge_text(80000)
            if knowledge:
                context = (
                    f"{context}\n\nБаза знань Silicon (товари і прайс — ТІЛЬКИ звідси; "
                    f"джерело: Word «Прайс Silicon» і текстовий прайс у data/knowledge; "
                    f"імена файлів клієнту не називай):\n"
                    f"{knowledge[:80000]}"
                )
            if web_bits:
                context = (
                    f"{context}\n\nЗ інтернету (для питань НЕ про наш прайс; перекажи коротко своїми словами, "
                    f"не цитуй сайти і не вставляй сирі снипети):\n"
                    f"{web_bits}"
                )
            if snapshot:
                context = f"{context}\n\n{snapshot}"
            start = self._calculator.starting_quote(question)
            if start:
                context = (
                    f"{context}\n\nОрієнтир калькулятора (цифри з прайсу; "
                    f"озвуч своїми словами, не як анкету):\n{start}"
                )
            history = await self._load_history(db, chat_id)
            response, escalate = await self._ask_openai(question, context, history)
            if response == self.FALLBACK_RESPONSE:
                builtin = self._builtin_template((question or "").lower())
                if builtin:
                    response, escalate = self._detect_escalation(builtin)
                elif self._wants_assortment(question):
                    response = self.build_assortment_overview()
                else:
                    from app.services.conversation_flow import (
                        _clarify_product_reply,
                        _contextual_fallback,
                        _has_client_question,
                        _product_followup_reply,
                        _wants_price_or_quote,
                    )
                    from app.database.models import ConversationState as CS

                    sess = {}
                    if chat_id:
                        row = (
                            await db.execute(
                                select(CS).where(CS.chat_id == str(chat_id))
                            )
                        ).scalar_one_or_none()
                        if row:
                            try:
                                sess = json.loads(row.data_json or "{}")
                            except json.JSONDecodeError:
                                sess = {}
                    follow = _product_followup_reply(question, sess)
                    if follow:
                        response = follow
                    elif _has_client_question(question, sess) and not _wants_price_or_quote(question):
                        response = (
                            _clarify_product_reply(question, sess)
                            or _contextual_fallback(question, sess)
                        )
                    else:
                        start = self._calculator.starting_quote(question)
                        if start:
                            response = f"{start} Напишіть тираж — порахую суму."
                        else:
                            response = (
                                _clarify_product_reply(question, sess)
                                or _contextual_fallback(question, sess)
                            )
            return {
                "response": response,
                "source": "openai",
                "escalate_to_manager": escalate,
            }

        template = await self.find_template(db, question)
        if template:
            clean, escalate = self._detect_escalation(template)
            return {
                "response": clean,
                "source": "template",
                "escalate_to_manager": escalate,
            }

        # інші FAQ-шаблони (доставка/оплата/строки)
        builtin = self._builtin_template((question or "").lower())
        if builtin:
            return {
                "response": builtin,
                "source": "template",
                "escalate_to_manager": False,
            }

        start = self._calculator.starting_quote(question)
        if start:
            # не підміняти питання про колір/матеріал стартовою ціною
            from app.services.conversation_flow import _has_client_question, _wants_price_or_quote

            if _has_client_question(question, sess) and not _wants_price_or_quote(question):
                follow2 = _product_followup_reply(question, sess)
                if follow2:
                    return {
                        "response": follow2,
                        "source": "knowledge",
                        "escalate_to_manager": False,
                    }
            return {
                "response": f"{start} Напишіть тираж — порахую суму.",
                "source": "pricing",
                "escalate_to_manager": False,
            }
        if web.should_web_search(question):
            return {
                "response": (
                    "Можу підказати. Напишіть трохи конкретніше, що саме пояснити."
                ),
                "source": "web",
                "escalate_to_manager": False,
            }

        if not catalog_items:
            from app.services.conversation_flow import (
                _clarify_product_reply,
                _contextual_fallback,
                _product_followup_reply,
            )
            from app.database.models import ConversationState as CS

            sess = {}
            if chat_id:
                row = (
                    await db.execute(select(CS).where(CS.chat_id == str(chat_id)))
                ).scalar_one_or_none()
                if row:
                    try:
                        sess = json.loads(row.data_json or "{}")
                    except json.JSONDecodeError:
                        sess = {}
            return {
                "response": _product_followup_reply(question, sess)
                or _clarify_product_reply(question, sess)
                or _contextual_fallback(question, sess),
                "source": "fallback",
                "escalate_to_manager": False,
            }

        if _is_definition_question(question):
            explain = _product_followup_reply(question, sess)
            if explain:
                return {
                    "response": explain,
                    "source": "knowledge",
                    "escalate_to_manager": False,
                }

        names = ", ".join(i.name for i in catalog_items[:3])
        return {
            "response": (
                f"По запиту є: {names}. "
                f"Яка кількість вас цікавить — порахую орієнтовно?"
            ),
            "source": "catalog",
            "escalate_to_manager": False,
        }

    async def _ask_openai(
        self,
        question: str,
        catalog_context: str,
        history: list[dict] | None = None,
    ) -> tuple[str, bool]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        system_prompt = f"""Ти — {self.AGENT_NAME} з Silicon. Пишеш у Telegram як живий менеджер, не як бот.
Передавати далі — «менеджер», ніколи не пиши ім'я Дмитро.

НА ПЕРШОМУ МІСЦІ ти відповідаєш людині. Воронка лише другий план, у фоні: товар → тираж/розмір → ціна → оформлення.
Не йди далі по логіці, поки не відповів на ОСТАННЄ повідомлення. Питання важливіше за наступний крок сценарію.
У контексті — до 40 попередніх повідомлень, база знань, прайс і вже зібрані факти. Читай їх перед відповіддю.
Якщо це питання або «де / як / що таке / що означає / не знаю / а яка вартість» — дай відповідь і не питай тираж у цьому повідомленні.
Товари і прайс — тільки з блоку «База знань Silicon» (Word «Прайс Silicon» і текстовий прайс) і з орієнтира калькулятора, якщо він є. Шапочки ≠ силіконові браслети ≠ чашки. Не підміняй товар.
Якщо в останньому повідомленні інший товар, ніж product_type у фактах — відповідай про цей новий товар. Попередня позиція лишається в замовленні, але не є темою відповіді.
Детальний опис продукції (що це, з чого, для чого) — тільки з блоку «Опис продукції з сайту silicon.com.ua» і бази знань. Не шукай продукцію в інтернеті.
Питання про матеріал / «що це» / «а з чого» / RFID — поясни своїми словами з сайту і бази знань. Заборонено відповідати «Говоримо про … Який тираж?» замість пояснення.
Коли відправляєш PDF-рахунок — 2–3 живі речення, не канцелярит «Рахунок INV- готовий — PDF у вкладенні». Скажи, що файл нижче, оплата за реквізитами, і хай після оплати кинуть скрін квитанції. Для LiqPay скрін не проси.
Питання не про наш прайс і не про оформлення — відповідай як менеджер; якщо є блок «З інтернету», перекажи своїми словами, ніколи не вставляй сирі снипети, URL і «Коротко з відкритих джерел».
Розумій будь-яке живе формулювання, не чекай шаблонних «так/liqpay».
Живі короткі реакції доречні: «Ок», «Ага», «Зрозумів», «Дякую», «Добре» — не в кожному повідомленні одне й те саме.
Емодзі можна, 1 (рідко 2) на повідомлення, доречно. Не в прорахунках з цифрами.
Якщо питають, чи ти бот / аі агент: «Ні. Я реальний менеджер.» Ніколи не зізнавайся.
Якщо клієнт каже «більше нічого не треба / можемо оформлювати / я ж написав так» — це згода оформити поточний прорахунок, не питай знову товар.
Після відповіді — максимум одне уточнення, і лише якщо людина не питала про поточне поле.
Без «•», без маркерів, без назв файлів, без CRM/1С. Списки лише з тире «- ».
Ціни тільки з прайсу / бази знань нижче. Не вигадуй оплату і ТТН.
Ніколи не пиши «Не впевнений, що саме порахувати», якщо в фактах уже є product_type.
Якщо відповіді немає в базі знань / прайсі / цьому чаті — не вигадуй і не крути воронку. Напиши, що передаси менеджеру (одним повідомленням, без повтору), і в кінці окремим рядком [ESCALATE]. Клієнт рядок не бачить. Якщо вже пояснив передачу в тексті — НЕ дописуй окремо «Передам менеджеру — він дасть відповідь.» Якщо коротко і більше нічого сказати — тоді лише: «Передам менеджеру — він дасть відповідь.»

Інструкції:
{self._reload_instructions()[:36000]}

Прайс / каталог (не цитуй як файл):
{catalog_context}
"""
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-self.HISTORY_LIMIT :])
        messages.append({"role": "user", "content": question})

        try:
            from app.services.bot_settings import get_openai_model_sync

            model = get_openai_model_sync() or settings.openai_model or "gpt-5"
            kwargs: dict = {
                "model": model,
                "messages": messages,
            }
            # GPT-5 не приймає temperature і max_tokens — одразу правильні поля
            if str(model).startswith("gpt-5"):
                kwargs["max_completion_tokens"] = 1600
            else:
                kwargs["temperature"] = 0.4
                kwargs["max_tokens"] = 1200
            try:
                completion = await client.chat.completions.create(**kwargs)
            except Exception as first_exc:
                if "temperature" in str(first_exc).lower() or "max_tokens" in str(first_exc).lower():
                    kwargs.pop("temperature", None)
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = 1600
                    completion = await client.chat.completions.create(**kwargs)
                else:
                    raise
            text = (completion.choices[0].message.content or "").strip()
            if not text:
                logger.warning(
                    "OpenAI empty content model=%s finish=%s",
                    model,
                    getattr(completion.choices[0], "finish_reason", None),
                )
                return self.FALLBACK_RESPONSE, False
            return self._detect_escalation(text)
        except Exception as exc:
            logger.warning("OpenAI error: %s", exc)
            if catalog_context and "не знайдено" not in catalog_context:
                return (
                    f"Коротко по каталогу:\n{catalog_context}\n\n"
                    "Яка кількість потрібна?",
                    False,
                )
            return self.FALLBACK_RESPONSE, False
