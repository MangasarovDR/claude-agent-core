import httpx
import json

def load_keys():
    with open("/app/keys.json") as f:
        return json.load(f)

async def get_deepseek_balance() -> str:
    data = load_keys()
    keys = data["providers"].get("deepseek", {}).get("keys", [])
    active_keys = [k for k in keys if k.get("active", True)]
    
    if not active_keys:
        return "❌ Нет активных ключей DeepSeek"
    
    results = []
    async with httpx.AsyncClient() as client:
        for i, key in enumerate(active_keys, 1):
            try:
                resp = await client.get(
                    "https://api.deepseek.com/user/balance",
                    headers={"Authorization": f"Bearer {key['value']}"},
                    timeout=10
                )
                d = resp.json()
                if d.get("is_available"):
                    for b in d.get("balance_infos", []):
                        currency = b.get("currency", "?")
                        total = b.get("total_balance", "?")
                        granted = b.get("granted_balance", "?")
                        topped = b.get("topped_up_balance", "?")
                        results.append(
                            f"🔑 Ключ {i}:\n"
                            f"  Всего: {total} {currency}\n"
                            f"  Бонусных: {granted} {currency}\n"
                            f"  Пополненных: {topped} {currency}"
                        )
                else:
                    results.append(f"🔑 Ключ {i}: ❌ недоступен")
            except Exception as e:
                results.append(f"🔑 Ключ {i}: ошибка — {e}")
    
    return "💰 Баланс DeepSeek:\n\n" + "\n\n".join(results)
