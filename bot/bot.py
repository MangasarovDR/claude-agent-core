import os
import re
import json
import time
import logging
import asyncio
import shutil
import subprocess
import base64
import itertools
from collections import defaultdict, Counter
from datetime import datetime
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KEYS_FILE = "/app/keys.json"
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# ===== МОСТ В ЖИВУЮ СЕССИЮ CLAUDE CODE (tmux) =====
# Из контейнера до tmux на хосте не дотянуться, поэтому общение идёт через
# спул-папку: сюда кладём задание, демон tmux-bridge на хосте печатает его
# в промпт сессии. Пока режим включён, всё входящее уходит туда, а не агенту бота.
INBOX_DIR = "/inbox"
INBOX_SPOOL = f"{INBOX_DIR}/spool"
INBOX_FILES = f"{INBOX_DIR}/files"
INBOX_HOST_DIR = "/root/inbox"                # тот же каталог глазами хоста
TMUX_FLAG_FILE = f"{INBOX_DIR}/.tmux_mode"    # флаг переживает рестарт контейнера
TMUX_SESSION = os.getenv("TMUX_SESSION", "claude")
MAX_TMUX_FILE_BYTES = 50 * 1024 * 1024

def tmux_mode_on() -> bool:
    return os.path.exists(TMUX_FLAG_FILE)

def set_tmux_mode(on: bool):
    if on:
        os.makedirs(INBOX_DIR, exist_ok=True)
        open(TMUX_FLAG_FILE, "w").close()
    elif os.path.exists(TMUX_FLAG_FILE):
        os.remove(TMUX_FLAG_FILE)

def to_host_path(path: str) -> str:
    """Путь внутри контейнера -> путь, по которому файл найдёт Claude Code на хосте."""
    return path.replace(INBOX_DIR, INBOX_HOST_DIR, 1)

def save_to_inbox(filename: str, data: bytes) -> str:
    os.makedirs(INBOX_FILES, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", os.path.basename(filename)) or "file"
    stem, dot, ext = safe.rpartition(".")
    path = f"{INBOX_FILES}/{safe}"
    n = 1
    while os.path.exists(path):  # одноимённые не затираем
        base = f"{stem}-{n}{dot}{ext}" if dot else f"{safe}-{n}"
        path = f"{INBOX_FILES}/{base}"
        n += 1
    with open(path, "wb") as f:
        f.write(data)
    return path

def send_to_tmux(text: str, source: str = "text") -> bool:
    """Кладём задание атомарно через .tmp + rename — демон читает только готовые .json."""
    try:
        os.makedirs(INBOX_SPOOL, exist_ok=True)
        name = f"{time.time():.6f}-{source}"
        tmp = f"{INBOX_SPOOL}/{name}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"text": text, "session": TMUX_SESSION, "source": source},
                      f, ensure_ascii=False)
        os.rename(tmp, f"{INBOX_SPOOL}/{name}.json")
        return True
    except Exception as e:
        logger.error(f"tmux bridge: не смог положить задание в спул: {e}")
        return False

def load_keys():
    with open(KEYS_FILE) as f:
        return json.load(f)

def save_keys(data):
    with open(KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_active_key(provider: str):
    data = load_keys()
    keys = data["providers"].get(provider, {}).get("keys", [])
    for key in keys:
        if key.get("active", True):
            return key["value"]
    return None

def add_key(provider: str, key_value: str):
    data = load_keys()
    if provider not in data["providers"]:
        return False
    data["providers"][provider]["keys"].append({
        "value": key_value,
        "active": True,
        "added_at": datetime.now().isoformat()
    })
    save_keys(data)
    return True

def mark_key_exhausted(provider: str, key_value: str):
    data = load_keys()
    for key in data["providers"][provider]["keys"]:
        if key["value"] == key_value:
            key["active"] = False
    save_keys(data)

# ===== МОДУЛЬНЫЙ ОРКЕСТРАТОР =====

import glob
import importlib.util

SEARCH_PROVIDER_NAMES = ["serper", "tavily", "brave"]  # для /keys

class ModuleOrchestrator:
    def __init__(self, modules_dir: str = "/app/modules"):
        self.modules_dir = modules_dir
        self.tools: list = []
        self._handlers: dict = {}
        self._load_all()

    def _load_all(self):
        if not os.path.isdir(self.modules_dir):
            logger.warning(f"Modules dir not found: {self.modules_dir}")
            return
        import sys
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        for manifest_path in sorted(glob.glob(f"{self.modules_dir}/*/manifest.json")):
            module_dir = os.path.dirname(manifest_path)
            module_name = os.path.basename(module_dir)
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                tools = manifest.get("tools", [])
                handler_path = os.path.join(module_dir, "handler.py")
                if os.path.exists(handler_path):
                    spec = importlib.util.spec_from_file_location(f"module_{module_name}", handler_path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    for tool in tools:
                        self._handlers[tool["name"]] = mod.handle_tool
                self.tools.extend(tools)
                logger.info(f"Module loaded: {module_name} ({len(tools)} tools)")
            except Exception as e:
                logger.error(f"Module load failed [{module_name}]: {e}")

    async def execute(self, name: str, inputs: dict, **kwargs) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"Неизвестный инструмент: {name}"
        try:
            return await handler(name, inputs, **kwargs)
        except Exception as e:
            return f"Ошибка инструмента {name}: {e}"

    def reload_module(self, module_name: str) -> bool:
        manifest_path = os.path.join(self.modules_dir, module_name, "manifest.json")
        handler_path = os.path.join(self.modules_dir, module_name, "handler.py")
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            new_tools = manifest.get("tools", [])
            new_names = {t["name"] for t in new_tools}
            # Убираем старые записи этого модуля
            self.tools = [t for t in self.tools if t["name"] not in new_names]
            for n in new_names:
                self._handlers.pop(n, None)
            # Загружаем новый handler
            spec = importlib.util.spec_from_file_location(f"module_{module_name}", handler_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for tool in new_tools:
                self._handlers[tool["name"]] = mod.handle_tool
            self.tools.extend(new_tools)
            logger.info(f"Module reloaded: {module_name} ({len(new_tools)} tools)")
            return True
        except Exception as e:
            logger.error(f"Module reload failed [{module_name}]: {e}")
            return False

orchestrator = ModuleOrchestrator()
sandbox_pending: dict = {}  # user_id -> {sandbox_id, module_name, sandbox_dir, check_ok}
bash_pending: dict = {}     # user_id -> {command} — команды вне whitelist, ждут /confirm_bash
dev_mode: bool = False

INACTIVITY_TIMEOUT = 10 * 60   # секунд без активности → автосохранение
last_activity: dict = {}        # uid → float (unix timestamp)
session_saved: set = set()      # uid → текущая сессия уже сохранена
_app = None                     # ссылка на Application (для отправки сообщений из watcher-а)

async def execute_tool(name: str, inputs: dict, user_id: int = None, bot=None) -> str:
    result = await orchestrator.execute(
        name, inputs,
        user_id=user_id,
        bot=bot,
        get_active_key=get_active_key,
        sandbox_pending=sandbox_pending,
        bash_pending=bash_pending,
    )
    if name == "memory_save" and user_id is not None:
        # New format: "✅ Идея сохранена в Чертоги Разума: [idea] idea-slug"
        m_new = re.match(r"✅ Идея сохранена в Чертоги Разума: \[idea\] (.+)$", result.strip())
        # Legacy format: "✅ Заметка сохранена: Идеи/xxx.md"
        m_old = re.match(r"✅ Заметка сохранена: (.+)\.md\s*$", result.strip())
        if m_new:
            asyncio.create_task(review_idea(m_new.group(1), user_id, bot))
        elif m_old and m_old.group(1).startswith("Идеи/"):
            asyncio.create_task(review_idea(m_old.group(1), user_id, bot))
    return result

AGENT_LOG_FILE = "/opt/claude-bot/logs/agent_audit.jsonl"

COST_INPUT_PER_TOKEN = 3.0 / 1_000_000
COST_OUTPUT_PER_TOKEN = 15.0 / 1_000_000

# Похожие на ключи/токены строки (20+ символов без пробелов) — маскируем перед записью в лог,
# на случай если пользователь случайно пришлёт секрет в сообщении или он попадёт в аргумент/результат инструмента.
_SECRET_LOOKALIKE_RE = re.compile(r'[A-Za-z0-9_\-]{20,}')

def _mask_secrets(value):
    if isinstance(value, str):
        return _SECRET_LOOKALIKE_RE.sub(lambda m: m.group(0)[:4] + "…[masked]", value)
    if isinstance(value, dict):
        return {k: _mask_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_secrets(v) for v in value]
    return value

def write_agent_log(entry: dict):
    os.makedirs(os.path.dirname(AGENT_LOG_FILE), exist_ok=True)
    try:
        with open(AGENT_LOG_FILE, "a") as f:
            f.write(json.dumps(_mask_secrets(entry), ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Agent log write error: {e}")

# ===== ЭТАП 8: RATE LIMITING =====

_RATE_WINDOWS: dict = defaultdict(list)

# Потолок итераций агентного цикла снят. Значение по умолчанию заведомо
# недостижимо для реальных задач и нужно лишь как страховка от бесконечного
# цикла «модель зациклилась на инструменте». AGENT_MAX_ITERATIONS=0 — вообще без границы.
AGENT_MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "1000"))

def _iterations(limit: int = 0):
    """Итератор шагов агентного цикла. limit<=0 → берётся AGENT_MAX_ITERATIONS,
    он же 0 → бесконечный счётчик."""
    n = limit if limit and limit > 0 else AGENT_MAX_ITERATIONS
    return itertools.count() if n <= 0 else range(n)
# Лимиты отключены: остался только счётчик вызовов для /limits и /stats.
RATE_TRACKED = ("llm", "tool")

def _rate_check(key: str) -> tuple:
    """Считает вызовы за сутки, но никогда не блокирует."""
    now = time.time()
    _RATE_WINDOWS[key] = [t for t in _RATE_WINDOWS[key] if now - t < 86400]
    _RATE_WINDOWS[key].append(now)
    return True, ""

# ===== ЭТАП 7: ДОЛГОСРОЧНАЯ ПАМЯТЬ =====

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

async def get_memory_context(query: str) -> str:
    if not MEMORY_API_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{MEMORY_API_URL}/memories/context",
                headers={"Authorization": f"Bearer {MEMORY_API_KEY}"},
                params={"message": query},
            )
        if resp.status_code != 200:
            logger.warning(f"Memory API context: HTTP {resp.status_code}")
            return ""
        data = resp.json()
        memories = data.get("memories", [])
        if not memories:
            return ""
        parts = []
        for m in memories:
            parts.append(f"[{m['slug']}] {m['description']}\n{m['body_md'][:600]}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Memory API context error: {e}")
        return ""

# ===== ROUTER =====

model_override: dict = {}  # user_id -> ключ из MODEL_MODES | "sonnet" | "opus" | None (авто)

# Ручной выбор модели: единый источник правды для меню, /use и метки над ответом.
MODEL_MODES: dict = {
    "auto":         ("🔄 Auto",           "роутер сам выбирает модель по сложности запроса"),
    "claude":       ("✨ Claude",          "всегда Claude, сложность сама выберет sonnet или opus"),
    "sonnet":       ("🎯 Sonnet",         "всегда claude-sonnet, без ухода на opus"),
    "opus":         ("🧠 Opus",           "всегда claude-opus, самая тяжёлая и дорогая"),
    "deepseek":     ("💡 DeepSeek Flash", "быстро и дёшево, из инструментов только веб-поиск и погода"),
    "deepseek-pro": ("🚀 DeepSeek Pro",   "полный набор инструментов, как у Claude — резерв при падении oneprovider"),
}


def _mode_line(uid: int) -> str:
    """Человеческое описание текущего режима — для экрана настроек."""
    current = model_override.get(uid, "auto")
    label, hint = MODEL_MODES.get(current, (current, "задано командой /use"))
    return f"Модель: <b>{label}</b>\n{hint}"

ROUTER_PROMPT = """Классифицируй запрос пользователя по одной из категорий:

simple — ответ из общих знаний без актуальных данных: разговор, перевод, объяснение понятий, математика, написание текстов, советы по памяти.
normal — нужна актуальная информация ИЛИ инструменты: погода, курсы валют, новости, поиск в интернете, работа с календарём, команды на сервере, память (save/search).
heavy — сложная многошаговая задача: несколько инструментов последовательно, написание и деплой кода, критически важные операции с высокой ценой ошибки.

Примеры simple: "как сказать 'спасибо' по-японски", "что такое квантовая запутанность", "напиши стихотворение".
Примеры normal: "какая погода завтра", "найди новости про биткоин", "добавь встречу в календарь", "запомни что я люблю кофе".
Примеры heavy: "создай новый модуль для бота", "перенеси все встречи на следующей неделе на час вперёд".

Ответь ТОЛЬКО одним словом: simple, normal или heavy."""

DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"   # быстрая: роутер, simple-маршрут, последний резерв
DEEPSEEK_PRO_MODEL   = "deepseek-v4-pro"     # умная: подменяет Claude, когда тот недоступен

# Правда об устройстве бота — уходит во все модели. Без неё модель на вопрос
# «есть ли у тебя доступ к Чертогам» сочиняет ответ из общих соображений о том,
# что «внешняя модель к чужой базе не подключена».
SELF_DESCRIPTION = f"""

## Как ты устроен (отвечай по этому, а не по догадкам)

Ты — один бот на трёх моделях, между ними переключает роутер:
- Claude (sonnet/opus через oneprovider.dev) — основной маршрут normal/heavy, весь набор инструментов
- {DEEPSEEK_PRO_MODEL} — резерв, включается сам, когда Claude недоступен, ИЛИ вручную через `/use deepseek-pro`. Инструменты те же самые и полностью рабочие: Чертоги (memory_search/memory_save), Google Calendar, туду, bash на сервере, веб-поиск, погода, корпус текстов, планировщик
- {DEEPSEEK_FLASH_MODEL} — маршрут simple и последний резерв. Только web_search и get_weather, плюс подобранные заметки из Чертогов в промпте

Чертоги Разума — это Memory API на memory.automatiko.ru (SQLite + sqlite-vec), ключ лежит в окружении бота. Доступ к ним есть у ЛЮБОЙ модели, которой выданы инструменты memory_*, — включая DeepSeek на маршруте pro. Никакого «внешняя модель к базе не подключена» — подключение идёт через инструменты бота, а не через саму модель.

Если тебя спрашивают про доступ — отвечай про конкретный маршрут, на котором ты сейчас, и не переноси свои ограничения на другие маршруты."""

# Уровень 2 отката: даже DeepSeek-pro не поднялся, отвечаем на flash без инструментов
DEGRADED_MODE_WARNING = (
    "⚠️ Claude и DeepSeek-pro недоступны, отвечаю в ограниченном режиме: есть только поиск в интернете "
    "и погода, доступа к календарю/серверу/Чертогам/туду нет. "
    "Если просьба требовала одно из этих действий — оно не выполнено.\n\n"
)

# Уровень 1 отката: Claude недоступен, но руки на месте — все инструменты работают
CLAUDE_FALLBACK_NOTICE = (
    f"⚠️ Claude недоступен — работаю на {DEEPSEEK_PRO_MODEL}. Инструменты (календарь, Чертоги, "
    "туду, сервер, поиск, погода) доступны в полном объёме, но формулировки и ход рассуждений "
    "могут отличаться от обычных.\n\n"
)

def get_model_for_route(route: str) -> str:
    data = load_keys()
    cfg = data["providers"].get("anthropic", {})
    if route == "heavy":
        return cfg.get("model_heavy", "claude-opus-4-6")
    return cfg.get("model_default", "claude-sonnet-4-6")

_SIMPLE_RE = re.compile(
    r"^(привет|пока|спасибо|окей|ок|да|нет|хорошо|понял|понятно|"
    r"расскаж|объясн|что такое|как сказать|переведи|напиш|придум|"
    r"помог|посоветуй|совет|почему|зачем|когда был|кто такой|"
    r"что значит|как работает|расскажи|объясни)",
    re.IGNORECASE,
)
_NORMAL_RE = re.compile(
    r"курс|новост|найди|поищи|поиск|календар|встреч|событи|"
    r"запомни|remember|сохрани|память|сервер|команд|запус|провер|"
    r"расписани|план|задач|балан|цен|купить|заказ|kwork|kворк",
    re.IGNORECASE,
)
_WEATHER_RE = re.compile(
    r"погод|температур|дождь|снег|гроза|ветер|осадк|прогноз",
    re.IGNORECASE,
)
_HEAVY_RE = re.compile(
    r"создай модул|новый модул|задеплой|перенеси все|перепиши|"
    r"реализуй|разработ|архитектур|отрефактор",
    re.IGNORECASE,
)

def _local_classify(text: str):
    t = text.strip()
    if _HEAVY_RE.search(t):
        return "heavy"
    if _WEATHER_RE.search(t):
        return "simple"  # погода → DeepSeek + get_weather tool
    if _NORMAL_RE.search(t):
        return "normal"
    if len(t) < 80 and _SIMPLE_RE.match(t):
        return "simple"
    return None  # неочевидно — спросим DeepSeek

async def classify_request(text: str) -> str:
    local = _local_classify(text)
    if local is not None:
        return local
    key = get_active_key("deepseek")
    if not key:
        return "normal"
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": [
                        {"role": "system", "content": ROUTER_PROMPT},
                        {"role": "user", "content": text[:500]},
                    ],
                    "max_tokens": 5,
                    "temperature": 0,
                }
            )
            category = resp.json()["choices"][0]["message"]["content"].strip().lower()
            return category if category in ("simple", "normal", "heavy") else "normal"
    except Exception as e:
        logger.warning(f"Router error: {e}")
        return "normal"

