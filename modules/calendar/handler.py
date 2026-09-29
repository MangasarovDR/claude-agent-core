import sys
sys.path.insert(0, "/app")
import calendar_tool as cal


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:
    if name == "get_calendar_events":
        return cal.list_events(days_ahead=inputs.get("days", 7))
    elif name == "create_calendar_event":
        return cal.create_event(
            title=inputs["title"],
            start=inputs["start"],
            end=inputs.get("end"),
            description=inputs.get("description", ""),
        )
    elif name == "update_calendar_event":
        return cal.update_event(
            query=inputs["query"],
            new_title=inputs.get("new_title"),
            new_start=inputs.get("new_start"),
            new_end=inputs.get("new_end"),
        )
    elif name == "delete_calendar_event":
        found = cal.find_event(inputs["query"])
        if "Не найдено" in found:
            return found
        for line in found.split("\n"):
            if "[" in line and "]" in line:
                event_id = line.split("[")[1].split("]")[0]
                return cal.delete_event(event_id)
        return "Не удалось найти ID события"
    return f"Неизвестный инструмент: {name}"
