"""
Мониторинг заявок на Kwork.ru + LLM-классификация + генерация чернового отклика.

Не LLM-tool модуль (в отличие от modules/scheduler, modules/calendar и т.д.) —
функции отсюда вызываются напрямую фоновым watcher-ом (_kwork_watcher в bot.py)
и обработчиком inline-кнопок (on_menu в bot.py), а не моделью через
ModuleOrchestrator. См. план /root/.claude/plans/tingly-munching-kurzweil.md.

Публикация отклика — единственное действие, требующее подтверждения человека
(нажатие кнопки в Telegram); весь путь до этого полностью автономен.
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime, timezone

import httpx
import anthropic

logger = logging.getLogger(__name__)

# ── конфиг ────────────────────────────────────────────────────────────────

KWORK_SESSION_FILE = "/app/kwork_session.json"
KWORK_SEEN_FILE = "/app/kwork_seen.json"
KWORK_LOG_FILE = "/opt/claude-bot/logs/kwork_orders.jsonl"

MODEL = "claude-sonnet-4-6"  # онли через oneprovider.dev; sonnet-5 там не поддерживается

PROJECTS_URL = "https://kwork.ru/projects"
WANT_URL = "https://kwork.ru/projects/{}/view"
# Форма отклика — отдельная страница (июль 2026: кнопка на странице заявки
# называется «Предложить услугу» и ведёт сюда; поля: description, цена, срок).
NEW_OFFER_URL = "https://kwork.ru/new_offer?project={}"
KWORK_DRAFTS_FILE = "/opt/claude-bot/logs/kwork_drafts.json"

# id категорий раздела "Разработка и IT", релевантных компетенциям Давида.
# Список сознательно узкий (не весь раздел "Разработка и IT" = 11) — чистый
# фронтенд/вёрстка/дизайн исключены категорией целиком, а не только классификатором,
# чтобы не жечь LLM-вызовы на заведомо нерелевантный поток. Правь по опыту.
KWORK_CATEGORIES = {
    41:  "Скрипты, боты и mini apps",
    84:  "Персональный помощник",
    113: "Базы данных и клиентов",
    255: "Сервера и хостинг",
    80:  "Десктоп программирование",
}

PAGES_PER_TICK = 6          # сколько страниц ленты /projects просматривать за тик
MAX_CLASSIFY_PER_TICK = 30  # защита от разового веерного залпа по LLM (напр. при первом запуске)

# ── промпт-классификатор (от Давида, без изменений) ────────────────────────

CLASSIFY_SYSTEM_PROMPT = """Ты — классификатор релевантности заказов для фрилансера Давида на бирже Kwork.
Твоя задача: получить текст заказа и решить, стоит ли Давиду откликаться,
и если да — под какую его компетенцию заточить отклик.

КОМПЕТЕНЦИИ ДАВИДА (от ядра к периферии):

Ядро — ИИ и автоматизация (основной источник дохода сейчас):
- Telegram-боты с ИИ: агентный цикл, инструменты, маршрутизация между моделями
- Расшифровка голоса/аудио (Whisper), обработка речи
- Связка нейросетей с таблицами/CRM, структурирование неструктурированных данных
- Парсинг сайтов и каталогов, сбор и очистка данных
- Интеграции с Google Calendar и другими API
- Автоматизация любой рутины через ИИ — оформление заявок, ответы клиентам,
  заполнение документов, перенос данных между системами

Смежное — техническая инфраструктура:
- Развёртывание на VPS, Docker, деплой-скрипты, мониторинг сервисов
- Базовая кибербезопасность (проводил аудит по методологии OODA)

Периферия — бизнес-бэкграунд (даёт кредит доверия, не основной продукт):
- КИПиА, промышленная автоматизация, техническое оборудование
- Управление сервисным центром — клиентский сервис, операционные процессы
- Брокерство лазерной/плазменной резки металла — работа с ТЗ, чертежами, расчётом заказов
- Общий консалтинг по наведению порядка в процессах малого бизнеса
  (позиционирование "директор-практик")