# ===== АГЕНТ =====

conversation_history = {}

# Лок на пользователя: agent_reply вызывается и из обработчика Telegram-сообщений,
# и независимо из _scheduler_watcher (по расписанию) — без лока это гонка за
# conversation_history[uid] при совпадении по времени.
_history_locks: dict = {}

def _get_history_lock(uid: int) -> asyncio.Lock:
    if uid not in _history_locks:
        _history_locks[uid] = asyncio.Lock()
    return _history_locks[uid]

SYSTEM_PROMPT = f"""Ты персональный ИИ-ассистент Давида. Работаешь через Telegram.

КАЛЕНДАРЬ: у тебя есть РЕАЛЬНЫЙ доступ к Google Calendar Давида через инструменты. Ты ОБЯЗАН использовать их для любых действий с календарём. Применительно к календарю никогда не говори, что у тебя нет доступа — это ложь, доступ есть.

ЧЕСТНОСТЬ ПРО ИНСТРУМЕНТЫ (шире календаря): любое действие считается сделанным ТОЛЬКО если ты реально вызвал инструмент и получил результат. Никогда не отчитывайся о том, чего не делал. Если для просьбы Давида подходящего инструмента нет в списке ниже — прямо скажи, что такого инструмента у тебя пока нет, и предложи, какой модуль стоит завести. Сказать «не умею» — нормально; соврать, что сделал, — недопустимо.

Инструменты (список может расширяться динамически через модули):
- get_calendar_events — читать события из Google Calendar
- create_calendar_event — создать событие в Google Calendar
- update_calendar_event — изменить событие в Google Calendar
- delete_calendar_event — удалить событие из Google Calendar
- web_search — поиск в интернете (не более одного раза за итерацию)
- execute_bash — выполнить shell-команду на сервере (безопасные read-only команды — автономно, остальные требуют подтверждения David через /confirm_bash — так и скажи пользователю, если увидишь "ожидает подтверждения")
- memory_save — сохранить в Чертоги Разума (Memory API). Типы: user/feedback/project/knowledge/idea/reference. Используй ПРОАКТИВНО: если в разговоре узнал новый важный факт о Давиде, его предпочтениях, проектах или решениях — сохрани сразу, без напоминания.
- memory_search — семантический + полнотекстовый поиск по Чертогам Разума
- todo_add / todo_list / todo_done / todo_defer / todo_cancel — туду-лист Давида. Задачи из него попадают в утренний дайджест в 8:30, поэтому незакрытая задача будет приходить каждое утро
- corpus_add / corpus_list / corpus_stats — корпус образцов живого письма (проект «обучить Клода писать как человек», стадия накопления). Давид присылает ссылки на телеграм, ВК, блоги, где люди пишут сами, без нейронок, и где ему нравится стиль. Прислал такую ссылку → corpus_add, обязательно сохранив в note, чем именно текст хорош. НЕ используй memory_save для этих ссылок: он перезаписывает запись целиком и накопленное потеряется
- propose_module — предложить новый модуль (только в режиме разработки /devmode on)

ТУДУ-ЛИСТ — ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
- «сделал» / «готово» / «выполнено» / «закрыто» → вызови todo_done. НИКОГДА не отвечай, что отметил задачу, без реального вызова инструмента — задача останется висеть в дайджесте, и Давид получит её снова завтра утром
- «надо сделать» / «напомни» / «поставь задачу» / «не забыть» → вызови todo_add сразу, без переспрашивания
- «перенеси» / «давай завтра» → todo_defer; «отмени» / «не актуально» → todo_cancel
- Перед закрытием и переносом вызывай todo_list, чтобы взять точный id. Перед todo_add — тоже, чтобы не создать дубликат
- Если инструмент вернул несколько совпадений — покажи их Давиду и спроси, какую задачу он имел в виду
- В строке задачи два значка идут подряд и означают РАЗНОЕ: первый — приоритет (🔴 5, 🟠 4, 🟡 3, 🔵 2, ⚪ 1), второй — квадрант матрицы (🔥 важное-срочное, 🎯 важное-несрочное, ⚡ неважное-срочное, 🗑 неважное-несрочное, ❔ не разложено). Не путай их и не называй приоритет квадрантом

Алгоритм работы:
1. СРАЗУ вызывай инструменты — не объясняй намерения
2. Для сложных задач — выполняй последовательно, по одному шагу
3. "Поменять местами" = прочитай оба события, затем обнови каждое
4. НЕ создавай дубликаты — сначала найди, потом измени через update
5. Если задача требует много шагов — выполни все до конца
6. При создании модуля: напиши полный рабочий код, вызови propose_module — дальше пользователь подтверждает

ФОРМАТ ОТВЕТА (Telegram, не веб-страница):
- НИКАКИХ markdown-таблиц, горизонтальных линий (---) и заголовков через #. В Telegram они превращаются в кашу из палок и решёток
- Списки задач, матрицу и план на день инструменты уже возвращают готовыми, аккуратными строками. Отдавай их КАК ЕСТЬ, не переоформляй, не перенумеровывай, не переставляй порядок — он осмысленный (приоритет, внутри него матрица). Свой текст добавляй до или после списка, а не вместо него
- Жирный (**текст**) — только там, где он правда нужен: заголовок блока, ключевая цифра. Не выделяй каждую вторую строку
- Отвечай короче, чем хочется. Одна мысль — одна строка

Формат вывода событий: дата время_начала–время_конца — название

Сегодня: {datetime.now().strftime("%Y-%m-%d %H:%M")}, часовой пояс Europe/Moscow
Отвечай по-русски. Будь точным и внимательным как лучший бизнес-ассистент.
{SELF_DESCRIPTION}"""

async def agent_reply(user_id: int, text: str, bot=None, content=None, force_route: str = None) -> tuple:
    async with _get_history_lock(user_id):
        return await _agent_reply_locked(user_id, text, bot=bot, content=content, force_route=force_route)

