import ast
import json
import os
import re
import subprocess
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

SANDBOX_ROOT = "/app/sandbox"
PROD_MODULES = "/app/modules"

MODULE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _check_manifest(manifest_str: str) -> str | None:
    try:
        m = json.loads(manifest_str)
    except json.JSONDecodeError as e:
        return f"manifest.json: невалидный JSON — {e}"
    for field in ("name", "version", "tools"):
        if field not in m:
            return f"manifest.json: отсутствует поле '{field}'"
    if not isinstance(m["tools"], list) or not m["tools"]:
        return "manifest.json: поле 'tools' должно быть непустым списком"
    return None


def _check_handler(handler_code: str) -> str | None:
    # compile() ловит рантайм-синтаксические ошибки, которые пропускает ast.parse
    # (например global-после-присваивания) — см. Проекты/claude_bot/контекст.md
    try:
        compile(handler_code, "<handler>", "exec")
    except SyntaxError as e:
        return f"handler.py: синтаксическая ошибка — {e}"
    tree = ast.parse(handler_code)
    async_funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    if "handle_tool" not in async_funcs:
        return "handler.py: не найдена async def handle_tool(name, inputs, **kwargs)"
    return None


def _build_diff(sandbox_dir: str, module_name: str) -> str:
    prod_dir = os.path.join(PROD_MODULES, module_name)
    if not os.path.exists(prod_dir):
        handler = open(os.path.join(sandbox_dir, "handler.py")).read()
        manifest = open(os.path.join(sandbox_dir, "manifest.json")).read()
        return f"=== НОВЫЙ МОДУЛЬ ===\n\nmanifest.json:\n{manifest}\n\nhandler.py:\n{handler}"
    result = subprocess.run(
        ["diff", "-u",
         os.path.join(prod_dir, "handler.py"),
         os.path.join(sandbox_dir, "handler.py")],
        capture_output=True, text=True
    )
    return result.stdout or "(handler.py не изменился)"


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:
    if name != "propose_module":
        return f"Неизвестный инструмент: {name}"

    module_name = inputs.get("module_name", "").strip()
    manifest_str = inputs.get("manifest", "")
    handler_code = inputs.get("handler", "")
    reason = inputs.get("reason", "")
    user_id = kwargs.get("user_id")
    bot = kwargs.get("bot")
    sandbox_pending = kwargs.get("sandbox_pending")

    if not module_name:
        return "Ошибка: module_name обязателен."
    if not MODULE_NAME_RE.match(module_name):
        return (
            "Ошибка: module_name должен состоять только из букв/цифр/подчёркивания/дефиса "
            f"(получено: {module_name!r}) — запрос отклонён до записи в sandbox."
        )

    # Автоматические проверки
    errors = []
    err = _check_manifest(manifest_str)
    if err:
        errors.append(err)
    err = _check_handler(handler_code)
    if err:
        errors.append(err)

    check_ok = len(errors) == 0
    check_summary = "✅ Все проверки пройдены" if check_ok else "❌ Ошибки:\n" + "\n".join(f"• {e}" for e in errors)

    # Запись в sandbox
    sandbox_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    sandbox_dir = os.path.join(SANDBOX_ROOT, sandbox_id, module_name)
    os.makedirs(sandbox_dir, exist_ok=True)

    with open(os.path.join(sandbox_dir, "manifest.json"), "w") as f:
        f.write(manifest_str)
    with open(os.path.join(sandbox_dir, "handler.py"), "w") as f:
        f.write(handler_code)

    # Diff
    diff_text = _build_diff(sandbox_dir, module_name)
    diff_preview = diff_text[:2000] + ("…" if len(diff_text) > 2000 else "")

    # Сохраняем предложение
    if sandbox_pending is not None:
        sandbox_pending[user_id] = {
            "sandbox_id": sandbox_id,
            "module_name": module_name,
            "sandbox_dir": sandbox_dir,
            "check_ok": check_ok,
        }

    action = "обновление" if os.path.exists(os.path.join(PROD_MODULES, module_name)) else "новый модуль"
    msg = (
        f"🔬 Предложен {action}: <b>{module_name}</b>\n\n"
        f"Причина: {reason}\n\n"
        f"{check_summary}\n\n"
        f"<pre>{diff_preview}</pre>\n\n"
    )
    if check_ok:
        msg += "/sandbox_apply — применить без перезапуска\n/sandbox_cancel — отменить"
    else:
        msg += "/sandbox_cancel — отменить (исправь ошибки и попробуй снова)"

    if bot and user_id:
        try:
            await bot.send_message(chat_id=user_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Sandbox notify error: {e}")

    return f"Предложение '{module_name}' ({action}) отправлено пользователю. Проверки: {'OK' if check_ok else 'FAIL — ' + '; '.join(errors)}."
