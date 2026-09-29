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

# Map Obsidian folder prefix → (type, importance)
_FOLDER_MAP = {
    "идеи":    ("idea",      2),
    "знания":  ("knowledge", 3),
    "проекты": ("project",   4),
    "давид":   ("user",      4),
}

_TYPE_IMPORTANCE = {
    "feedback":  5,
    "user":      4,
    "project":   4,
    "reference": 3,
    "knowledge": 3,
    "idea":      2,
}


def _to_slug(raw: str) -> str:
    """Convert Obsidian path or arbitrary string to kebab-case slug."""
    parts = raw.replace("\\", "/").split("/")
    slug_parts = []
    for part in parts:
        part = os.path.splitext(part)[0]
        part = re.sub(r"[^\w\s-]", "", part).strip()
        part = re.sub(r"[\s_]+", "-", part).lower()
        if part:
            slug_parts.append(part)
    return "-".join(slug_parts[:5])[:80] or "memory"


def _infer_type_importance(path: str) -> tuple[str, int]:
    folder = path.lower().split("/")[0] if "/" in path else ""
    return _FOLDER_MAP.get(folder, ("project", 3))


def _api(method: str, path: str, body: dict | None = None):
    url = MEMORY_API_URL + path
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:

    if name == "memory_save":
        # ─── New interface: slug + type + description + body_md ───────────────
        if "slug" in inputs and "type" in inputs:
            slug       = inputs["slug"].strip()
            mem_type   = inputs["type"]
            description = inputs.get("description", "").strip() or slug.replace("-", " ")
            body_md    = inputs.get("body_md", "").strip()
            importance = inputs.get("importance", _TYPE_IMPORTANCE.get(mem_type, 3))
            tags       = inputs.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

        # ─── Legacy interface: filename + content (Obsidian compat) ───────────
        elif "filename" in inputs:
            raw = inputs.get("filename", "").strip()
            if raw.lower().endswith(".md"):
                raw = raw[:-3]
            content = inputs.get("content", "")
            if not raw:
                return "Ошибка: filename обязателен."

            mem_type, importance = _infer_type_importance(raw)
            slug = _to_slug(raw)

            body_clean = content
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    body_clean = content[end + 3:].lstrip("\n")
            description = ""
            for line in body_clean.splitlines():
                line = line.strip().lstrip("#").strip()
                if line and len(line) > 4:
                    description = line[:150]
                    break
            if not description:
                description = os.path.basename(raw).replace("-", " ").replace("_", " ")
            body_md = content
            tags = [raw.split("/")[0].lower()] if "/" in raw else []
        else:
            return "Ошибка: нужен либо slug+type+body_md, либо filename+content."

        if not body_md:
            return "Ошибка: body_md (или content) не может быть пустым."

        try:
            _api("POST", "/memories", {
                "slug": slug, "type": mem_type,
                "description": description[:200], "body_md": body_md,
                "importance": importance, "source": "tg-bot", "tags": tags,
            })
            # Return format that bot.py review_idea hook can parse for ideas
            if mem_type == "idea":
                return f"✅ Идея сохранена в Чертоги Разума: [{mem_type}] {slug}"
            return f"✅ Сохранено в Чертоги Разума: [{mem_type}] {slug}"
        except urllib.error.HTTPError as e:
            err = e.read().decode()
            logger.error(f"Memory API save error {e.code}: {err}")
            return f"⚠️ Ошибка сохранения: HTTP {e.code} — {err[:200]}"
        except Exception as e:
            logger.error(f"Memory API save exception: {e}")
            return f"⚠️ Ошибка: {e}"

    elif name == "memory_search":
        query = inputs.get("query", "").strip()
        if not query:
            return "Запрос пустой."
        mem_type = inputs.get("type", "")
        params = {"q": query, "limit": 8, "mode": "hybrid"}
        if mem_type:
            params["type"] = mem_type
        try:
            results = _api("GET", f"/memories/search?{urllib.parse.urlencode(params)}")
            if not results:
                return "Ничего не найдено."
            parts = []
            for r in results:
                head = f"### [{r['type']}] {r['slug']}\n_{r['description']}_\n"
                body = r.get("body_md") or ""
                # Если API вернул совпавший фрагмент — показываем его: он релевантнее
                # начала записи, которое может не иметь к запросу отношения.
                chunk = r.get("matched_chunk")
                if chunk and len(body) > 600:
                    head += f"\n📍 Совпавший фрагмент:\n{chunk}\n"
                    head += f"\n_(запись целиком — {len(body)} символов, запроси по слагу)_"
                else:
                    head += f"\n{body[:600]}"
                    if len(body) > 600:
                        head += f"\n\n_(обрезано, всего {len(body)} символов)_"
                parts.append(head)
            return f"Найдено: {len(results)}\n\n---\n\n" + "\n\n---\n\n".join(parts)
        except Exception as e:
            logger.error(f"Memory API search error: {e}")
            return f"⚠️ Ошибка поиска: {e}"

    return f"Неизвестный инструмент памяти: {name}"