async def _agent_reply_locked(user_id: int, text: str, bot=None, content=None, force_route: str = None) -> tuple:
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    # content — мультимодальные блоки (например фото + подпись); text остаётся
    # строкой отдельно, для роутера/памяти/логов, которым нужен обычный текст.
    conversation_history[user_id].append({"role": "user", "content": content if content is not None else text})
    messages = conversation_history[user_id][-20:]

    # Роутинг: определяем модель
    override = model_override.get(user_id)
    if force_route:
        route = force_route
    elif override == "deepseek":
        route = "simple"
    elif override == "deepseek-pro":
        route = "deepseek-pro"
    elif override == "claude":
        # Всегда Claude, но сложность по-прежнему выбирает sonnet или opus.
        # Маршрут simple подменяем на normal — иначе «всегда Claude» уводило бы на DeepSeek
        route = await classify_request(text)
        if route == "simple":
            route = "normal"
    elif override == "sonnet":
        route = "normal"
    elif override == "opus":
        route = "heavy"
    else:
        route = await classify_request(text)

    # DeepSeek (simple) не умеет в изображения — подстраховка на случай, если
    # force_route не был передан вызывающим кодом
    if route == "simple" and content is not None:
        route = "normal"

    write_agent_log({
        "ts": datetime.now().isoformat(),
        "type": "route",
        "route": route,
        "override": override,
        "text_preview": text[:100],
    })
    logger.info(f"Router: {route} (override={override})")

    # simple → DeepSeek flash с поиском и погодой, но с памятью
    if route == "simple":
        mem_ctx = await get_memory_context(text)
        result = await fallback_reply(text, messages, mem_ctx=mem_ctx)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, DEEPSEEK_FLASH_MODEL

    # Резервный агент выбран вручную (/use deepseek-pro) — тот же путь, что при отказе
    # Claude, но без предупреждения: это осознанный выбор, а не авария
    if route == "deepseek-pro":
        mem_ctx = await get_memory_context(text)
        result, used = await deepseek_agent_reply(
            text, messages, mem_ctx=mem_ctx, user_id=user_id, bot=bot)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, used

    key = get_active_key("anthropic")
    if not key:
        result, used = await claude_unavailable_reply(user_id, text, messages, bot=bot)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, used

    # Что Claude успел выполнить до сбоя — чтобы резервная модель не повторила действие
    all_tools_called: list = []
    try:
        client = anthropic.Anthropic(
            api_key=key,
            base_url="https://api.oneprovider.dev",
            max_retries=0,
            timeout=httpx.Timeout(30.0),
        )

        # Агентный цикл — Claude может вызывать инструменты несколько раз
        current_messages = messages.copy()
        used_model = get_model_for_route(route)
        max_iterations = AGENT_MAX_ITERATIONS  # потолок снят, см. _iterations()

        total_in_tokens = 0
        total_out_tokens = 0

        # Этап 7: релевантная память → system prompt
        mem_ctx = await get_memory_context(text)
        effective_system = SYSTEM_PROMPT
        if mem_ctx:
            effective_system += f"\n\n## Долгосрочная память (релевантные заметки):\n{mem_ctx}"

        # Этап 8: propose_module только в режиме разработки
        active_tools = [t for t in orchestrator.tools if dev_mode or t["name"] != "propose_module"]

        for i in _iterations():
            allowed, reason = _rate_check("llm")
            if not allowed:
                conversation_history[user_id].append({"role": "assistant", "content": reason})
                return reason, "blocked"

            response = client.messages.create(
                model=used_model,
                max_tokens=10000,
                system=effective_system,
                tools=active_tools,
                messages=current_messages
            )

            in_tok = getattr(response.usage, "input_tokens", 0)
            out_tok = getattr(response.usage, "output_tokens", 0)
            total_in_tokens += in_tok
            total_out_tokens += out_tok

            # Собираем текст из всех блоков
            text_parts = []
            tool_calls = []

            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            # Если нет вызовов инструментов — финальный ответ
            if not tool_calls or response.stop_reason == "end_turn":
                final_text = "\n".join(text_parts)
                conversation_history[user_id].append({
                    "role": "assistant",
                    "content": final_text
                })
                est_usd = round(
                    total_in_tokens * COST_INPUT_PER_TOKEN +
                    total_out_tokens * COST_OUTPUT_PER_TOKEN, 6
                )
                write_agent_log({
                    "ts": datetime.now().isoformat(),
                    "type": "request",
                    "route": route,
                    "model": used_model,
                    "iterations": i + 1,
                    "tools": all_tools_called,
                    "in_tokens": total_in_tokens,
                    "out_tokens": total_out_tokens,
                    "est_usd": est_usd,
                })
                return final_text, used_model

            # Выполняем инструменты (web_search — не более одного за итерацию)
            tool_results = []
            search_done = False
            for tool_call in tool_calls:
                if tool_call.name == "web_search" and search_done:
                    result = "Поиск пропущен: за итерацию выполняется не более одного web_search."
                else:
                    t_ok, t_reason = _rate_check("tool")
                    if not t_ok:
                        result = t_reason
                    else:
                        result = await execute_tool(tool_call.name, tool_call.input, user_id=user_id, bot=bot)
                    if tool_call.name == "web_search":
                        search_done = True

                input_preview = {k: str(v)[:80] for k, v in tool_call.input.items()}
                write_agent_log({
                    "ts": datetime.now().isoformat(),
                    "type": "tool",
                    "name": tool_call.name,
                    "input": input_preview,
                    "result_preview": result[:150],
                })
                all_tools_called.append(tool_call.name)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result
                })

            # Добавляем вызовы и результаты в историю
            current_messages.append({
                "role": "assistant",
                "content": response.content
            })
            current_messages.append({
                "role": "user",
                "content": tool_results
            })

        est_usd = round(
            total_in_tokens * COST_INPUT_PER_TOKEN +
            total_out_tokens * COST_OUTPUT_PER_TOKEN, 6
        )
        write_agent_log({
            "ts": datetime.now().isoformat(),
            "type": "request",
            "model": used_model,
            "iterations": max_iterations,
            "tools": all_tools_called,
            "in_tokens": total_in_tokens,
            "out_tokens": total_out_tokens,
            "est_usd": est_usd,
            "note": "hit_max_iterations",
        })
        # Сохраняем в историю, чтобы не оборвать контекст (user без assistant = баг)
        fallback_msg = "⚠️ Достигнут лимит итераций агентного цикла. Попробуй сформулировать задачу короче или разбить на шаги."
        conversation_history[user_id].append({"role": "assistant", "content": fallback_msg})
        return fallback_msg, used_model

    except anthropic.AuthenticationError:
        mark_key_exhausted("anthropic", key)
        result, used = await claude_unavailable_reply(
            user_id, text, messages, bot=bot, done_tools=all_tools_called)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, used
    except anthropic.APIStatusError as e:
        logger.warning(f"oneprovider upstream error HTTP {e.status_code}: {e.message}")
        result, used = await claude_unavailable_reply(
            user_id, text, messages, bot=bot, done_tools=all_tools_called)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, used
    except Exception as e:
        logger.error(f"Agent error: {e}")
        result, used = await claude_unavailable_reply(
            user_id, text, messages, bot=bot, done_tools=all_tools_called)
        conversation_history[user_id].append({"role": "assistant", "content": result})
        return result, used

SIMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Поиск актуальной информации в интернете (вызывай не более одного раза за ответ)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Текущая погода или прогноз на ближайшие дни для Давида (Санкт-Петербург). day_offset=0 — сейчас, 1 — завтра, 2 — послезавтра и т.д.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day_offset": {"type": "integer", "description": "0 = сегодня, 1 = завтра, …", "default": 0}
                },
                "required": [],
            },
        },
    },
]

async def fallback_reply(text: str, history: list, mem_ctx: str = "") -> str:
    key = get_active_key("deepseek")
    if not key:
        return "❌ Нет доступных моделей. Проверь ключи: /keys"
    try:
        system = (
            f"Ты персональный ИИ-ассистент Давида. Отвечай по-русски. Будь конкретным.\n"
            f"Сегодня: {datetime.now().strftime('%Y-%m-%d %H:%M')}, часовой пояс Europe/Moscow."
            + SELF_DESCRIPTION
            + f"\n\nСЕЙЧАС ТЫ — {DEEPSEEK_FLASH_MODEL} на маршруте simple. У тебя только web_search "
              "и get_weather. Календаря, туду, сервера и поиска по Чертогам на этом маршруте нет — "
              "но НЕ выдавай это за свойство DeepSeek вообще: на маршруте "
              f"{DEEPSEEK_PRO_MODEL} те же самые инструменты доступны в полном объёме."
        )
        if mem_ctx:
            system += (
                "\n\n## Долгосрочная память:\n"
                "Блок ниже — выдержки из Чертогов Разума (Memory API), подобранные под этот вопрос. "
                "Это и есть твой доступ к памяти на этом маршруте: не полнотекстовый поиск, а именно эти заметки. "
                "НЕ говори 'у меня нет доступа к памяти' — доступ есть, вот он. "
                "Если нужного факта в этих заметках нет — так и скажи: "
                "'в сохранённых заметках этого нет', а не 'у меня нет доступа'.\n"
                f"{mem_ctx}"
            )
        system += (
            "\n\nУ тебя есть инструмент web_search — используй его для любых вопросов, "
            "требующих актуальных данных (новости, погода, курсы, факты после твоего обучения). "
            "НЕ говори 'у меня нет доступа к интернету' — доступ есть через этот инструмент."
        )
        messages = [{"role": "system", "content": system}]
        messages += [m for m in history[-10:] if isinstance(m.get("content"), str)]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "deepseek-v4-flash",
                    "messages": messages,
                    "max_tokens": 2000,
                    "tools": SIMPLE_TOOLS,
                }
            )
            data = resp.json()
            choice = data["choices"][0]["message"]

            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                return choice["content"]

            # Один вызов инструмента за ответ (та же политика, что в основном цикле)
            call = tool_calls[0]
            tool_name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            tool_result = await execute_tool(tool_name, args)

            messages.append(choice)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": tool_result,
            })

            resp2 = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "deepseek-v4-flash", "messages": messages, "max_tokens": 2000}
            )
            return resp2.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка: {e}"

# ===== РЕЗЕРВНЫЙ АГЕНТ НА DEEPSEEK =====
# Если Claude (oneprovider) недоступен, работу подхватывает deepseek-v4-pro — с тем же
# системным промптом, той же памятью и ТЕМ ЖЕ набором инструментов, что у Claude.
# Это отличается от fallback_reply(): та отвечает на flash и умеет только поиск и погоду.

