import json
import os
import uuid
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

JOBS_FILE = "/obsidian/Проекты/claude_bot/расписание.json"

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Сервер живёт в своём часовом поясе (сейчас UTC контейнера, физически — Берлин),
# а пользователь — в Europe/Moscow (без перехода на летнее/зимнее время). Разница
# с Берлином плавает 1-2ч из-за CET/CEST, поэтому расписание всегда считается
# в московском времени, а не в системном.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def load_jobs() -> list:
    if not os.path.exists(JOBS_FILE):
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_jobs(jobs: list) -> None:
    os.makedirs(os.path.dirname(JOBS_FILE), exist_ok=True)
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def compute_next_run(schedule_type: str, schedule_value: str, from_dt: datetime = None) -> datetime | None:
    now = from_dt or moscow_now()

    if schedule_type == "interval":
        v = schedule_value.strip()
        if v.endswith("m"):
            return now + timedelta(minutes=int(v[:-1]))
        elif v.endswith("h"):
            return now + timedelta(hours=int(v[:-1]))
        elif v.endswith("d"):
            return now + timedelta(days=int(v[:-1]))
        return None

    elif schedule_type == "daily":
        hh, mm = map(int, schedule_value.split(":"))
        next_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if next_dt <= now:
            next_dt += timedelta(days=1)
        return next_dt

    elif schedule_type == "weekly":
        parts = schedule_value.split(":")
        target_wd = int(parts[0])
        hh, mm = int(parts[1]), int(parts[2])
        days_ahead = (target_wd - now.weekday()) % 7
        next_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if days_ahead == 0 and next_dt <= now:
            days_ahead = 7
        return next_dt + timedelta(days=days_ahead)

    elif schedule_type == "once":
        try:
            return datetime.fromisoformat(schedule_value)
        except Exception:
            return None

    return None


def parse_schedule(schedule_str: str) -> tuple[str, str] | None:
    s = schedule_str.strip()
    for prefix in ("interval:", "daily:", "weekly:", "once:"):
        if s.startswith(prefix):
            return (prefix.rstrip(":"), s[len(prefix):])
    return None


def format_schedule(schedule_type: str, schedule_value: str) -> str:
    if schedule_type == "interval":
        v = schedule_value
        if v.endswith("m"):
            return f"каждые {v[:-1]} мин"
        elif v.endswith("h"):
            return f"каждые {v[:-1]} ч"
        elif v.endswith("d"):
            return f"каждые {v[:-1]} дн"
    elif schedule_type == "daily":
        return f"ежедневно в {schedule_value}"
    elif schedule_type == "weekly":
        parts = schedule_value.split(":")
        wd = WEEKDAY_NAMES[int(parts[0])]
        return f"каждый {wd} в {parts[1]}:{parts[2]}"
    elif schedule_type == "once":
        return f"однократно в {schedule_value}"
    return f"{schedule_type}:{schedule_value}"


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:
    if name == "schedule_add":
        label = inputs.get("label", "").strip()
        task = inputs.get("task", "").strip()
        schedule_str = inputs.get("schedule", "").strip()
        user_id = kwargs.get("user_id")

        if not label or not task or not schedule_str:
            return "Ошибка: нужны label, task, schedule."

        parsed = parse_schedule(schedule_str)
        if not parsed:
            return (
                "Неверный формат расписания. Допустимые форматы:\n"
                "• interval:30m / interval:2h / interval:1d\n"
                "• daily:09:00\n"
                "• weekly:0:09:00 (0=Пн, 6=Вс)\n"
                "• once:2026-07-01T09:00"
            )

        schedule_type, schedule_value = parsed
        next_run = compute_next_run(schedule_type, schedule_value)
        if not next_run:
            return "Не удалось вычислить время следующего запуска — проверь формат."

        if schedule_type == "once" and next_run <= moscow_now():
            return "Ошибка: время для once-задачи уже в прошлом."

        job = {
            "id": str(uuid.uuid4()),
            "label": label,
            "task": task,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "next_run": next_run.isoformat(timespec="minutes"),
            "created": moscow_now().isoformat(timespec="minutes"),
            "user_id": user_id,
        }

        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)

        return (
            f"✅ Задача добавлена: {label}\n"
            f"Расписание: {format_schedule(schedule_type, schedule_value)}\n"
            f"Первый запуск: {next_run.strftime('%Y-%m-%d %H:%M')}"
        )

    elif name == "schedule_list":
        jobs = load_jobs()
        if not jobs:
            return "Нет активных задач по расписанию."
        lines = []
        for j in jobs:
            sched = format_schedule(j["schedule_type"], j["schedule_value"])
            next_run = j.get("next_run", "?")
            task_preview = j["task"][:80] + ("…" if len(j["task"]) > 80 else "")
            lines.append(
                f"[{j['id'][:8]}] {j['label']}\n"
                f"  📅 {sched} → следующий: {next_run}\n"
                f"  💬 {task_preview}"
            )
        return f"Задачи по расписанию ({len(jobs)}):\n\n" + "\n\n".join(lines)

    elif name == "schedule_remove":
        job_id = inputs.get("job_id", "").strip()
        jobs = load_jobs()
        found = [j for j in jobs if j["id"] == job_id or j["id"].startswith(job_id)]
        if not found:
            return f"Задача с ID '{job_id}' не найдена."
        labels = ", ".join(f["label"] for f in found)
        found_ids = {f["id"] for f in found}
        save_jobs([j for j in jobs if j["id"] not in found_ids])
        return f"✅ Удалено: {labels}"

    return f"Неизвестный инструмент: {name}"
