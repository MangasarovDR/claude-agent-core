import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

PRIORITY_LABEL = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪"}
STATUS_ICON    = {"pending": "○", "in_progress": "▶", "done": "✓", "cancelled": "✗"}

# Порядок квадрантов при сортировке — вторичный ключ после приоритета.
# Неразложенные задачи уходят в конец: по ним ещё нет решения.
QUADRANT_ORDER = {
    "важное-срочное":     0,
    "важное-несрочное":   1,
    "неважное-срочное":   2,
    "неважное-несрочное": 3,
}


def _sort_key(t: dict):
    """Сначала приоритет (5 → 1), внутри приоритета — по матрице Эйзенхауэра."""
    q = t.get("quadrant") or {}
    return (
        -int(t.get("priority") or 3),
        QUADRANT_ORDER.get(q.get("key"), 4),
        t.get("due_at") or "9999",
        t.get("id") or 0,
    )


def _sorted(todos: list[dict]) -> list[dict]:
    return sorted(todos, key=_sort_key)


def _api(method: str, path: str, body: dict | None = None):
    url = MEMORY_API_URL + path
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]


def _human_due(t: dict) -> str:
    """ISO-дата → человеческая: «сегодня 12:00», «завтра», «3 авг», «3 авг 2027»."""
    raw = t.get("due_at")
    if not raw:
        return ""
    try:
        d = datetime.fromisoformat(raw.replace("T", " "))
    except ValueError:
        return raw[:16]

    today = datetime.now().date()
    delta = (d.date() - today).days
    if delta == 0:
        day = "сегодня"
    elif delta == 1:
        day = "завтра"
    elif delta == -1:
        day = "вчера"
    elif d.year == today.year:
        day = f"{d.day} {_MONTHS[d.month - 1]}"
    else:
        day = f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    return f"{day} {d:%H:%M}" if t.get("has_time") else day


def _fmt(t: dict, show_quadrant: bool = True) -> str:
    """
    Одна задача — одна строка, всегда в одном порядке:
    статус, приоритет, квадрант · название · срок · повтор · теги · id
    """
    icon   = PRIORITY_LABEL.get(t.get("priority", 3), "🟡")
    status = STATUS_ICON.get(t.get("status", "pending"), "○")
    quad   = ""
    if show_quadrant:
        q = t.get("quadrant")
        quad = q["icon"] if q else "❔"

    parts = [f"{status} {icon}{quad} {t['title']}"]
    due = _human_due(t)
    if due:
        parts.append(due)
    if t.get("repeat_days"):
        parts.append(f"🔁 раз в {t['repeat_days']} дн.")
    tags = t.get("tags") or []
    if tags:
        parts.append(" ".join(f"#{x}" for x in tags))
    parts.append(f"id {t['id']}")
    return " · ".join(parts)