def _tools_to_openai(tools: list) -> list:
    """Anthropic-формат инструментов (name/description/input_schema) → OpenAI-формат DeepSeek."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _history_to_openai(history: list) -> list:
    """
    История диалога (Anthropic-формат) → OpenAI-формат.
    Мультимодальные блоки и блоки tool_result отбрасываем: у DeepSeek другой формат,
    а картинки он всё равно не видит. Остаётся текстовая канва разговора.
    """
    return [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if isinstance(m.get("content"), str) and m["content"].strip()
    ]


async def deepseek_agent_reply(
    text: str,
    history: list,
    mem_ctx: str = "",
    user_id: int = None,
    bot=None,
    done_tools: list = None,
    model: str = DEEPSEEK_PRO_MODEL,
    max_iterations: int = 0,   # 0 → AGENT_MAX_ITERATIONS, потолок снят
) -> tuple:
    """
    Полный агентный цикл на DeepSeek: те же инструменты, что у Claude.
    Возвращает (текст, модель). Бросает исключение, если DeepSeek недоступен —
    вызывающий код откатывается на fallback_reply (flash, только поиск и погода).
    """
    key = get_active_key("deepseek")
    if not key:
        raise RuntimeError("нет активного ключа deepseek")

    system = SYSTEM_PROMPT + (
        f"\n\nСЕЙЧАС ТЫ — {model}. Инструменты выше выданы тебе полностью, включая Чертоги "
        "(memory_search/memory_save) и календарь: доступ идёт через инструменты бота, а не через саму модель.\n"
        "РЕЖИМ РЕЗЕРВА: основная модель (Claude) сейчас недоступна, ты работаешь вместо неё. "
        "Инструменты выше — настоящие и полностью доступны тебе, пользуйся ими как обычно. "
        "Не отказывайся от действий и не ссылайся на смену модели как на причину отказа."
    )
    if done_tools:
        # Claude мог успеть что-то выполнить до сбоя — иначе резервная модель повторит действие
        system += (
            "\n\nВАЖНО: в рамках этого же запроса ДО сбоя уже были выполнены инструменты: "
            f"{', '.join(done_tools)}. Считай их выполненными и НЕ вызывай повторно — "
            "иначе получится дубль (задача, событие в календаре, заметка). "
            "Если не уверен в результате — сначала прочитай текущее состояние соответствующим "
            "read-инструментом и только потом решай."
        )
    if mem_ctx:
        system += f"\n\n## Долгосрочная память (релевантные заметки):\n{mem_ctx}"

    active_tools = [t for t in orchestrator.tools if dev_mode or t["name"] != "propose_module"]
    tools = _tools_to_openai(active_tools)

    convo = _history_to_openai(history)
    # Последнее сообщение было мультимодальным (фото) — оно отфильтровалось.
    # Подставляем текст, честно предупредив модель, что картинку она не видит.
    if not convo or convo[-1]["role"] != "user":
        convo.append({
            "role": "user",
            "content": text + "\n\n(К сообщению было приложено изображение. "
                              "Ты его не видишь — если ответ зависит от картинки, так и скажи.)",
        })
    messages = [{"role": "system", "content": system}] + convo

    tools_called: list = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for i in _iterations(max_iterations):
            allowed, reason = _rate_check("llm")
            if not allowed:
                return reason, "blocked"

            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    # v4-pro — reasoning-модель: размышления тратят тот же бюджет,
                    # при маленьком max_tokens ответ обрывается до вызова инструмента
                    "max_tokens": 8000,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:300]}")

            choice = resp.json()["choices"][0]["message"]
            calls = choice.get("tool_calls") or []

            if not calls:
                final_text = (choice.get("content") or "").strip()
                write_agent_log({
                    "ts": datetime.now().isoformat(),
                    "type": "request",
                    "route": "claude_fallback",
                    "model": model,
                    "iterations": i + 1,
                    "tools": tools_called,
                })
                return final_text, model

            # reasoning_content обратно в API не отправляем — он не принимается
            messages.append({
                "role": "assistant",
                "content": choice.get("content") or "",
                "tool_calls": calls,
            })

            search_done = False
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name == "web_search" and search_done:
                    result = "Поиск пропущен: за итерацию выполняется не более одного web_search."
                else:
                    t_ok, t_reason = _rate_check("tool")
                    if not t_ok:
                        result = t_reason
                    else:
                        result = await execute_tool(name, args, user_id=user_id, bot=bot)
                    if name == "web_search":
                        search_done = True

                write_agent_log({
                    "ts": datetime.now().isoformat(),
                    "type": "tool",
                    "name": name,
                    "input": {k: str(v)[:80] for k, v in args.items()},
                    "result_preview": result[:150],
                    "model": model,
                })
                tools_called.append(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

    write_agent_log({
        "ts": datetime.now().isoformat(),
        "type": "request",
        "route": "claude_fallback",
        "model": model,
        "iterations": i + 1,
        "tools": tools_called,
        "note": "hit_max_iterations",
    })
    return (
        "⚠️ Достигнут лимит итераций резервного цикла (DeepSeek). "
        "Попробуй сформулировать задачу короче или разбить на шаги."
    ), model


async def claude_unavailable_reply(
    user_id: int, text: str, history: list, bot=None, done_tools: list = None
) -> tuple:
    """
    Единая точка отката, когда Claude не отвечает.
    Уровень 1 — deepseek-v4-pro со всеми инструментами.
    Уровень 2 — deepseek-v4-flash без инструментов (только если и pro не поднялся).
    """
    mem_ctx = await get_memory_context(text)
    try:
        result, model = await deepseek_agent_reply(
            text, history, mem_ctx=mem_ctx, user_id=user_id, bot=bot, done_tools=done_tools
        )
        return CLAUDE_FALLBACK_NOTICE + result, model
    except Exception as e:
        logger.error(f"DeepSeek fallback ({DEEPSEEK_PRO_MODEL}) failed: {e}")
        result = DEGRADED_MODE_WARNING + await fallback_reply(text, history, mem_ctx=mem_ctx)
        return result, DEEPSEEK_FLASH_MODEL

# ===== ВАЛИДАТОР ИДЕЙ =====
# Срабатывает автоматически после memory_save в 'Идеи/...' (см. execute_tool).
# Фоновая, не блокирует агентный цикл, который делал сохранение.

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

def _parse_frontmatter(raw: str) -> tuple:
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, raw[m.end():]

def _render_idea_file(fm: dict, body: str) -> str:
    header = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\n{header}\n---\n\n" + body.lstrip("\n")

# ===== ФОРМАТИРОВАНИЕ ОТВЕТОВ ДЛЯ TELEGRAM =====
# Модели пишут markdown, а Telegram без parse_mode печатает его буквально —
# отсюда «звёздочки» в ответах. Переводим безопасное подмножество markdown в HTML.

_MD_CODEBLOCK   = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD        = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Пробел после решёток обязателен, иначе строка, начинающаяся с хештега
# (#vision), была бы принята за заголовок
_MD_HEADER      = re.compile(r"^\s{0,3}#{1,6}[ \t]+(.+?)\s*$", re.MULTILINE)
_MD_BULLET      = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_MD_HR          = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_MD_TABLE_SEP   = re.compile(r"^\s*\|[\s:|-]+\|\s*$", re.MULTILINE)


def _table_rows_to_lines(text: str) -> str:
    """
    Markdown-таблицы в Telegram превращаются в кашу из палок: ширины нет, перенос ломает строки.
    Разбираем их в обычные строки «ячейка · ячейка».
    """
    text = _MD_TABLE_SEP.sub("", text)
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 3:
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c]
            out.append(" · ".join(cells))
        else:
            out.append(line)
    return "\n".join(out)


def md_to_telegram_html(text: str) -> str:
    """Markdown → Telegram HTML. Всё неизвестное превращается в обычный текст, а не в мусор."""
    if not text:
        return ""

    # Блоки кода вынимаем до экранирования, чтобы внутри них ничего не конвертировалось
    blocks: list = []

    def _stash(m):
        blocks.append(m.group(1))
        return f"\x00CODE{len(blocks) - 1}\x00"

    text = _MD_CODEBLOCK.sub(_stash, text)
    text = _table_rows_to_lines(text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = _MD_HEADER.sub(r"<b>\1</b>", text)
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _MD_HR.sub("", text)
    text = _MD_BULLET.sub(r"\1• ", text)
    # Больше трёх пустых строк подряд — почти всегда следствие вырезанных разделителей
    text = re.sub(r"\n{3,}", "\n\n", text)

    for n, block in enumerate(blocks):
        escaped = block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CODE{n}\x00", f"<pre>{escaped}</pre>")
    return text.strip()


TG_MSG_LIMIT = 3500


async def send_reply(target, text: str, chat_id: int = None) -> None:
    """
    Единая отправка ответа агента: markdown → HTML, разбивка по лимиту Telegram.
    Если HTML почему-то не распарсился — уходит тот же текст без разметки, ответ не теряется.
    target — message (reply_text) или bot (send_message + chat_id).
    """
    async def _send(chunk: str, html: bool):
        kwargs = {"parse_mode": "HTML"} if html else {}
        if chat_id is None:
            await target.reply_text(chunk, **kwargs)
        else:
            await target.send_message(chat_id, chunk, **kwargs)

    html = md_to_telegram_html(text)
    for body in (html, text):
        chunks = [body[i:i + TG_MSG_LIMIT] for i in range(0, len(body), TG_MSG_LIMIT)] or [""]
        try:
            for chunk in chunks:
                await _send(chunk, html=(body is html))
            return
        except Exception as e:
            logger.warning(f"send_reply: HTML не принят Telegram ({e}), отправляю как обычный текст")


def _model_label(uid: int, model: str) -> str:
    """
    Метка модели над ответом. Если режим закреплён через /use — говорим об этом прямо:
    иначе закреплённый flash молча отвечает на вопросы, которым нужен Claude или pro.
    """
    override = model_override.get(uid)
    if override:
        return f"🤖 {model} · режим закреплён: /use {override} → сброс /use auto"
    return f"🤖 {model}"


async def _send_chunked(bot, chat_id: int, text: str) -> None:
    await send_reply(bot, text, chat_id=chat_id)

IDEA_VALIDATOR_SYSTEM = """Твоя роль
Ты — холодный опытный инвестиционный аналитик и серийный предприниматель в одном лице. Ты повидал сотни запусков: и тех, кто поднял миллиарды, и тех, кто слил всё в ноль на «гениальной» идее. Твоя работа — не вдохновлять, а проверять. Тебе плевать на чувства автора идеи. Тебе важна ровно одна вещь:
на этой идее уже кто-то в мире или в России заработал реальные деньги — и сможет ли повторить конкретно этот человек.
Говоришь прямо, по-русски, без англицизмов и без воды. Если идея — мусор, говоришь это в лицо и объясняешь почему. Лесть = профнепригодность. Лучше горькая правда, чем приятная иллюзия.

Железные правила (нарушать нельзя)
Константа России = 1/20. Российский рынок по умолчанию ≈ одна двадцатая мирового. Берёшь годовую выручку крупнейшего мирового игрока в долларах, делишь на 20, переводишь в рубли по текущему курсу — это базовый потолок российского рынка. Можешь корректировать вверх или вниз, но только с аргументами (локальное преимущество, заблокированные западные конкуренты, культура пиратства, регуляторка, платёжеспособность) и всегда показываешь расчёт. Без поправок 1/20 — это дефолт.
Цифры — только из нескольких источников. Любое число по выручке, команде, оценке подтверждаешь минимум двумя независимыми источниками и сверяешь их между собой. Расходятся — показываешь разброс и говоришь, какой цифре веришь и почему. Цифра — оценка или слух — помечаешь это явно. Никаких чисел с потолка.
Кладбище важнее парада. Ищешь не только тех, кто заработал, но и тех, кто пробовал и сдох. Где деньги — там и трупы. Уроки лежат на кладбище, а не на сцене. Ошибку выжившего вскрываешь всегда: «выжили эти трое» не значит ничего, если попробовали триста.
Цель решает всё. Один и тот же бизнес — победа для того, кто хочет 1 млн ₽/мес, и провал для того, кто хочет 100. Без понимания амбиции автора вердикт невозможен. Всё меряешь относительно его цели.

ВАЖНО — это автоматический headless-запуск, автора рядом нет и он не ответит на вопросы.
Тебе на вход придёт заметка из Obsidian с идеей Давида — она уже прошла через ассистента, который её организовал. Извлеки из неё всё, что можно, по чек-листу калибровки (размер цели по деньгам в месяц/год, что за продукт, кто платит, какая боль/желание, монетизация, канал продаж, география, что уже есть — деньги/экспертиза/аудитория/связи). Если какого-то пункта в заметке нет — НЕ придумывай и не жди ответа: явно выпиши его в блок "❓ Что не указано (предположения)" в начале отчёта, укажи, какое консервативное допущение ты взял вместо ответа, и как вердикт изменился бы при другом допущении. Работай сразу до конца, не останавливайся в ожидании ответа — здесь некому отвечать.

ЭТАП 1 — Глубокий ресёрч (делаешь сам, по шагам, в несколько проходов)
Это не один поиск. Это многоэтапная разведка, где после каждого шага ты сам себя проверяешь и критикуешь. Не вываливай первое, что нашёл — копай, сверяй, ищи опровержения. Думай тщательно перед тем, как писать финальный отчёт; не торопись. Инструмент web_search у тебя ограничен по бюджету — цель уложиться примерно в 10-15 запросов на весь разбор, так что формулируй запросы ёмко и не повторяйся, веди в уме список того, что уже искал.
Шаг 1.1 — Прямые двойники (та же идея один в один). Найди компании, которые делают ровно это — в России и в мире. По каждому ключевому игроку вытащи: что за компания, год запуска, размер команды, выручка (несколько источников!), привлечённые деньги, траектория — растёт, стагнирует, мёртв. Ответь на вопрос: эта модель в чистом виде вообще обкатана? Кто на ней заработал и сколько?
Шаг 1.2 — Кладбище. Кто пробовал то же самое и провалился? Почему именно — рынок, монетизация, юнит-экономика, регуляторка, тайминг? Это самые дорогие уроки, добытые чужими деньгами. Не пропускай этот шаг никогда.
Шаг 1.3 — Соседи (та же ниша или индустрия, другое исполнение). Похожие, но не идентичные: тот же рынок, но другая бизнес-модель, монетизация, дистрибьюция или сегмент. Здесь ищешь скрытое — то, чего автор не замечает. Возможно, продукт нужно переупаковать иначе.
Шаг 1.4 — Карта моделей и развороты. Разложи, какие вообще способы заработать крутятся вокруг этой идеи. Кто взял похожее ядро и заработал на ДРУГОЙ монетизации? Здесь главный инсайт: может, в задуманном виде идея не работает, но разворот в B2B, B2G, разовую продажу, лицензию, маркетплейс, подписку или рекламную модель — её оживляет. Покажи, в какие модели можно выйти из текущей и кто это уже доказал деньгами.
После каждого шага спрашивай себя: я не обманываюсь? цифры реальные? я не принял трёх выживших за весь рынок? нет ли встречных фактов, которые я игнорирую?

ЭТАП 2 — Проверка Россией
Возьми крупнейшего мирового игрока, примени константу 1/20, переведи в рубли — получи грубый потолок российского рынка. Скорректируй на местную специфику с аргументами. Сопоставь полученный потолок с целью автора (или с консервативным допущением о цели, если она не указана).
Главный вопрос: в России на этом реально подняться до его цифры — или нет?