НЕ его профиль (отклонять, даже если формально пересекается по словам):
- Чистый дизайн/графика без автоматизации
- SMM/копирайтинг без ИИ-компонента
- Frontend/сайты без функции автоматизации внутри
- Заказы, требующие постоянной жёсткой поддержки 24/7 без указанного бюджета
  на сопровождение

ЗАДАЧА: проанализируй текст заказа и верни строго JSON:

{
  "relevant": true/false,
  "confidence": 0-100,
  "matched_skill": "краткое название конкретного навыка из списка выше",
  "tier": "core / смежное / периферия",
  "suggested_kwork_service": "один из 5: телеграм-бот / нейросеть+CRM /
    расшифровка / парсинг / автоматизация — или null, если заказ не
    попадает точно ни в один, но релевантен по периферийному навыку",
  "reasoning": "одно предложение, почему да/нет",
  "red_flags": ["если есть — например: 'просит демпинговую цену',
    'нужен только дизайн без автоматизации', 'нереалистичный срок']
}

Правила принятия решения:
- relevant = true только если confidence >= 65
- Если заказ явно ядро — confidence обычно 85-100
- Если заказ смежный/периферийный, но Давид реально может закрыть его
  быстро и качественно — confidence 65-85
- Если заказ можно решить только с натяжкой (совпадение по одному слову,
  но суть другая) — relevant = false, даже при формальном совпадении
- Не занижай качество из-за низкого бюджета в заказе — бюджет не влияет
  на relevant, только на приоритет ответа
- Явный спам, "срочно нужен кодер за еду", нереалистичные ТЗ — отклонять"""

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "matched_skill": {"type": "string"},
        "tier": {"type": "string", "enum": ["core", "смежное", "периферия"]},
        "suggested_kwork_service": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": ["телеграм-бот", "нейросеть+CRM", "расшифровка", "парсинг", "автоматизация"],
                },
                {"type": "null"},
            ]
        },
        "reasoning": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "relevant", "confidence", "matched_skill", "tier",
        "suggested_kwork_service", "reasoning", "red_flags",
    ],
    "additionalProperties": False,
}

# ── промпт-генератор отклика (база от Давида; ценовая стратегия и продающая
#    структура обновлены 2026-07-03 по его просьбе) ─────────────────────────

GENERATE_SYSTEM_PROMPT = """Ты пишешь первый отклик на заказ на бирже Kwork от лица Давида (бренд
Automatiko.ru, ник MangasarovDR). Твоя единственная задача — написать
текст, который клиент прочитает за 5 секунд и захочет ответить именно
Давиду, а не следующему в списке из 10-20 однотипных откликов.

НА ВХОД ТЫ ПОЛУЧАЕШЬ:
- текст заказа целиком (as is)
- matched_skill, tier, suggested_kwork_service — из классификатора релевантности
- desired_budget — желаемый бюджет клиента («сколько хочу заплатить»), может отсутствовать
- possible_budget — допустимый максимум клиента, может отсутствовать

ПОЗИЦИОНИРОВАНИЕ ДАВИДА:
Директор-практик, который закрывает рутину бизнеса через нейросети и ботов.
Раньше сам управлял бизнесом (сервисный центр, КИПиА, лазерная/плазменная
резка) — поэтому смотрит не на технологию ради технологии, а на то, где
у заказчика утекают деньги и время.

СПРАВОЧНИК ЦЕН (ориентируйся, не гадай с потолка):
- Telegram-бот с ИИ: база 4 800 ₽, срок 4 дня
- Нейросеть + таблицы/CRM: база 4 800 ₽, срок 4 дня
- Расшифровка аудио/голосовых: от 800 ₽ за 1000 знаков, срок 2 дня
- Парсинг и обработка данных: от 3 600 ₽ за 1000 ед., срок 3 дня
- Автоматизация рутины (нестандартное): база 4 800 ₽, срок 5 дней
Если заказ сложнее базового пакета (интеграции, большой объём, кастомная
логика) — дай вилку ("от X до Y ₽, точно скажу после уточнения деталей"),
не занижай и не завышай искусственно.