def _normalize_date(raw: str) -> str:
    """'сегодня' / 'завтра' / '+3d' / ISO → ISO. Пустое → как есть."""
    if not raw:
        return raw
    s = raw.strip().lower()
    today = datetime.now()
    if s in ("сегодня", "today"):
        return today.strftime("%Y-%m-%d")
    if s in ("завтра", "tomorrow"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if s in ("послезавтра",):
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    if s.startswith("+") and s.endswith("d") and s[1:-1].isdigit():
        return (today + timedelta(days=int(s[1:-1]))).strftime("%Y-%m-%d")
    return raw.strip()


def _resolve(inputs: dict) -> tuple[list[dict], str | None]:
    """
    Находит задачи по id или по подстроке названия.
    Возвращает (список совпадений, сообщение об ошибке).
    Ищет только среди незакрытых — закрытую нельзя закрыть повторно.
    """
    todo_id = inputs.get("id")
    if todo_id not in (None, ""):
        try:
            return [_api("GET", f"/todos/{int(todo_id)}")], None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return [], f"Задача id {todo_id} не найдена."
            raise
        except (ValueError, TypeError):
            return [], f"Некорректный id: {todo_id!r}"

    query = (inputs.get("query") or "").strip().lower()
    if not query:
        return [], "Нужен либо id, либо query (часть названия задачи)."

    todos = _api("GET", "/todos?limit=200") or []
    open_todos = [t for t in todos if t["status"] not in ("done", "cancelled")]
    matches = [t for t in open_todos if query in t["title"].lower()]
    if not matches:
        return [], f"Не нашёл открытых задач по запросу «{query}»."
    return matches, None


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:

    try:
        # ─── Создать задачу ───────────────────────────────────────────────────
        if name == "todo_add":
            title = (inputs.get("title") or "").strip()
            if not title:
                return "Ошибка: title обязателен."

            tags = inputs.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            due_at   = _normalize_date(inputs.get("due_at") or "")
            has_time = 1 if (due_at and len(due_at) > 10) else 0

            payload = {
                "title":       title,
                "description": inputs.get("description", ""),
                "priority":    int(inputs.get("priority", 3)),
                "due_at":      due_at or None,
                "has_time":    has_time,
                "remind_at":   inputs.get("remind_at") or None,
                "tags":        tags,
                "notes":       inputs.get("notes", ""),
            }
            if inputs.get("important") is not None:
                payload["important"] = bool(inputs["important"])
            if inputs.get("urgent") is not None:
                payload["urgent"] = bool(inputs["urgent"])
            if inputs.get("repeat_days"):
                payload["repeat_days"] = int(inputs["repeat_days"])

            created = _api("POST", "/todos", payload)
            out = f"✅ Задача создана:\n{_fmt(created)}"
            q = created.get("quadrant")
            if q:
                out += f"\n\nКвадрант: {q['icon']} {q['key']} — {q['action']}"
            else:
                out += ("\n\n⚠️ Квадрант не задан. Спроси Давида, важная ли задача и срочная ли, "
                        "и проставь через todo_classify — он просил всегда раскладывать дела по матрице.")
            return out

        # ─── Показать задачи ──────────────────────────────────────────────────
        elif name == "todo_list":
            scope = (inputs.get("scope") or "open").strip().lower()

            if scope == "today":
                data = _api("GET", "/todos/today")
                blocks = []
                for key, label in (
                    ("overdue",       "⚠️ Просрочено"),
                    ("scheduled",     "🕐 По времени"),
                    ("today_no_time", "📋 На сегодня"),
                    ("anytime",       "📌 Без даты"),
                ):
                    items = data.get(key) or []
                    # «По времени» остаётся хронологическим — это расписание,
                    # остальные блоки сортируем по приоритету и матрице
                    if key != "scheduled":
                        items = _sorted(items)
                    if items:
                        blocks.append(f"{label} — {len(items)}\n" + "\n".join(_fmt(t) for t in items))
                if not blocks:
                    return "На сегодня задач нет."
                return f"📅 План на {data.get('date', '?')}\n\n" + "\n\n".join(blocks)

            params = {"limit": int(inputs.get("limit", 50))}
            if scope == "done":
                params["status"] = "done"
            elif scope == "no_date":
                params["no_date"] = "true"
            if inputs.get("date"):
                params["date"] = _normalize_date(inputs["date"])[:10]
            if inputs.get("tags"):
                tg = inputs["tags"]
                params["tags"] = tg if isinstance(tg, str) else ",".join(tg)

            todos = _api("GET", f"/todos?{urllib.parse.urlencode(params)}") or []
            if scope == "open":
                todos = [t for t in todos if t["status"] not in ("done", "cancelled")]
            if not todos:
                return "Задач не найдено."
            todos = _sorted(todos)
            title = {"done": "Закрытые задачи", "no_date": "Задачи без даты"}.get(scope, "Открытые задачи")
            return f"📋 {title} — {len(todos)}\n\n" + "\n".join(_fmt(t) for t in todos)

        # ─── Матрица Эйзенхауэра ──────────────────────────────────────────────
        elif name == "todo_matrix":
            data = _api("GET", "/todos/matrix") or {}
            q, meta = data.get("quadrants", {}), data.get("meta", {})
            if not data.get("total_open"):
                return "Открытых задач нет — матрица пуста."

            order = ["важное-срочное", "важное-несрочное",
                     "неважное-срочное", "неважное-несрочное"]
            blocks = []
            for key in order:
                items = q.get(key) or []
                if not items:
                    continue
                m = meta.get(key, {})
                blocks.append(
                    f"{m.get('icon','')} {key} — {m.get('action','')} ({len(items)})\n"
                    + "\n".join(_fmt(t, show_quadrant=False) for t in _sorted(items))
                )
            unc = q.get("неклассифицировано") or []
            if unc:
                blocks.append(
                    f"❔ не разложено ({len(unc)}) — надо спросить Давида\n"
                    + "\n".join(_fmt(t, show_quadrant=False) for t in _sorted(unc))
                )
            return f"📐 Матрица Эйзенхауэра — открытых задач {data['total_open']}\n\n" + "\n\n".join(blocks)

        # ─── Поставить напоминание (в т.ч. повторяющееся) ─────────────────────
        elif name == "todo_remind":
            when = _normalize_date(inputs.get("at") or "")
            repeat = inputs.get("repeat_days")
            if not when and not repeat:
                return "Нужно указать at (когда напомнить) и/или repeat_days (период повтора)."

            matches, err = _resolve(inputs)
            if err:
                return f"⚠️ {err}"
            if len(matches) > 1:
                return ("Подходит несколько задач — уточни id:\n"
                        + "\n".join(_fmt(t) for t in matches))
            todo = matches[0]

            body = {}
            if when:
                # Без времени напоминание бессмысленно — ставим на 9 утра
                body["remind_at"] = when if len(when) > 10 else f"{when} 09:00"
            if repeat:
                body["repeat_days"] = int(repeat)
                if "remind_at" not in body and not todo.get("remind_at"):
                    from datetime import datetime as _dt
                    body["remind_at"] = (
                        _dt.now() + timedelta(days=int(repeat))
                    ).strftime("%Y-%m-%d 09:00")

            updated = _api("PATCH", f"/todos/{todo['id']}", body)
            out = f"⏰ Напоминание поставлено:\n{_fmt(updated)}\n\nКогда: {updated.get('remind_at')}"
            if updated.get("repeat_days"):
                out += f"\nПовтор: раз в {updated['repeat_days']} дн., пока задача открыта"
            return out

        # ─── Разложить задачу по матрице ──────────────────────────────────────
        elif name == "todo_classify":
            if inputs.get("important") is None or inputs.get("urgent") is None:
                return "Нужны оба параметра: important и urgent (true/false)."
            matches, err = _resolve(inputs)
            if err:
                return f"⚠️ {err}"
            if len(matches) > 1:
                return ("Подходит несколько задач — уточни id:\n"
                        + "\n".join(_fmt(t) for t in matches))
            updated = _api("PATCH", f"/todos/{matches[0]['id']}", {
                "important": bool(inputs["important"]),
                "urgent":    bool(inputs["urgent"]),
            })
            qd = updated.get("quadrant") or {}
            return (f"📐 Разложено: {qd.get('icon','')} {qd.get('key','')} — {qd.get('action','')}\n"
                    f"{_fmt(updated)}")

        # ─── Закрыть / отменить / отложить ────────────────────────────────────
        elif name in ("todo_done", "todo_cancel", "todo_defer"):
            matches, err = _resolve(inputs)
            if err:
                return f"⚠️ {err}"
            if len(matches) > 1:
                listing = "\n".join(_fmt(t) for t in matches)
                return (
                    f"Под запрос подходит {len(matches)} задачи — уточни, какую именно "
                    f"(передай id):\n{listing}"
                )

            todo = matches[0]
            tid  = todo["id"]

            if name == "todo_done":
                body = {"notes": inputs["notes"]} if inputs.get("notes") else {}
                updated = _api("POST", f"/todos/{tid}/done", body)
                return f"✅ Отмечено выполненным:\n{_fmt(updated)}"

            if name == "todo_cancel":
                body = {"notes": inputs["notes"]} if inputs.get("notes") else {}
                updated = _api("POST", f"/todos/{tid}/cancel", body)
                return f"✗ Задача отменена:\n{_fmt(updated)}"

            to = _normalize_date(inputs.get("to") or "")
            if not to:
                return "⚠️ Для переноса нужна дата: параметр to (напр. 'завтра' или '2026-08-05')."
            body = {"to": to, "has_time": 1 if len(to) > 10 else 0}
            if inputs.get("remind_at"):
                body["remind_at"] = inputs["remind_at"]
            updated = _api("POST", f"/todos/{tid}/defer", body)
            return f"📅 Задача перенесена:\n{_fmt(updated)}"

        return f"Неизвестный инструмент тудушек: {name}"

    except urllib.error.HTTPError as e:
        err = e.read().decode()
        logger.error(f"Todos API error {e.code} on {name}: {err}")
        return f"⚠️ Ошибка API: HTTP {e.code} — {err[:200]}"
    except Exception as e:
        logger.error(f"Todos handler exception on {name}: {e}")
        return f"⚠️ Ошибка: {e}"