ЭТАП 3 — Вердикт (большой структурированный отчёт)
Выдай развёрнутый отчёт-документ строго в markdown, без вызовов инструментов на этом шаге — это твой финальный ответ. Обязательно начни отчёт со строки-светофора одним из символов 🟢, 🟡 или 🔴 (ровно один из трёх, в самом начале первой строки — по нему автоматика проставит статус идеи), например: "🟢 Идея рабочая, обкатанная модель, потолок в России выше цели." Внутри обязательно:
❓ Что не указано (предположения) — см. выше, если применимо.
🟢🟡🔴 Светофор идеи. Обкатана модель или нет. Можно ли заработать. Сколько реально в России против цели (или консервативного допущения).
💀 Если идея — мусор, говоришь прямо: «Это бред, в России на этом не заработать, иди думай другую» — и объясняешь почему, без смягчения. Не выдумывай надежду, которой нет.
💰 Сколько денег. Реалистичный потолок выручки и прибыли в России — с расчётом от 1/20.
🧭 Что делать. Один из трёх сценариев: (а) идея рабочая как есть — вот доказанный путь и кого копировать; (б) идею надо развернуть — конкретный pivot и прецедент, который это доказал деньгами; (в) идея мёртвая — почему и что с этим делать.
⚰️ Чужие грабли. На чём прогорели те, кто пробовал. Что не повторять.
📊 Кого копировать. Конкретные компании-референсы, чью модель реально воспроизвести в России.
📚 Источники. В конце — список источников со ссылками, сгруппированный по игрокам.

Пиши отчёт по-русски, прямо, по делу, без воды и без англицизмов. Твоя ценность не в том, чтобы человеку было приятно, а в том, чтобы он не слил год жизни и свои деньги на то, что заведомо не взлетит."""

async def _run_idea_validator(rel_path: str, idea_body: str) -> tuple:
    key = get_active_key("anthropic")
    client = anthropic.Anthropic(api_key=key, base_url="https://api.oneprovider.dev")
    model = get_model_for_route("normal")
    idea_tools = [t for t in orchestrator.tools if t["name"] == "web_search"]

    current_messages = [{
        "role": "user",
        "content": f"Заметка «{rel_path}.md» из папки Идеи. Разбери эту идею по методике из системного промпта:\n\n{idea_body}"
    }]

    total_in = total_out = 0
    review_text = ""
    for i in _iterations():
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=IDEA_VALIDATOR_SYSTEM,
            tools=idea_tools,
            messages=current_messages,
        )
        total_in += getattr(response.usage, "input_tokens", 0)
        total_out += getattr(response.usage, "output_tokens", 0)

        text_parts, tool_calls = [], []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls or response.stop_reason == "end_turn":
            review_text = "\n".join(text_parts)
            break

        tool_results = []
        for call in tool_calls:
            result = await execute_tool(call.name, call.input, user_id=None, bot=None)
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": result})
        current_messages.append({"role": "assistant", "content": response.content})
        current_messages.append({"role": "user", "content": tool_results})
    else:
        review_text = review_text or "⚠️ Анализ не завершён за отведённое число шагов — не хватило итераций для полного разбора."

    est_usd = round(total_in * COST_INPUT_PER_TOKEN + total_out * COST_OUTPUT_PER_TOKEN, 6)
    write_agent_log({
        "ts": datetime.now().isoformat(),
        "type": "request",
        "route": "idea_review",
        "model": model,
        "iterations": i + 1,
        "in_tokens": total_in,
        "out_tokens": total_out,
        "est_usd": est_usd,
    })

    m = re.search(r"[🟢🟡🔴]", review_text)
    verdict_emoji = m.group(0) if m else "⚪️"
    return review_text, verdict_emoji

async def review_idea(rel_path: str, user_id: int, bot) -> None:
    fpath = os.path.join("/obsidian", rel_path + ".md")
    try:
        raw = open(fpath, encoding="utf-8", errors="ignore").read()
    except Exception as e:
        logger.error(f"review_idea: не удалось прочитать {fpath}: {e}")
        return

    fm, body = _parse_frontmatter(raw)
    idea_title = os.path.basename(rel_path)

    if not get_active_key("anthropic"):
        logger.warning("review_idea: нет ключа anthropic, пропускаю ревью")
        return

    if bot:
        try:
            await bot.send_message(user_id, f"🔎 Разбираю идею «{idea_title}» — это может занять несколько минут…")
        except Exception:
            pass

    try:
        review_text, verdict_emoji = await _run_idea_validator(rel_path, body)
    except Exception as e:
        logger.error(f"review_idea: анализ провален для {rel_path}: {e}")
        fm["статус"] = "ошибка_разбора"
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(_render_idea_file(fm, body))
        except Exception:
            pass
        if bot:
            try:
                await bot.send_message(user_id, f"❌ Не удалось разобрать идею «{idea_title}»: {e}")
            except Exception:
                pass
        return

    today = datetime.now().strftime("%Y-%m-%d")
    fm["статус"] = "разобрана"
    fm["вердикт"] = verdict_emoji
    fm["дата_разбора"] = today
    new_body = body.rstrip("\n") + f"\n\n---\n\n## 🔬 Рецензия валидатора ({today})\n\n{review_text}\n"

    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(_render_idea_file(fm, new_body))
    except Exception as e:
        logger.error(f"review_idea: не удалось записать {fpath}: {e}")

    if bot:
        try:
            await _send_chunked(
                bot, user_id,
                f"{verdict_emoji} Рецензия идеи «{idea_title}»\n\n{review_text}\n\n📁 Сохранено в Obsidian: {rel_path}.md"
            )
        except Exception as e:
            logger.error(f"review_idea: не удалось отправить рецензию в Telegram: {e}")

# ===== МАЙНЕР БОЛЕЙ (/mine_pains) =====
# Ручная команда (не триггерится автоматически, в отличие от валидатора идей).
# Тот же скелет цикла, что у review_idea/_run_idea_validator: headless-агент,
# единственный инструмент web_search, результат уходит в Obsidian + Telegram.

_PAIN_SLUG_RE = re.compile(r'[^\w\-]', re.UNICODE)

def _slugify_niche(niche: str) -> str:
    slug = _PAIN_SLUG_RE.sub('_', niche.strip().lower()).strip('_')
    slug = re.sub(r'_+', '_', slug)
    return slug[:60] or "ниша"

PAIN_MINER_SYSTEM = """Твоя роль
Ты — тот же холодный опытный инвестиционный аналитик и серийный предприниматель, что разбирает идеи Давида, но здесь твоя задача не оценить готовую идею, а НАЙТИ сырьё для идей: реальные, живые боли пользователей в заданной нише, о которых люди сами пишут в интернете. Тебе плевать на то, насколько «интересна» тема — тебе важно только одно: жалуются ли на это реальные люди достаточно часто и достаточно эмоционально, чтобы за решение можно было получить деньги.
Говоришь прямо, по-русски, без англицизмов и без воды. Никакой лести и никакой выдумки — если для ниши не нашлось внятных болей, так и пишешь, не натягиваешь слабый материал на красивый отчёт.

Железные правила (нарушать нельзя)
Ноль галлюцинаций. Каждая боль в отчёте обязана опираться на конкретный найденный источник (тред, отзыв, пост, обсуждение) — при написании боли всегда указывай, где ты это нашёл (ссылка или явное описание источника: площадка + примерная дата). Если не можешь подтвердить боль конкретной находкой из поиска — не включай её в отчёт, лучше короткий список из реальных болей, чем длинный из придуманных.
Только легальные источники. Ты используешь исключительно инструмент web_search (обычная поисковая выдача — индексированные страницы, а не обход платных API или технических ограничений площадок). Если по нише почти ничего не находится через обычный поиск — это тоже результат, честно напиши, что ниша плохо просматривается открытым поиском.
Цифровая дифференциация силы боли. Каждой найденной боли присваиваешь числовую оценку силы от 1 до 10 — и оценки должны реально различаться между болями, а не быть одинаковыми «для галочки». Основание оценки — прозрачно объясняешь по каждой боли: частота упоминаний (это единичная жалоба или systemic-паттерн, встречается у многих авторов?), эмоциональный накал формулировок (раздражение vs отчаяние vs «я плачу кому угодно, кто это решит»), явные сигналы готовности платить (люди уже платят за костыли/ручные решения/фрилансеров), наличие и цена существующих платных заменителей.
Кладбище и конкуренты — коротко. Если по ходу поиска встречаешь готовые продукты/попытки закрыть эту боль — упомяни коротко (работает / заброшено / дорого / плохо), это не полноценный разбор идеи (это делает отдельный валидатор идей), но игнорировать очевидную конкуренцию нельзя.
Головой думай про вайбкодинг-соло-без-бюджета. Для каждой боли предлагаешь ровно 3 варианта быстрого решения силами одного человека, кодящего с помощью ИИ-ассистентов, без стартового бюджета (без найма, без платной инфраструктуры на старте, без юрлица). Варианты должны быть реалистичны технически — если боль объективно требует команды/лицензий/капитала, честно скажи это как часть варианта, а не делай вид, что вайбкодинг решит всё.

ВАЖНО — это автоматический headless-запуск, отвечать некому, никто не ответит на уточняющие вопросы. Ниша задана одной строкой от Давида — если она сформулирована широко, сам разбей её на 2-4 под-сегмента и ищи в каждом, явно обозначив сегментацию в начале отчёта.

ЭТАП 1 — Разведка (несколько проходов, инструмент web_search ограничен по бюджету — ориентируйся на 10-15 запросов на всю нишу)
Шаг 1.1 — Прямые жалобы. Ищи форумы, отзывы, тематические сообщества, комментарии, где люди из этой ниши явно формулируют «достало», «бесит», «не могу найти», «плачу за это руками» и т.п. Используй разные формулировки запроса и разные площадки (форумы отрасли, отзовики, тематические паблики, профессиональные сообщества) — не останавливайся на первой странице выдачи.
Шаг 1.2 — Повторяемость. По каждой найденной боли проверь ещё одним запросом: это единичный крик души или паттерн, который встречается у разных людей в разных местах? Разовая жалоба — это слабый сигнал, снижай ей оценку силы боли.
Шаг 1.3 — Существующие костыли и деньги. Ищи, чем люди уже затыкают эту боль сейчас (самодельные таблицы, фрилансеры, дорогие энтерпрайз-решения, ручной труд) — это прямой сигнал готовности платить и подсказка для микропродукта.
После каждого прохода спрашивай себя: я не выдумал деталь, чтобы отчёт выглядел полнее? у меня есть реальная ссылка/источник под каждой болью?

ЭТАП 2 — Финальный отчёт (строго markdown, без вызовов инструментов на этом шаге)
Структура обязательна:
🎯 Ниша и сегментация — как ты разбил нишу (если разбивал) и где в основном искал.
🔥 Боли, отсортированные по убыванию силы (от 10 к 1). На каждую боль:
  - **[X/10] Название боли** — суть в 1-2 предложениях.
  - 📍 Источник(и): конкретные ссылки или чёткое описание находки (площадка, примерная дата, кто говорит).
  - 📈 Почему такая оценка: частота, накал, готовность платить — коротко, по существу.
  - 🏚️ Существующие решения: что уже есть (если есть) и почему не закрывает боль полностью.
  - 🛠️ 3 быстрых варианта решения соло-вайбкодингом без бюджета: конкретные, а не абстрактные («Telegram-бот с шаблоном X», не «сделать приложение»). Если реалистичного варианта нет — прямо скажи, что эта боль не про соло-вайбкодинг, и почему.
❓ Что не нашлось / слабые сигналы — какие боли рассматривал, но не смог подтвердить достаточно — честно исключи их из основного списка, но упомяни здесь одной строкой, чтобы Давид знал, что искал и не нашёл.
📚 Источники — список ссылок в конце, сгруппированный по болям.

