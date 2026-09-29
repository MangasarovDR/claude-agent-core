import os
import re
import json
import logging
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger(__name__)

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

_URL_RE = re.compile(r"https?://\S+")


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


def _fmt(item: dict) -> str:
    line = f"[id {item['id']}] {item['url']}"
    meta = [item.get("platform") or "?"]
    if item.get("source"):
        meta.append(item["source"])
    line += f"\n    ({', '.join(meta)})"
    if item.get("title"):
        line += f" {item['title']}"
    if item.get("note"):
        line += f"\n    💬 {item['note']}"
    tags = item.get("tags") or []
    if tags:
        line += f"\n    #{' #'.join(tags)}"
    return line


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:

    try:
        # ─── Добавить образец в корпус ────────────────────────────────────────
        if name == "corpus_add":
            raw_url = (inputs.get("url") or "").strip()
            if not raw_url:
                return "Ошибка: нужна ссылка (url)."

            # Если пришёл кусок текста со ссылкой внутри — вытащим саму ссылку
            if not raw_url.lower().startswith("http"):
                found = _URL_RE.search(raw_url)
                if not found:
                    return f"Не нашёл ссылки в «{raw_url[:80]}». Нужен URL, начинающийся с http."
                raw_url = found.group(0)
            raw_url = raw_url.rstrip(").,;»\"'")

            tags = inputs.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            payload = {
                "url":     raw_url,
                "title":   inputs.get("title", ""),
                "note":    inputs.get("note", ""),
                "excerpt": inputs.get("excerpt", ""),
                "source":  "tg-bot",
                "tags":    tags,
            }
            item = _api("POST", "/corpus", payload)

            stats = _api("GET", "/corpus/stats") or {}
            total = stats.get("total", "?")
            if item.get("duplicate"):
                return (
                    f"↩️ Эта ссылка уже в корпусе, дубликат не создан:\n{_fmt(item)}\n\n"
                    f"Всего в корпусе: {total}"
                )
            return f"📥 Добавлено в корпус:\n{_fmt(item)}\n\nВсего в корпусе: {total}"

        # ─── Показать корпус ──────────────────────────────────────────────────
        elif name == "corpus_list":
            params = {"limit": int(inputs.get("limit", 30))}
            if inputs.get("platform"):
                params["platform"] = inputs["platform"]
            if inputs.get("status"):
                params["status"] = inputs["status"]
            if inputs.get("tags"):
                tg = inputs["tags"]
                params["tags"] = tg if isinstance(tg, str) else ",".join(tg)

            items = _api("GET", f"/corpus?{urllib.parse.urlencode(params)}") or []
            if not items:
                return "Корпус пока пуст — ни одного образца не добавлено."

            stats = _api("GET", "/corpus/stats") or {}
            head = (
                f"Корпус образцов живого письма — всего {stats.get('total', len(items))}, "
                f"показано {len(items)}.\n"
                f"По площадкам: {stats.get('by_platform', {})}\n"
                f"С заметкой «чем нравится»: {stats.get('with_note', 0)}\n"
            )
            return head + "\n" + "\n".join(_fmt(i) for i in items)

        # ─── Сводка по накоплению ─────────────────────────────────────────────
        elif name == "corpus_stats":
            s = _api("GET", "/corpus/stats") or {}
            if not s.get("total"):
                return "Корпус пока пуст."
            return (
                f"📊 Корпус живого письма\n"
                f"Всего образцов: {s['total']}\n"
                f"С заметкой «чем нравится»: {s.get('with_note', 0)}\n"
                f"По площадкам: {s.get('by_platform', {})}\n"
                f"По статусу: {s.get('by_status', {})}\n"
                f"По источнику: {s.get('by_source', {})}\n"
                f"Первый: {s.get('first_added')}\n"
                f"Последний: {s.get('last_added')}"
            )

        return f"Неизвестный инструмент корпуса: {name}"

    except urllib.error.HTTPError as e:
        err = e.read().decode()
        logger.error(f"Corpus API error {e.code} on {name}: {err}")
        return f"⚠️ Ошибка API: HTTP {e.code} — {err[:200]}"
    except Exception as e:
        logger.error(f"Corpus handler exception on {name}: {e}")
        return f"⚠️ Ошибка: {e}"
