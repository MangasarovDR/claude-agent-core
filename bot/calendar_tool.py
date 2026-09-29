from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import json

CREDENTIALS_FILE = "/app/google_credentials.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)

def list_events(days_ahead: int = 7, calendar_id: str = "mangasarovdr@gmail.com") -> str:
    try:
        service = get_service()
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=now,
            timeMax=end,
            maxResults=20,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"Нет событий на ближайшие {days_ahead} дней"
        lines = [f"📅 События на {days_ahead} дней:"]
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            end = e["end"].get("dateTime", e["end"].get("date", ""))
            if "T" in start:
                dt_start = datetime.fromisoformat(start.replace("Z",""))
                dt_end = datetime.fromisoformat(end.replace("Z",""))
                time_str = f"{dt_start.strftime('%d.%m %H:%M')}–{dt_end.strftime('%H:%M')}"
            else:
                time_str = start
            lines.append(f"• {time_str} [{e['id'][:8]}] — {e.get('summary', 'Без названия')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"

def create_event(title: str, start: str, end: str = None, description: str = "", calendar_id: str = "mangasarovdr@gmail.com") -> str:
    try:
        service = get_service()
        if not end:
            start_dt = datetime.fromisoformat(start)
            end_dt = start_dt + timedelta(hours=1)
            end = end_dt.isoformat()
        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start, "timeZone": "Europe/Moscow"},
            "end": {"dateTime": end, "timeZone": "Europe/Moscow"},
        }
        result = service.events().insert(calendarId=calendar_id, body=event).execute()
        return f"✅ Создано: {title}\n🔗 {result.get('htmlLink')}"
    except Exception as e:
        return f"Ошибка: {e}"

def delete_event(event_id: str, calendar_id: str = "mangasarovdr@gmail.com") -> str:
    try:
        service = get_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return "✅ Событие удалено"
    except Exception as e:
        return f"Ошибка: {e}"

def find_event(query: str, calendar_id: str = "mangasarovdr@gmail.com") -> str:
    try:
        service = get_service()
        now = datetime.utcnow().isoformat() + "Z"
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=now,
            q=query,
            maxResults=5,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"Не найдено событий по запросу: {query}"
        lines = [f"🔍 Найдено по '{query}':"]
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z",""))
                start_str = dt.strftime("%d.%m %H:%M")
            else:
                start_str = start
            lines.append(f"• [{e['id']}] {start_str} — {e.get('summary', 'Без названия')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"

def update_event(query: str, new_title: str = None, new_start: str = None, 
                 new_end: str = None, new_description: str = None,
                 calendar_id: str = "mangasarovdr@gmail.com") -> str:
    try:
        service = get_service()
        # Ищем событие по названию
        now = (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z"
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=now,
            q=query,
            maxResults=5,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"Не найдено событий по запросу: {query}"
        
        event = events[0]
        if new_title:
            event["summary"] = new_title
        if new_start:
            event["start"] = {"dateTime": new_start, "timeZone": "Europe/Moscow"}
        if new_end:
            event["end"] = {"dateTime": new_end, "timeZone": "Europe/Moscow"}
        if new_description:
            event["description"] = new_description
            
        updated = service.events().update(
            calendarId=calendar_id,
            eventId=event["id"],
            body=event
        ).execute()
        return f"✅ Обновлено: {updated.get('summary')}\n🔗 {updated.get('htmlLink')}"
    except Exception as e:
        return f"Ошибка: {e}"