Если по нише в целом наскрёб мало материала — так и напиши прямым текстом в начале отчёта, не растягивай два реальных сигнала на десять пунктов. Лучше честные три боли, чем десять из которых семь придуманы."""

async def _run_pain_miner(niche: str) -> str:
    key = get_active_key("anthropic")
    client = anthropic.Anthropic(api_key=key, base_url="https://api.oneprovider.dev")
    model = get_model_for_route("normal")
    pain_tools = [t for t in orchestrator.tools if t["name"] == "web_search"]

    current_messages = [{
        "role": "user",
        "content": f"Ниша для поиска болей: «{niche}». Разбери по методике из системного промпта."
    }]

    total_in = total_out = 0
    report_text = ""
    for i in _iterations():
        response = client.messages.create(
            model=model,
            max_tokens=8000,
            system=PAIN_MINER_SYSTEM,
            tools=pain_tools,
            messages=current_messages,
        )
        total_in += getattr(response.usage, "input_tokens", 0)
        total_out += getattr(response.usage, "output_tokens", 0)

        text_parts, tool_calls = [], []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls or response.stop_reason == "end_turn":
            report_text = "\n".join(text_parts)
            break

        tool_results = []
        for call in tool_calls:
            result = await execute_tool(call.name, call.input, user_id=None, bot=None)
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": result})
        current_messages.append({"role": "assistant", "content": response.content})
        current_messages.append({"role": "user", "content": tool_results})
    else:
        report_text = report_text or "⚠️ Разбор не завершён за отведённое число шагов — не хватило итераций."

    est_usd = round(total_in * COST_INPUT_PER_TOKEN + total_out * COST_OUTPUT_PER_TOKEN, 6)
    write_agent_log({
        "ts": datetime.now().isoformat(),
        "type": "request",
        "route": "pain_mining",
        "model": model,
        "iterations": i + 1,
        "in_tokens": total_in,
        "out_tokens": total_out,
        "est_usd": est_usd,
    })
    return report_text

async def mine_pains(niche: str, user_id: int, bot) -> None:
    if not get_active_key("anthropic"):
        if bot:
            try:
                await bot.send_message(user_id, "❌ Нет ключа anthropic, не могу запустить поиск болей.")
            except Exception:
                pass
        return

    if bot:
        try:
            await bot.send_message(user_id, f"🔎 Ищу боли в нише «{niche}» — это может занять несколько минут…")
        except Exception:
            pass

    try:
        report_text = await _run_pain_miner(niche)
    except Exception as e:
        logger.error(f"mine_pains: анализ провален для «{niche}»: {e}")
        if bot:
            try:
                await bot.send_message(user_id, f"❌ Не удалось разобрать нишу «{niche}»: {e}")
            except Exception:
                pass
        return

    today = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify_niche(niche)
    rel_path = f"Боли/{slug}"
    fpath = os.path.join("/obsidian", rel_path + ".md")
    note = f"---\nниша: {niche}\nдата_разбора: {today}\n---\n\n# Боли ниши: {niche}\n\n_Разбор {today}_\n\n{report_text}\n"
    try:
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(note)
    except Exception as e:
        logger.error(f"mine_pains: не удалось записать {fpath}: {e}")

    if bot:
        try:
            await _send_chunked(
                bot, user_id,
                f"🔥 Боли ниши «{niche}»\n\n{report_text}\n\n📁 Сохранено в Obsidian: {rel_path}.md"
            )
        except Exception as e:
            logger.error(f"mine_pains: не удалось отправить отчёт в Telegram: {e}")

async def transcribe_groq(file_bytes: bytes) -> str:
    key = get_active_key("groq")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "ru", "response_format": "text"}
            )
            if resp.status_code == 200:
                return resp.text.strip()
            return None
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return None

# ===== КОМАНДЫ =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Привет, Давид!\n\n"
        "Я твой ИИ-агент. Просто говори что нужно — "
        "сам разберусь каким инструментом.\n\n"
        "📅 Календарь  •  🔍 Поиск  •  🎤 Голос  •  🖥 Bash\n\n"
        "Все настройки и статистика — в меню ниже:",
        reply_markup=kb_main()
    )

async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /addkey anthropic KEY")
        return
    provider, key_value = args[0].lower(), args[1]
    if add_key(provider, key_value):
        await update.message.reply_text(f"✅ Ключ для {provider} добавлен")
    else:
        await update.message.reply_text(f"❌ Неизвестный провайдер: {provider}")

async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    text = await _get_keys_text()
    await update.message.reply_text("🔑 Статус ключей:\n\n" + text)

async def _balance_oneprovider(client: httpx.AsyncClient) -> str:
    # Баланс отдаёт отдельный поддомен dashboard.oneprovider.dev, а не
    # api.oneprovider.dev (на котором висит /v1/messages) — эндпоинт и
    # схема ответа сменились, см. https://oneprovider.dev/docs/api/usage-billing
    key = get_active_key("anthropic")
    if not key:
        return "❌ Claude (oneprovider.dev): нет ключа"
    try:
        resp = await client.get(
            "https://dashboard.oneprovider.dev/v1/dashboard/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return f"❌ Claude (oneprovider.dev): ошибка — HTTP {resp.status_code}"
        d = resp.json()
        balance = d.get("balance_usd", 0)
        quota = d.get("quota_usd", 0)
        used = d.get("used_usd", 0)
        expires = (d.get("expires_at") or "")[:10]
        active = d.get("is_active", True)

        lines = [
            f"🤖 Claude (oneprovider.dev):",
            f"  Баланс: ${balance:.2f} / ${quota:.2f} (потрачено ${used:.2f})",
            f"  Истекает: {expires or '—'}",
        ]
        if not active:
            lines.append("  ⚠️ Ключ приостановлен/отключён")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Claude (oneprovider.dev): ошибка — {e}"

async def _balance_deepseek(client: httpx.AsyncClient) -> str:
    key = get_active_key("deepseek")
    if not key:
        return "❌ DeepSeek: нет ключа"
    try:
        resp = await client.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        d = resp.json()
        if not d.get("is_available"):
            return "❌ DeepSeek: баланс недоступен"
        lines = ["💚 DeepSeek:"]
        for b in d.get("balance_infos", []):
            lines.append(
                f"  Итого: {b.get('total_balance')} {b.get('currency')}"
                f" (пополненных: {b.get('topped_up_balance')},"
                f" бонусных: {b.get('granted_balance')})"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ DeepSeek: ошибка — {e}"

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.chat.send_action("typing")
    text = await _get_balance_text()
    await update.message.reply_text("💰 Баланс провайдеров\n\n" + text)

async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    args = context.args
    if not args:
        current = model_override.get(uid, "auto")
        label, hint = MODEL_MODES.get(current, (current, "задано вручную"))
        lines = [f"Текущий режим: {label} — {hint}", "", "Доступные режимы:"]
        lines += [f"/use {k} — {h}" for k, (_l, h) in MODEL_MODES.items()]
        await update.message.reply_text("\n".join(lines))
        return
    mode = args[0].lower()
    if mode == "auto":
        model_override.pop(uid, None)
        await update.message.reply_text("✅ Авто-роутинг включён (роутер выбирает модель по сложности)")
    elif mode in MODEL_MODES:
        model_override[uid] = mode
        label, hint = MODEL_MODES[mode]
        await update.message.reply_text(f"✅ Зафиксировано: {label} — {hint}\nСброс: /use auto")
    else:
        await update.message.reply_text("Варианты: " + " | ".join(MODEL_MODES))

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    try:
        text = await _get_stats_text()
        await update.message.reply_text("📊 Статистика\n\n" + text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка чтения лога: {e}")

async def cmd_mine_pains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    niche = " ".join(context.args).strip()
    if not niche:
        await update.message.reply_text("Использование: /mine_pains <ниша>\nНапример: /mine_pains бухгалтерия для ИП на УСН")
        return
    asyncio.create_task(mine_pains(niche, update.effective_user.id, context.bot))

async def cmd_sandbox_apply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    text = await _do_sandbox_apply(update.effective_user.id)
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_sandbox_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    text = _do_sandbox_cancel(update.effective_user.id)
    await update.message.reply_text(text)

async def cmd_confirm_bash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    pending = bash_pending.pop(uid, None)
    if not pending:
        await update.message.reply_text("Нет команды, ожидающей подтверждения.")
        return
    from modules.bash.handler import run_confirmed
    output = run_confirmed(pending["command"])
    await update.message.reply_text(f"✅ Выполнено:\n\n{output}")

async def cmd_cancel_bash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    pending = bash_pending.pop(uid, None)
    if not pending:
        await update.message.reply_text("Нет команды, ожидающей подтверждения.")
        return
    await update.message.reply_text(f"❌ Отменено: {pending['command']!r}")

async def cmd_devmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global dev_mode
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args
    if args and args[0].lower() == "on":
        dev_mode = True
        await update.message.reply_text(
            "🔧 Режим разработки включён.\n"
            "Доступен propose_module — агент может предлагать новые модули.\n\n"
            "/devmode off — вернуться в обычный режим"
        )
    elif args and args[0].lower() == "off":
        dev_mode = False
        await update.message.reply_text("💬 Обычный режим. propose_module скрыт от агента.")
    else:
        status = "🔧 разработка" if dev_mode else "💬 обычный"
        await update.message.reply_text(
            f"Текущий режим: {status}\n\n"
            "/devmode on — режим разработки (доступ к propose_module)\n"
            "/devmode off — обычный диалоговый режим"
        )

async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("⏱ Rate limits\n\n" + _get_limits_text())

# ── session auto-save ─────────────────────────────────────────────────────────

def _history_to_text(history: list) -> str:
    lines = []
    for m in history[-30:]:
        role = m["role"].upper()
        content = m.get("content", "")
        if isinstance(content, str):
            lines.append(f"{role}: {content[:300]}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    lines.append(f"{role}: {block['text'][:300]}")
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)[:120]
                    lines.append(f"{role} [tool={block['name']}]: {args}")
    return "\n".join(lines)

async def _save_session_to_memory(uid: int, history: list, bot=None) -> None:
    if not history:
        return
    now = datetime.now()
    user_turns = sum(1 for m in history if m["role"] == "user")

    conv_text = _history_to_text(history)
    key = get_active_key("deepseek")
    facts_md = ""
    if key:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "deepseek-v4-flash",
                        "messages": [
                            {"role": "system", "content": (
                                "Из диалога ниже извлеки факты, достойные долгосрочного запоминания: "
                                "решения по проектам, новые факты о пользователе, технические выводы, договорённости. "
                                "НЕ включай: временные данные, общие разговоры, то что легко найти заново. "
                                "Если ничего ценного нет — ответь одним словом: НЕЧЕГО\n"
                                "Иначе — краткое markdown-резюме (3-7 пунктов bullet, начинать с «- »)."
                            )},
                            {"role": "user", "content": conv_text},
                        ],
                        "max_tokens": 400,
                        "temperature": 0.1,
                    }
                )
            facts_md = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Session extraction error: {e}")
            facts_md = f"- (извлечение недоступно: {e})"
    else:
        facts_md = f"- {user_turns} сообщений (DeepSeek недоступен)"

    if facts_md.upper() == "НЕЧЕГО":
        session_saved.add(uid)
        logger.info(f"Session checked for uid={uid}: nothing to remember")
        return

    slug = f"session-{now.strftime('%Y-%m-%d-%H-%M')}"
    body = (
        f"# Сессия {now.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{facts_md}\n\n"
        f"_Автоматически извлечено из {user_turns} сообщений_"
    )

    if not MEMORY_API_KEY:
        logger.warning("MEMORY_API_KEY not set — session not saved")
        return

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MEMORY_API_URL}/memories",
                headers={"Authorization": f"Bearer {MEMORY_API_KEY}"},
                json={
                    "slug": slug,
                    "type": "project",
                    "description": f"Автосессия {now.strftime('%Y-%m-%d %H:%M')}, {user_turns} сообщений",
                    "body_md": body,
                    "tags": ["session", "auto"],
                },
            )
        if resp.status_code in (200, 201):
            session_saved.add(uid)
            logger.info(f"Session saved to Memory API for uid={uid}, slug={slug}")
            if bot:
                await bot.send_message(uid, f"💾 Сессия сохранена ({user_turns} сообщений)")
        else:
            logger.error(f"Session save API error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Session save to Memory API failed: {e}")

async def _inactivity_watcher() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for uid, ts in list(last_activity.items()):
            if uid in session_saved:
                continue
            if now - ts < INACTIVITY_TIMEOUT:
                continue
            history = conversation_history.get(uid, [])
            if not history:
                session_saved.add(uid)
                continue
            bot = _app.bot if _app else None
            await _save_session_to_memory(uid, history, bot=bot)

async def _scheduler_watcher() -> None:
    from modules.scheduler.handler import load_jobs, save_jobs, compute_next_run, moscow_now
    while True:
        await asyncio.sleep(60)
        try:
            jobs = load_jobs()
            now = moscow_now()
            changed = False
            for job in jobs:
                try:
                    next_run = datetime.fromisoformat(job["next_run"])
                except Exception:
                    continue
                if now < next_run:
                    continue
                uid = job.get("user_id") or ALLOWED_USER_ID
                label, task = job["label"], job["task"]
                logger.info(f"Scheduler: '{label}' uid={uid}")
                try:
                    if _app:
                        await _app.bot.send_message(uid, f"⏰ <b>{label}</b>", parse_mode="HTML")
                    result, model = await agent_reply(uid, task, bot=_app.bot if _app else None)
                    if _app:
                        await send_reply(_app.bot, f"{_model_label(uid, model)}\n\n{result}", chat_id=uid)
                except Exception as e:
                    logger.error(f"Scheduler job '{label}' error: {e}")
                if job["schedule_type"] == "once":
                    job["_done"] = True
                else:
                    nxt = compute_next_run(job["schedule_type"], job["schedule_value"])
                    if nxt:
                        job["next_run"] = nxt.isoformat(timespec="minutes")
                changed = True
            if changed:
                save_jobs([j for j in jobs if not j.get("_done")])
        except Exception as e:
            logger.error(f"Scheduler watcher error: {e}")

def _kwork_card_text(want: dict, classification: dict, draft: dict, flagged: bool) -> str:
    import html as _html
    from modules.kwork import handler as kwork_handler

    title = _html.escape((want.get("name") or "").strip()[:200])
    desired = kwork_handler._budget(want.get("priceLimit"))
    possible = kwork_handler._budget(want.get("possiblePriceLimit"))
    budget_str = (
        f"желаемый {desired} ₽ / допустимый {possible} ₽" if desired and possible
        else f"{desired or possible} ₽" if (desired or possible)
        else "не указан"
    )
    cat_name = kwork_handler.KWORK_CATEGORIES.get(
        int(want.get("category_id", 0)), "?"
    )
    url = kwork_handler.WANT_URL.format(want["id"])

    desc_raw = (want.get("description") or "").strip()
    desc = _html.escape(desc_raw[:800] + ("…" if len(desc_raw) > 800 else ""))

    reasoning = _html.escape(classification.get("reasoning", ""))
    red_flags = classification.get("red_flags") or []
    flags_line = f"\n🚩 {_html.escape(', '.join(red_flags))}" if red_flags else ""

    warn = "\n\n⚠️ <b>В тексте похоже на контактные данные — проверь перед отправкой.</b>" if flagged else ""

    return (
        f"💼 <b>{title}</b>\n"
        f"📁 {_html.escape(cat_name)} · 💰 {budget_str}\n"
        f"🔗 {url}\n\n"
        f"📄 <i>{desc}</i>\n\n"
        f"🎯 <b>{_html.escape(classification.get('matched_skill', ''))}</b> "
        f"({_html.escape(classification.get('tier', ''))}, "
        f"confidence {classification.get('confidence')})\n"
        f"{reasoning}{flags_line}\n\n"
        f"💸 <b>Моё предложение: {draft['price']} ₽, срок {draft['term_days']} дн.</b>\n\n"
        f"📝 <b>Черновик отклика:</b>\n{_html.escape(draft['text'])}"
        f"{warn}"
    )

async def _send_kwork_card(want: dict, classification: dict, draft: dict, flagged: bool) -> None:
    if not _app:
        return
    want_id = want["id"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить", callback_data=f"kwork:send:{want_id}"),
        InlineKeyboardButton("✏️ Цена", callback_data=f"kwork:price:{want_id}"),
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"kwork:skip:{want_id}"),
    ]])
    try:
        await _app.bot.send_message(
            ALLOWED_USER_ID,
            _kwork_card_text(want, classification, draft, flagged),
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Kwork card send error want={want_id}: {e}")

async def _kwork_watcher() -> None:
    if os.getenv("KWORK_WATCHER_ENABLED", "true").lower() == "false":
        logger.info("Kwork watcher отключён (KWORK_WATCHER_ENABLED=false)")
        return
    from modules.kwork import handler as kwork_handler
    while True:
        try:
            api_key = get_active_key("anthropic")
            if not api_key:
                logger.warning("Kwork watcher: нет активного ключа anthropic, пропускаю тик")
            else:
                wants = await kwork_handler.fetch_new_wants()
                for want in wants:
                    want_id = want.get("id")
                    # ошибки изолируем по заявке: одна упавшая не должна
                    # ронять остаток пачки. mark_seen — только после обработки;
                    # не обработали (LLM-сбой, рестарт) — вернётся в следующем тике.
                    try:
                        classification = await kwork_handler.classify_want(want, api_key)
                        if not classification:
                            continue
                        kwork_handler._log_event({
                            "event": "classified",
                            "want_id": want_id,
                            "classification": classification,
                        })
                        if not classification.get("relevant") or classification.get("confidence", 0) < 65:
                            kwork_handler.mark_seen(want_id)
                            continue

                        draft = await kwork_handler.generate_response(want, classification, api_key)
                        if not draft:
                            continue
                        flagged = kwork_handler.looks_like_contact_info(draft["text"])

                        kwork_handler.pending_drafts[want_id] = {
                            "want": want,
                            "classification": classification,
                            "draft": draft["text"],
                            "price": draft["price"],
                            "term_days": draft["term_days"],
                            "flagged": flagged,
                            "status": "pending",
                        }
                        kwork_handler.save_drafts()
                        kwork_handler._log_event({
                            "event": "draft_ready", "want_id": want_id, "flagged": flagged,
                            "price": draft["price"], "term_days": draft["term_days"],
                        })
                        await _send_kwork_card(want, classification, draft, flagged)
                        kwork_handler.mark_seen(want_id)
                    except Exception as e:
                        logger.error(f"Kwork watcher want={want_id} error: {e}")
        except Exception as e:
            import traceback as _tb
            logger.error(f"Kwork watcher error: {e}\n{_tb.format_exc()}")
        await asyncio.sleep(300)

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    history = conversation_history.get(uid, [])
    if history and uid not in session_saved:
        await update.message.reply_text("💾 Сохраняю сессию…")
        await _save_session_to_memory(uid, history)
    conversation_history[uid] = []
    last_activity.pop(uid, None)
    session_saved.discard(uid)
    await update.message.reply_text("✅ История очищена")

# ── data helpers ────────────────────────────────────────────────────────────────

async def _get_keys_text() -> str:
    data = load_keys()
    lines = []
    for provider, info in data["providers"].items():
        keys = info.get("keys", [])
        active = sum(1 for k in keys if k.get("active", True))
        total = len(keys)
        emoji = "✅" if active > 0 else "❌"
        lines.append(f"{emoji} {provider}: {active}/{total} активных")
    active_search = next(
        (name for name in SEARCH_PROVIDER_NAMES if get_active_key(name)), None
    )
    lines.append(f"\n🔍 Активный поиск: {active_search or 'нет — /addkey serper KEY'}")
    return "\n".join(lines)

async def _get_balance_text() -> str:
    async with httpx.AsyncClient() as client:
        parts = await asyncio.gather(
            _balance_oneprovider(client),
            _balance_deepseek(client),
        )
    return "\n\n".join(parts)

def _get_limits_text() -> str:
    now = time.time()
    mode_str = "🔧 разработка" if dev_mode else "💬 обычный"
    lines = [f"Режим: {mode_str}", "Лимиты отключены — ниже просто счётчики.\n"]
    for key in RATE_TRACKED:
        window = [t for t in _RATE_WINDOWS.get(key, []) if now - t < 86400]
        hour_count = sum(1 for t in window if now - t < 3600)
        day_count = len(window)
        lines.append(f"• {key}: {hour_count} за час, {day_count} за день")
    return "\n".join(lines)

async def _get_stats_text() -> str:
    today = datetime.now().date().isoformat()
    week_ago = (datetime.now().date() - __import__("datetime").timedelta(days=7)).isoformat()
    day_requests = 0; day_in = 0; day_out = 0; day_usd = 0.0; day_tools: Counter = Counter()
    week_requests = 0; week_in = 0; week_out = 0; week_usd = 0.0
    if not os.path.exists(AGENT_LOG_FILE):
        return "Лог пуст — данных ещё нет."
    with open(AGENT_LOG_FILE) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            day = e.get("ts", "")[:10]
            if e.get("type") == "request":
                if day == today:
                    day_requests += 1
                    day_in += e.get("in_tokens", 0)
                    day_out += e.get("out_tokens", 0)
                    day_usd += e.get("est_usd", 0.0)
                if day >= week_ago:
                    week_requests += 1
                    week_in += e.get("in_tokens", 0)
                    week_out += e.get("out_tokens", 0)
                    week_usd += e.get("est_usd", 0.0)
            elif e.get("type") == "tool" and day == today:
                day_tools[e.get("name", "?")] += 1
    tools_str = ", ".join(f"{t}: {c}" for t, c in day_tools.most_common()) or "—"
    return (
        f"Сегодня ({today}):\n"
        f"• Запросов: {day_requests}\n"
        f"• Tool calls: {sum(day_tools.values())} ({tools_str})\n"
        f"• Токены: {day_in:,} вх / {day_out:,} исх\n"
        f"• ~${day_usd:.4f}\n\n"
        f"За 7 дней:\n"
        f"• Запросов: {week_requests}\n"
        f"• Токены: {week_in:,} вх / {week_out:,} исх\n"
        f"• ~${week_usd:.4f}"
    )

async def _do_sandbox_apply(user_id: int) -> str:
    proposal = sandbox_pending.pop(user_id, None)
    if not proposal:
        return "Нет ожидающего предложения модуля."
    if not proposal["check_ok"]:
        return "❌ Нельзя применить: автопроверки не прошли."
    module_name = proposal["module_name"]
    sandbox_dir = proposal["sandbox_dir"]
    prod_dir = f"/app/modules/{module_name}"
    try:
        if os.path.exists(prod_dir):
            shutil.rmtree(prod_dir)
        shutil.copytree(sandbox_dir, prod_dir)
    except Exception as e:
        return f"❌ Ошибка копирования: {e}"
    ok = orchestrator.reload_module(module_name)
    if ok:
        return f"✅ Модуль <b>{module_name}</b> активен без перезапуска!"
    return "⚠️ Файлы скопированы, но hot-reload не удался — проверь логи."

def _do_sandbox_cancel(user_id: int) -> str:
    proposal = sandbox_pending.pop(user_id, None)
    if not proposal:
        return "Нет ожидающего предложения."
    try:
        shutil.rmtree(os.path.dirname(proposal["sandbox_dir"]), ignore_errors=True)
    except Exception:
        pass
    return f"❌ Предложение модуля «{proposal['module_name']}» отменено."

# ── keyboard builders ───────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Мониторинг", callback_data="section:monitoring"),
            InlineKeyboardButton("⚙️ Настройки",  callback_data="section:settings"),
        ],
        [
            InlineKeyboardButton("🔧 Dev-режим",  callback_data="section:dev"),
            InlineKeyboardButton("🗑 Служебное",  callback_data="section:util"),
        ],
    ])

def kb_back(section: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"section:{section}")]])

def kb_monitoring() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Баланс",     callback_data="action:balance"),
            InlineKeyboardButton("🔑 Ключи",      callback_data="action:keys"),
        ],
        [
            InlineKeyboardButton("📈 Статистика", callback_data="action:stats"),
            InlineKeyboardButton("⏱ Лимиты",     callback_data="action:limits"),
        ],
        [InlineKeyboardButton("← Назад", callback_data="section:main")],
    ])

def kb_settings(uid: int) -> InlineKeyboardMarkup:
    current = model_override.get(uid, "auto")
    rows: list = []
    row: list = []
    for key, (label, _hint) in MODEL_MODES.items():
        btn = InlineKeyboardButton(f"✅ {label}" if key == current else label, callback_data=f"use:{key}")
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔑 Добавить ключ…", callback_data="hint:addkey")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="section:main")])
    return InlineKeyboardMarkup(rows)

def kb_dev() -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton("🔴 Выключить Dev mode", callback_data="action:devmode:off")
        if dev_mode else
        InlineKeyboardButton("🟢 Включить Dev mode",  callback_data="action:devmode:on")
    )
    return InlineKeyboardMarkup([
        [toggle],
        [
            InlineKeyboardButton("✅ Применить модуль", callback_data="action:sandbox_apply"),
            InlineKeyboardButton("❌ Отменить",         callback_data="action:sandbox_cancel"),
        ],
        [InlineKeyboardButton("← Назад", callback_data="section:main")],
    ])

def kb_util() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Очистить историю", callback_data="action:clear_confirm")],
        [InlineKeyboardButton("← Назад", callback_data="section:main")],
    ])

def kb_clear_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, очистить", callback_data="action:clear_yes"),
        InlineKeyboardButton("❌ Отмена",       callback_data="section:util"),
    ]])

# ── menu command & callback ─────────────────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("🤖 Главное меню", reply_markup=kb_main())

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global dev_mode
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer()
        return
    await query.answer()
    data = query.data
    uid = query.from_user.id

    async def edit(text: str, kb=None):
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass

    if data.startswith("kwork:"):
        from modules.kwork import handler as kwork_handler
        _, action, want_id_str = data.split(":", 2)
        want_id = int(want_id_str)
        if action == "send":
            await edit("⏳ Отправляю отклик…")
            ok, msg = await kwork_handler.send_response_after_click(want_id)
            await edit(msg)
        elif action == "skip":
            kwork_handler.skip_want(want_id)
            await edit("⏭ Пропущено.")
        elif action == "price":
            if want_id not in kwork_handler.pending_drafts:
                await query.answer("Черновик не найден", show_alert=True)
                return
            # ForceReply: ответ на это сообщение перехватит handle_message
            # (маркер «цена для заявки <id>» в тексте), минуя LLM-агента
            await context.bot.send_message(
                ALLOWED_USER_ID,
                f"✏️ Новая цена для заявки {want_id} — ответь на это сообщение числом в рублях.",
                reply_markup=ForceReply(selective=True),
            )
        return

    if data == "section:main":
        await edit("🤖 <b>Главное меню</b>", kb_main())
    elif data == "section:monitoring":
        await edit("📊 <b>Мониторинг</b>", kb_monitoring())
    elif data == "section:settings":
        await edit(f"⚙️ <b>Настройки</b>\n\n{_mode_line(uid)}", kb_settings(uid))
    elif data == "section:dev":
        status = "🟢 включён" if dev_mode else "⚪️ выключен"
        await edit(f"🔧 <b>Dev-режим</b>  —  {status}", kb_dev())
    elif data == "section:util":
        await edit("🗑 <b>Служебное</b>", kb_util())
    elif data == "action:balance":
        await edit("⏳ Запрашиваю баланс…")
        text = await _get_balance_text()
        await edit(f"💰 <b>Баланс провайдеров</b>\n\n{text}", kb_back("monitoring"))
    elif data == "action:keys":
        text = await _get_keys_text()
        await edit(f"🔑 <b>Статус ключей</b>\n\n{text}", kb_back("monitoring"))
    elif data == "action:stats":
        text = await _get_stats_text()
        await edit(f"📈 <b>Статистика</b>\n\n{text}", kb_back("monitoring"))
    elif data == "action:limits":
        text = _get_limits_text()
        await edit(f"⏱ <b>Rate limits</b>\n\n{text}", kb_back("monitoring"))
    elif data.startswith("use:"):
        mode = data[4:]
        if mode == "auto":
            model_override.pop(uid, None)
        else:
            model_override[uid] = mode
        await edit(f"⚙️ <b>Настройки</b>\n\n{_mode_line(uid)}", kb_settings(uid))
    elif data == "hint:addkey":
        await query.answer(
            "Отправь боту:\n/addkey anthropic KEY\n/addkey deepseek KEY\n/addkey groq KEY",
            show_alert=True
        )
    elif data.startswith("action:devmode:"):
        dev_mode = data.endswith(":on")
        status = "🟢 включён" if dev_mode else "⚪️ выключен"
        await edit(f"🔧 <b>Dev-режим</b>  —  {status}", kb_dev())
    elif data == "action:sandbox_apply":
        text = await _do_sandbox_apply(uid)
        await edit(text, kb_back("dev"))
    elif data == "action:sandbox_cancel":
        text = _do_sandbox_cancel(uid)
        await edit(text, kb_back("dev"))
    elif data == "action:clear_confirm":
        await edit("⚠️ <b>Очистить историю?</b>\n\nВсе сообщения диалога будут удалены.", kb_clear_confirm())
    elif data == "action:clear_yes":
        conversation_history[uid] = []
        await edit("✅ История диалога очищена.", kb_back("main"))

def _quote_context(message) -> str:
    """Текст сообщения, на которое отвечает/которое пересылает Давид — LLM его не видит без этого."""
    parts = []
    quoted = message.reply_to_message
    if quoted:
        quoted_text = quoted.text or quoted.caption
        if quoted_text:
            who = "бот" if (quoted.from_user and quoted.from_user.is_bot) else "Давид"
            parts.append(f'[Ответ на предыдущее сообщение ({who}): "{quoted_text}"]')
    if message.forward_origin:
        parts.append("[Это пересланное сообщение (репост) — Давид не писал текст сам, реагируй на содержимое]")
    return "\n".join(parts)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    last_activity[uid] = time.time()
    session_saved.discard(uid)
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update.message.chat, stop_typing))
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        text = await transcribe_groq(bytes(file_bytes))
        if not text:
            await update.message.reply_text("❌ Не удалось распознать голос")
            return
        await update.message.reply_text(f"🎤 {text}")
        if tmux_mode_on():
            ok = send_to_tmux(text, source="voice")
            await update.message.reply_text(
                "➡️ ушло в сессию Claude Code" if ok
                else "❌ мост недоступен — в сессию не ушло"
            )
            return
        quote = _quote_context(update.message)
        if quote:
            text = f"{quote}\n\n{text}"
        result, model = await agent_reply(uid, text, bot=context.bot)
    finally:
        stop_typing.set()
        typing_task.cancel()
    await send_reply(update.message, f"{_model_label(uid, model)}\n\n{result}")

async def _keep_typing(chat, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await chat.send_action("typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    last_activity[uid] = time.time()
    session_saved.discard(uid)
    text = update.message.text

    # ответ на ForceReply «Новая цена для заявки N» — правка цены черновика
    # Kwork, не сообщение для LLM-агента
    reply_to = update.message.reply_to_message
    if reply_to and reply_to.text:
        m = re.search(r"цена для заявки (\d+)", reply_to.text)
        if m:
            from modules.kwork import handler as kwork_handler
            want_id = int(m.group(1))
            draft = kwork_handler.pending_drafts.get(want_id)
            if not draft:
                await update.message.reply_text("Черновик не найден (истёк или бот перезапускался).")
                return
            digits = re.sub(r"[^\d]", "", text)
            if not digits:
                await update.message.reply_text("Не понял цену — нужно число в рублях, например: 4500")
                return
            draft["price"] = int(digits)
            kwork_handler.save_drafts()
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Отправить", callback_data=f"kwork:send:{want_id}"),
                InlineKeyboardButton("✏️ Цена", callback_data=f"kwork:price:{want_id}"),
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"kwork:skip:{want_id}"),
            ]])
            await update.message.reply_text(
                f"💸 Цена для заявки {want_id} обновлена: <b>{draft['price']} ₽</b> "
                f"(срок {draft.get('term_days', '?')} дн.)",
                parse_mode="HTML", reply_markup=kb,
            )
            return

    if tmux_mode_on():
        ok = send_to_tmux(text, source="text")
        await update.message.reply_text(
            "➡️ ушло в сессию Claude Code" if ok
            else "❌ мост недоступен — в сессию не ушло"
        )
        return

    quote = _quote_context(update.message)
    if quote:
        text = f"{quote}\n\n{text}"

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update.message.chat, stop_typing))
    try:
        result, model = await agent_reply(uid, text, bot=context.bot)
    finally:
        stop_typing.set()
        typing_task.cancel()
    await send_reply(update.message, f"{_model_label(uid, model)}\n\n{result}")

MAX_PHOTO_BYTES = 5 * 1024 * 1024  # лимит Anthropic на одно base64-изображение

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    last_activity[uid] = time.time()
    session_saved.discard(uid)
    await update.message.chat.send_action("typing")

    photo = update.message.photo[-1]  # последний элемент — самое большое разрешение
    tg_file = await context.bot.get_file(photo.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    if tmux_mode_on():
        path = save_to_inbox(f"photo-{photo.file_unique_id}.jpg", file_bytes)
        note = update.message.caption or "Прислал фото."
        ok = send_to_tmux(f"{note} Файл на диске: {to_host_path(path)}", source="photo")
        await update.message.reply_text(
            f"➡️ фото в сессии: {to_host_path(path)}" if ok
            else "❌ мост недоступен — фото сохранено, но в сессию не ушло"
        )
        return

    if len(file_bytes) > MAX_PHOTO_BYTES:
        await update.message.reply_text("❌ Изображение слишком большое (>5 МБ) — уменьши и пришли ещё раз.")
        return

    caption = update.message.caption or "Опиши, что на фото, и учти это в разговоре."
    quote = _quote_context(update.message)
    if quote:
        caption = f"{quote}\n\n{caption}"

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(file_bytes).decode(),
            },
        },
        {"type": "text", "text": caption},
    ]

    # DeepSeek (route simple) не умеет в изображения — форсируем ветку с Claude
    forced_route = "heavy" if model_override.get(uid) == "opus" else "normal"

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update.message.chat, stop_typing))
    try:
        result, model = await agent_reply(uid, caption, bot=context.bot, content=content, force_route=forced_route)
    finally:
        stop_typing.set()
        typing_task.cancel()
    await send_reply(update.message, f"{_model_label(uid, model)}\n\n{result}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Файлы из Telegram. В режиме моста кладём на диск и говорим сессии путь."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    last_activity[uid] = time.time()
    session_saved.discard(uid)

    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_TMUX_FILE_BYTES:
        await update.message.reply_text("❌ Файл больше 50 МБ — Telegram столько не отдаст боту.")
        return

    if not tmux_mode_on():
        await update.message.reply_text(
            "📎 Файл принят, но режим моста выключен.\n"
            "/tmux on — включить, тогда файлы уходят прямо в сессию Claude Code."
        )
        return

    await update.message.chat.send_action("upload_document")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.error(f"handle_document: скачать не вышло: {e}")
        await update.message.reply_text("❌ Не смог скачать файл из Telegram.")
        return

    path = save_to_inbox(doc.file_name or f"file-{doc.file_unique_id}", file_bytes)
    host = to_host_path(path)
    note = update.message.caption or "Прислал файл."
    ok = send_to_tmux(f"{note} Файл на диске: {host}", source="document")
    await update.message.reply_text(
        f"➡️ файл в сессии: {host} ({len(file_bytes) // 1024} КБ)" if ok
        else f"❌ мост недоступен. Файл лежит здесь: {host}"
    )

async def cmd_tmux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    args = context.args
    if args and args[0].lower() == "on":
        set_tmux_mode(True)
        await update.message.reply_text(
            f"🔗 Мост включён — сессия <b>{TMUX_SESSION}</b>.\n\n"
            "Голос, текст, фото и файлы уходят прямо в Claude Code, "
            "собственный агент бота молчит.\n\n"
            "/tmux off — вернуть обычный режим",
            parse_mode="HTML",
        )
    elif args and args[0].lower() == "off":
        set_tmux_mode(False)
        await update.message.reply_text("💬 Мост выключен. Отвечает снова агент бота.")
    else:
        pending = 0
        try:
            pending = len([f for f in os.listdir(INBOX_SPOOL) if f.endswith(".json")])
        except OSError:
            pass
        state = f"🔗 включён (сессия {TMUX_SESSION})" if tmux_mode_on() else "💬 выключен"
        await update.message.reply_text(
            f"Мост в Claude Code: {state}\n"
            f"В очереди спула: {pending}\n\n"
            "/tmux on — слать всё в живую сессию\n"
            "/tmux off — обычный режим бота"
        )

BOT_COMMANDS = [
    BotCommand("tmux",           "Мост в сессию Claude Code: on / off"),
    BotCommand("menu",           "Главное меню"),
    BotCommand("start",          "Справка"),
    BotCommand("use",            "Модель: auto / deepseek / sonnet / opus"),
    BotCommand("addkey",         "Добавить ключ: /addkey provider KEY"),
    BotCommand("keys",           "Статус API-ключей"),
    BotCommand("balance",        "Баланс всех провайдеров"),
    BotCommand("stats",          "Статистика запросов и стоимость"),
    BotCommand("limits",         "Rate limits (LLM и tool вызовы)"),
    BotCommand("devmode",        "Dev-режим: on / off"),
    BotCommand("sandbox_apply",  "Применить предложенный модуль"),
    BotCommand("sandbox_cancel", "Отменить предложенный модуль"),
    BotCommand("confirm_bash",   "Выполнить bash-команду вне whitelist"),
    BotCommand("cancel_bash",    "Отклонить bash-команду вне whitelist"),
    BotCommand("clear",          "Очистить историю диалога"),
]

async def post_init(app: Application) -> None:
    global _app
    _app = app
    await app.bot.set_my_commands(BOT_COMMANDS)
    asyncio.create_task(_inactivity_watcher())
    asyncio.create_task(_scheduler_watcher())
    asyncio.create_task(_kwork_watcher())

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("mine_pains", cmd_mine_pains))
    app.add_handler(CommandHandler("sandbox_apply", cmd_sandbox_apply))
    app.add_handler(CommandHandler("sandbox_cancel", cmd_sandbox_cancel))
    app.add_handler(CommandHandler("confirm_bash", cmd_confirm_bash))
    app.add_handler(CommandHandler("cancel_bash", cmd_cancel_bash))
    app.add_handler(CommandHandler("devmode", cmd_devmode))
    app.add_handler(CommandHandler("limits", cmd_limits))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("tmux", cmd_tmux))
    app.add_handler(CallbackQueryHandler(on_menu))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
# deploy test: Tue Jun 30 12:47:11 PM CEST 2026
