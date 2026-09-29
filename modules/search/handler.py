import logging
import httpx

logger = logging.getLogger(__name__)


async def _search_serper(query: str, key: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key},
            json={"q": query, "num": 5, "hl": "ru"},
        )
        if resp.status_code == 429:
            raise RuntimeError("rate limit")
        resp.raise_for_status()
        data = resp.json()
        results = [f"• {r.get('title')}: {r.get('snippet')}" for r in data.get("organic", [])[:5]]
        if not results:
            raise RuntimeError("no results")
        return "\n".join(results)


async def _search_tavily(query: str, key: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query, "max_results": 5, "search_depth": "basic"},
        )
        if resp.status_code in (401, 403, 429):
            raise RuntimeError(f"http {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        results = [f"• {r.get('title')}: {r.get('content', '')[:200]}" for r in data.get("results", [])[:5]]
        if not results:
            raise RuntimeError("no results")
        return "\n".join(results)


async def _search_brave(query: str, key: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            params={"q": query, "count": 5, "search_lang": "ru"},
        )
        if resp.status_code in (401, 403, 429):
            raise RuntimeError(f"http {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        results = [
            f"• {r.get('title')}: {r.get('description', '')}"
            for r in data.get("web", {}).get("results", [])[:5]
        ]
        if not results:
            raise RuntimeError("no results")
        return "\n".join(results)


PROVIDER_CASCADE = [
    ("serper", _search_serper),
    ("tavily", _search_tavily),
    ("brave", _search_brave),
]


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:
    if name != "web_search":
        return f"Неизвестный инструмент: {name}"
    query = inputs["query"]
    get_active_key = kwargs.get("get_active_key")
    for provider_name, search_fn in PROVIDER_CASCADE:
        key = get_active_key(provider_name) if get_active_key else None
        if not key:
            continue
        try:
            result = await search_fn(query, key)
            logger.info(f"web_search: провайдер {provider_name}")
            return f"[{provider_name}]\n{result}"
        except Exception as e:
            logger.warning(f"web_search: {provider_name} ошибка ({e}), пробуем следующий")
    return "Ни один поисковый провайдер не ответил. Добавь ключ: /addkey serper KEY"