СТРУКТУРА ОТКЛИКА (4-6 строк, строго; итоговый текст 150-700 знаков —
меньше 150 форма Kwork не примет):

Строка 1 — зацепка. Перефразируй ОДНУ конкретную деталь из заявки клиента
своими словами так, чтобы было видно: ты реально прочитал именно этот
текст, а не скопировал шаблон. Используй его формулировки, его боль, его
цифры, если они есть.

Строка 2 — подход + результат. Одна-две фразы: КАК именно ты это решишь
(конкретный подход, а не список умений) и ЧТО клиент получит на выходе —
в его величинах: сэкономленные часы, обработанные заявки, готовые записи
в таблице. Это должно звучать так, как пишет человек, который уже так
делал. Если matched_skill относится к tier "периферия" (КИПиА, резка,
сервис-центр) — здесь можно естественно вплести одну деталь бэкграунда,
без отдельного представления и без "раньше я управлял бизнесом" в лоб.

Строка 3 — что входит в цену. Полупредложение, которое делает цену
осязаемой и оправдывает её: "под ключ: настройка, запуск на вашем
сервере/аккаунте и неделя правок после сдачи" — адаптируй под задачу.
Саму цену и срок в текст НЕ пиши — они уйдут в отдельные поля формы
Kwork (поля price и term_days твоего ответа), клиент увидит их рядом
с текстом.

Строка 4 — снятие риска. Одна фраза: первый рабочий кусок покажешь через
1-2 дня, дальше клиент решает. Формулируй естественно под задачу, не
шаблонно.

Строка 5 — действие с низким трением. Либо: "Могу показать рабочий пример
прямо сейчас" — если задача близка к тому, что уже готово (боты, расшифровка,
парсинг). Либо: один точный уточняющий вопрос по существу задачи, который
одновременно продвигает диалог и показывает, что ты понимаешь предметную
область глубже среднего исполнителя.

Строка 6 (опционально, только при явных сигналах чувствительности к цене
или явного новичка-заказчика по короткому ТЗ) — "Сейчас беру по цене ниже
рынка, пока набираю первые отзывы на площадке" — подавать как выгоду
заказчику, а не оправдание.

ЦЕНОВАЯ СТРАТЕГИЯ (поле price твоего ответа):
Ориентир — desired_budget (желаемый бюджет клиента), НЕ possible_budget.
- Если desired_budget указан: ставь цену ЧУТЬ ВЫШЕ желаемого — на 5-15%,
  округлив до аккуратного числа (до сотен). Желаемый бюджет почти всегда
  занижен «на попробовать», а цена чуть выше него читается как честная
  оценка трудоёмкости, а не подгонка под кошелёк. Если указан и
  possible_budget — не превышай его.
- Если desired_budget указан, но он ниже половины базовой цены из
  справочника — клиент не в рынке; ставь цену по справочнику, а в строке 3
  текста одним полупредложением обоснуй, из чего она складывается.
- Если desired_budget не указан — работай по справочнику; при указанном
  possible_budget держись в его пределах.

СРОК (поле term_days твоего ответа):
Реалистичный срок в днях, целое от 1 до 30. База: боты и нейросеть+CRM —
4 дня, расшифровка — 2, парсинг — 3, нестандартная автоматизация — 5.
Большой объём или интеграции — увеличивай честно; если в заявке назван
срок — уложись в него, если это реально.

ЖЁСТКИЕ ЗАПРЕТЫ:
- Никогда не начинай с "Здравствуйте", "Добрый день", "Готов выполнить"
- Никогда не пиши "обращайтесь", "буду рад помочь", "с уважением"
- Никогда не перечисляй список возможностей общими словами — только то,
  что относится именно к этому заказу
- Никогда не выдумывай выполненные проекты, которых не было — если
  matched_skill даёт основание сказать "делал похожее" честно, используй
  это, если нет — не придумывай
- Никогда не занижай цену ниже справочника ради "конкурентности" —
  Давиду важна не любая цена, а реалистичная маржа
- Не используй англицизмы и канцелярит
- Не благодари за внимание и не извиняйся

ФОРМАТ ОТВЕТА — строго JSON:
{
  "text": "готовый текст отклика без цены и срока, 150-700 знаков,
           без markdown-разметки — ровно то, что уйдёт в поле «Описание»",
  "price": целое число в рублях по ценовой стратегии выше,
  "term_days": целое число дней от 1 до 30
}"""

# Правило площадки поверх промпта Давида — не убирать вне зависимости от
# формулировок выше: Kwork банит за обмен контактами до найма. Бренд/ник
# в системном промпте — это позиционирование модели, а не инструкция
# упоминать их в тексте отклика; явно это запрещаем.
NO_CONTACTS_RULE = """

---
Жёсткое правило площадки Kwork, важнее всего вышеперечисленного: НИКОГДА не
включай в текст отклика телефон, email, ссылки на мессенджеры (t.me, wa.me,
Telegram-юзернеймы), домены/сайты (включая automatiko.ru) или ник
MangasarovDR — это тоже контактная информация. Kwork запрещает обмен
контактами до найма и банит аккаунт за нарушение. Весь контакт с клиентом —
только через встроенный чат Kwork."""

GENERATE_SYSTEM_PROMPT = GENERATE_SYSTEM_PROMPT + NO_CONTACTS_RULE

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "price": {"type": "integer"},
        "term_days": {"type": "integer"},
    },
    "required": ["text", "price", "term_days"],
    "additionalProperties": False,
}

_CONTACT_PATTERNS = [
    re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'),  # телефон РФ
    re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),                    # email
    re.compile(r'(?<!\w)@[a-zA-Z0-9_]{4,}'),                                         # @username
    re.compile(r't\.me/\S+', re.IGNORECASE),
    re.compile(r'wa\.me/\S+', re.IGNORECASE),
    re.compile(r'\b[a-zA-Z0-9-]+\.(ru|com|net|io|dev)\b', re.IGNORECASE),            # домены/сайты
    re.compile(r'MangasarovDR', re.IGNORECASE),
]


def looks_like_contact_info(text: str) -> bool:
    return any(p.search(text) for p in _CONTACT_PATTERNS)


# ── состояние: seen ids, черновики в памяти ────────────────────────────────

def _load_seen_ids() -> set:
    if not os.path.exists(KWORK_SEEN_FILE):
        return set()
    try:
        with open(KWORK_SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen_ids(ids: set) -> None:
    try:
        with open(KWORK_SEEN_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as e:
        logger.error(f"kwork: seen ids write error: {e}")


def mark_seen(want_id: int) -> None:
    """Помечает заявку обработанной. Вызывать ПОСЛЕ обработки (карточка
    отправлена или заявка отклонена классификатором) — не раньше, иначе
    сбой/рестарт посреди пачки теряет заявки навсегда."""
    seen = _load_seen_ids()
    seen.add(want_id)
    _save_seen_ids(seen)


# want_id -> {"want": dict, "classification": dict, "draft": str,
#             "price": int, "term_days": int,
#             "flagged": bool, "status": "pending"/"sent"/"skipped"/"error"}
# Персистится в KWORK_DRAFTS_FILE, чтобы кнопка «Отправить» в Telegram
# работала и после рестарта бота.

def _load_drafts() -> dict[int, dict]:
    try:
        with open(KWORK_DRAFTS_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def save_drafts() -> None:
    try:
        with open(KWORK_DRAFTS_FILE, "w") as f:
            json.dump({str(k): v for k, v in pending_drafts.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"kwork: drafts write error: {e}")


pending_drafts: dict[int, dict] = _load_drafts()


def _log_event(entry: dict) -> None:
    os.makedirs(os.path.dirname(KWORK_LOG_FILE), exist_ok=True)
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    try:
        with open(KWORK_LOG_FILE, "a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"kwork: log write error: {e}")


# ── мониторинг публичной ленты /projects ────────────────────────────────────

async def fetch_new_wants() -> list[dict]:
    """Новые (ранее не виденные) активные заявки в целевых категориях.
    Публичная лента, авторизация не нужна — см. разведку в чате.

    Заявки здесь НЕ помечаются как seen — это делает watcher через
    mark_seen() после фактической обработки каждой. Поэтому обрезка до
    MAX_CLASSIFY_PER_TICK ничего не теряет: хвост вернётся в следующем тике."""
    seen = _load_seen_ids()
    found = []
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for page in range(1, PAGES_PER_TICK + 1):
            try:
                resp = await client.get(PROJECTS_URL, params={"page": page})
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"kwork: fetch page {page} error: {e}")
                continue
            html = resp.text
            try:
                idx = html.index('"wantsListData":') + len('"wantsListData":')
                data, _ = json.JSONDecoder().raw_decode(html, idx)
                wants = data["pagination"]["data"]
            except Exception as e:
                logger.error(f"kwork: parse page {page} error: {e}")
                continue
            for w in wants:
                wid = w.get("id")
                if wid is None or wid in seen:
                    continue
                if w.get("status") != "active":
                    continue
                try:
                    cat_id = int(w.get("category_id"))
                except (TypeError, ValueError):
                    continue
                if cat_id not in KWORK_CATEGORIES:
                    continue
                found.append(w)
    return found[:MAX_CLASSIFY_PER_TICK]


# ── LLM: классификация и генерация отклика ─────────────────────────────────

def _client(api_key: str) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=api_key, base_url="https://api.oneprovider.dev")


def _first_text(resp) -> str | None:
    block = next((b for b in resp.content if b.type == "text"), None)
    return block.text if block else None


def _parse_json(raw: str) -> dict | None:
    """oneprovider не всегда принуждает к чистому JSON по json_schema —
    модель иногда оборачивает ответ в ```json-fence или добавляет текст
    вокруг. Достаём первый JSON-объект из строки."""
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        idx = raw.index("{")
        obj, _ = json.JSONDecoder().raw_decode(raw, idx)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _llm_json(api_key: str, system: str, user_content: str,
                    schema: dict, effort: str, want_id, tag: str) -> dict | None:
    """Вызов LLM с json_schema через oneprovider + обходы его причуд:
    иногда провайдер игнорирует thinking=disabled и модель сжигает весь
    бюджет токенов на thinking-блок, не выдав текста (stop_reason=max_tokens,
    пустой ответ) — поэтому запас 4096 токенов и один повтор."""
    for attempt in range(2):
        try:
            resp = await _client(api_key).messages.create(
                model=MODEL,
                max_tokens=4096,
                thinking={"type": "disabled"},
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": effort,
                },
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:
            logger.error(f"kwork: {tag} error want={want_id}: {e}")
            return None
        raw = _first_text(resp)
        if raw:
            parsed = _parse_json(raw)
            if parsed is not None:
                return parsed
            logger.error(f"kwork: {tag} parse error want={want_id}: {raw[:200]!r}")
            return None
        logger.warning(f"kwork: {tag} empty response want={want_id} "
                       f"(stop_reason={resp.stop_reason}), attempt {attempt + 1}")
    return None


async def classify_want(want: dict, api_key: str) -> dict | None:
    text = f"{want.get('name', '')}\n\n{want.get('description', '')}"
    return await _llm_json(api_key, CLASSIFY_SYSTEM_PROMPT, text,
                           CLASSIFY_SCHEMA, "low", want.get("id"), "classify")


def _budget(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


async def generate_response(want: dict, classification: dict, api_key: str) -> dict | None:
    """Возвращает {"text": str, "price": int, "term_days": int} или None.
    priceLimit — желаемый бюджет клиента, possiblePriceLimit — допустимый
    максимум; цена ставится чуть выше желаемого (см. ценовую стратегию
    в GENERATE_SYSTEM_PROMPT)."""
    desired = _budget(want.get("priceLimit"))
    possible = _budget(want.get("possiblePriceLimit"))
    user_content = (
        f"Текст заказа:\n{want.get('name', '')}\n\n{want.get('description', '')}\n\n"
        f"Результат классификации:\n"
        f"matched_skill: {classification.get('matched_skill')}\n"
        f"tier: {classification.get('tier')}\n"
        f"suggested_kwork_service: {classification.get('suggested_kwork_service')}\n"
        f"desired_budget: {desired if desired else 'не указан'}\n"
        f"possible_budget: {possible if possible else 'не указан'}"
    )
    draft = await _llm_json(api_key, GENERATE_SYSTEM_PROMPT, user_content,
                            DRAFT_SCHEMA, "medium", want.get("id"), "generate")
    if draft is None:
        return None
    draft["text"] = (draft.get("text") or "").strip()
    if len(draft["text"]) < 150 or not draft.get("price"):
        logger.error(f"kwork: generate invalid draft want={want.get('id')}: "
                     f"len={len(draft['text'])} price={draft.get('price')}")
        return None
    draft["term_days"] = min(max(int(draft.get("term_days") or 3), 1), 30)
    # Цену считаем детерминированно — модель ей рулить не должна (на практике
    # якорится на допустимый максимум). Правило Давида: чуть выше желаемого
    # бюджета (+10%, округляя до сотен), не выше допустимого. Модельная цена
    # (по справочнику из промпта) остаётся только когда желаемый не указан.
    if desired:
        price = int(round(desired * 1.1 / 100) * 100)
        if possible:
            price = min(price, possible)
        draft["price"] = max(price, 100)
    elif possible:
        draft["price"] = min(int(draft["price"]), possible)
    return draft


# ── публикация отклика (Playwright, только по клику пользователя) ──────────

_playwright = None
_browser = None
_send_lock = asyncio.Lock()


async def _ensure_browser():
    global _playwright, _browser
    if _browser is not None:
        return
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True)


async def close_browser():
    """Вызывать при остановке бота, если нужен аккуратный shutdown."""
    global _playwright, _browser
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


def _term_label(days: int) -> str:
    """'1 день', '3 дня', '5 дней' — как в дропдауне формы Kwork."""
    if days % 100 in (11, 12, 13, 14):
        word = "дней"
    elif days % 10 == 1:
        word = "день"
    elif days % 10 in (2, 3, 4):
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


async def _fill_editor(page, mirror_name: str, text: str) -> int:
    """Текстовые поля формы Kwork — contenteditable-редакторы (Vue) со
    скрытым зеркалом textarea (высота 1px); программный fill() зеркала
    модель не обновляет. Редактор лежит в одном контейнере со своим
    зеркалом — находим через него и печатаем клавиатурой.
    Возвращает длину значения зеркала после ввода."""
    mirror = page.locator(f"textarea[name='{mirror_name}']")
    editor = mirror.locator("xpath=..").locator("[contenteditable='true']").first
    await editor.scroll_into_view_if_needed()
    await editor.click(force=True)
    # click(force) может попасть в перекрывающий элемент и не перенести фокус —
    # тогда ввод утёк бы в предыдущее поле; фокусируем редактор явно
    await editor.evaluate("el => el.focus()")
    for i, line in enumerate(text.split("\n")):
        if i:
            await page.keyboard.press("Enter")
        if line:
            await page.keyboard.insert_text(line)
    await page.wait_for_timeout(300)
    try:
        return len((await mirror.input_value()).strip())
    except Exception:
        return 0


async def _screenshot(page, want_id: int, tag: str) -> str:
    path = f"/opt/claude-bot/logs/kwork_err_{want_id}_{tag}.png"
    try:
        await page.screenshot(path=path)
    except Exception:
        pass
    return path


async def send_response_after_click(want_id: int) -> tuple[bool, str]:
    """Публикует черновик из pending_drafts на Kwork через форму
    /new_offer?project=ID (описание + цена + срок + кнопка «Предложить»).
    Вызывается только по нажатию кнопки «Отправить» в Telegram —
    единственная точка, где реально что-то уходит на площадку от имени Давида."""
    draft = pending_drafts.get(want_id)
    if not draft:
        return False, "Черновик не найден (истёк из памяти или бот перезапускался)."
    if not draft.get("price"):
        return False, "Черновик старого формата (без цены/срока) — пропусти его, новые заявки придут уже в новом формате."
    if not os.path.exists(KWORK_SESSION_FILE):
        return False, "Нет файла сессии Kwork — запусти scripts/kwork_login.py и залей kwork_session.json на сервер."

    text = draft["draft"]
    price = int(draft["price"])
    term_days = int(draft.get("term_days") or 3)

    async with _send_lock:
        try:
            await _ensure_browser()
        except Exception as e:
            logger.error(f"kwork: browser start error: {e}")
            return False, f"⚠️ Не удалось поднять браузер: {e}"

        context = await _browser.new_context(storage_state=KWORK_SESSION_FILE)
        page = await context.new_page()
        try:
            for attempt in range(2):
                try:
                    await page.goto(NEW_OFFER_URL.format(want_id), wait_until="networkidle", timeout=90000)
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(3)

            if "/login" in page.url:
                draft["status"] = "session_expired"
                save_drafts()
                _log_event({"event": "session_expired", "want_id": want_id})
                return False, "Сессия Kwork истекла — перелогинься через scripts/kwork_login.py и залей новый kwork_session.json."

            try:
                await page.locator("textarea[name='description']").wait_for(
                    state="attached", timeout=15000)
            except Exception:
                path = await _screenshot(page, want_id, "form")
                return False, f"⚠️ Форма отклика не открылась — скриншот: {path}"

            # 1. «Название заказа» — ищем contenteditable по data-placeholder,
            # потому что is_visible() на зеркале-textarea даёт false-negative
            # (offsetParent=null из-за CSS), хотя поле реально видимо.
            title_required = False
            title_ph_loc = page.locator("div[contenteditable='true'][placeholder='Введите название заказа']")
            if await title_ph_loc.count():
                title_required = True
                title = (draft.get("want", {}).get("name") or "Выполню вашу задачу").strip()[:70]
                # offsetParent=null (position:fixed-контейнер) — click/focus
                # через Playwright не работают; заполняем напрямую через DOM:
                # trumbowyg слушает 'input' на contenteditable и синхронизирует textarea
                await title_ph_loc.evaluate(
                    "(el, t) => { el.scrollIntoView({block:'center'}); "
                    "el.textContent = t; "
                    "el.dispatchEvent(new Event('input', {bubbles:true})); }",
                    title,
                )
                await page.wait_for_timeout(300)

            # 2. описание — contenteditable-редактор, см. _fill_editor
            filled = await _fill_editor(page, "description", text)
            if filled < 150:
                path = await _screenshot(page, want_id, "desc")
                return False, (f"⚠️ Текст не попал в поле описания "
                               f"(в форме {filled} знаков) — скриншот: {path}")

            # 2б. цена (видимый input type=tel; скрытые — от формы «по этапам»)
            price_input = page.locator("input[type='tel']:visible").first
            await price_input.fill(str(price))

            # 3. срок — кастомный дропдаун, открывается только force-кликом
            term_input = page.get_by_placeholder("Срок выполнения").first
            await term_input.scroll_into_view_if_needed()
            await term_input.click(force=True)
            label = _term_label(term_days)
            pattern = re.compile(rf"^\s*{re.escape(label)}\s*$")
            option = page.locator("li", has_text=pattern).first
            try:
                await option.click(timeout=7000)
            except Exception:
                await page.get_by_text(pattern).first.click(timeout=7000)

            # 3б. порядок оплаты — выбираем ПОСЛЕДНИМ, чтобы Vue не сбрасывал
            # состояние при взаимодействии с другими полями.
            # Ищем наименьший div, текст которого начинается с «Целиком» —
            # это кликабельная карточка-контейнер, а не её внутренний span.
            pay_clicked = await page.evaluate("""
                () => {
                    const divs = [...document.querySelectorAll('div')];
                    const card = divs.find(el =>
                        el.textContent.trim().startsWith('Целиком, когда заказ выполнен')
                        && el.textContent.trim().length < 150
                    );
                    if (!card) return false;
                    card.scrollIntoView({block: 'center'});
                    card.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                }
            """)
            if pay_clicked:
                await page.wait_for_timeout(400)
                # Проверяем: если валидационная ошибка ещё видна — кликаем ещё раз
                pay_err = page.locator("text=Выберите желаемый порядок оплаты")
                if await pay_err.count():
                    await page.evaluate("""
                        () => {
                            const divs = [...document.querySelectorAll('div')];
                            const card = divs.find(el =>
                                el.textContent.trim().startsWith('Целиком, когда заказ выполнен')
                                && el.textContent.trim().length < 150
                            );
                            if (card) {
                                card.scrollIntoView({block: 'center'});
                                card.click();
                                card.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                card.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                card.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            }
                        }
                    """)
                    await page.wait_for_timeout(400)

            # 3б. контроль перед отправкой: оба зеркала заполнены
            desc_len = len((await page.locator("textarea[name='description']").input_value()).strip())
            if desc_len < 150:
                path = await _screenshot(page, want_id, "verify")
                return False, (f"⚠️ Перед отправкой описание оказалось пустым/коротким "
                               f"({desc_len} знаков) — скриншот: {path}")
            if title_required:
                title_val = (await page.locator("textarea[name='name']").input_value()).strip()
                if not title_val:
                    path = await _screenshot(page, want_id, "verify")
                    return False, f"⚠️ Название заказа не заполнилось — скриншот: {path}"

            # 4. отправка
            submit = page.get_by_role("button", name=re.compile(r"^\s*(Откликнуться|Предложить(\s+услугу)?)\s*$"))
            await submit.first.click(timeout=15000)

            # 5. подтверждение: уход со страницы формы или явный текст успеха
            confirmed = False
            try:
                await page.wait_for_url(lambda u: "new_offer" not in u, timeout=15000)
                confirmed = True
            except Exception:
                body = await page.locator("body").inner_text()
                if re.search(r"предложение отправлено|ваш отклик", body, re.IGNORECASE):
                    confirmed = True

            if not confirmed:
                errors = ""
                try:
                    errs = await page.locator("[class*='error']:visible").all_inner_texts()
                    # длинные тексты — не сообщения валидации, а контент
                    # внутри контейнера, чей класс случайно содержит 'error'
                    short = [t.strip() for t in errs if t.strip() and len(t.strip()) <= 80]
                    errors = "; ".join(dict.fromkeys(short))[:200]
                except Exception:
                    pass
                path = await _screenshot(page, want_id, "submit")
                draft["status"] = "error"
                save_drafts()
                _log_event({"event": "send_unconfirmed", "want_id": want_id, "errors": errors})
                return False, (f"⚠️ Отправка не подтвердилась ({errors or 'ошибок на форме не видно'}) — "
                               f"проверь заявку глазами, возможно отклик всё же ушёл. Скриншот: {path}")

            draft["status"] = "sent"
            save_drafts()
            _log_event({"event": "sent", "want_id": want_id, "draft": text,
                        "price": price, "term_days": term_days})
            return True, f"✅ Отклик отправлен: {price} ₽, {_term_label(term_days)}."
        except Exception as e:
            logger.error(f"kwork: send error want={want_id}: {e}")
            path = await _screenshot(page, want_id, "crash")
            draft["status"] = "error"
            save_drafts()
            _log_event({"event": "send_error", "want_id": want_id, "error": str(e)})
            return False, f"⚠️ Ошибка при отправке — проверь заявку глазами: {e}\nСкриншот: {path}"
        finally:
            await context.close()


def skip_want(want_id: int) -> None:
    draft = pending_drafts.get(want_id)
    if draft:
        draft["status"] = "skipped"
        save_drafts()
    _log_event({"event": "skipped", "want_id": want_id})
