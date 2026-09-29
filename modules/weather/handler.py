import os
import logging
import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.weather.yandex.ru/v2/forecast"

# Координаты по умолчанию (Москва), если lat/lon не переданы и не заданы в .env
DEFAULT_LAT = float(os.getenv("WEATHER_DEFAULT_LAT", "55.75"))
DEFAULT_LON = float(os.getenv("WEATHER_DEFAULT_LON", "37.62"))

CONDITIONS = {
    "clear": "ясно",
    "partly-cloudy": "малооблачно",
    "cloudy": "облачно с прояснениями",
    "overcast": "пасмурно",
    "drizzle": "морось",
    "light-rain": "небольшой дождь",
    "rain": "дождь",
    "moderate-rain": "умеренный дождь",
    "heavy-rain": "сильный дождь",
    "continuous-heavy-rain": "длительный сильный дождь",
    "showers": "ливень",
    "wet-snow": "дождь со снегом",
    "light-snow": "небольшой снег",
    "snow": "снег",
    "snow-showers": "снегопад",
    "hail": "град",
    "thunderstorm": "гроза",
    "thunderstorm-with-rain": "дождь с грозой",
    "thunderstorm-with-hail": "гроза с градом",
}

WIND_DIRS = {
    "nw": "северо-западный", "n": "северный", "ne": "северо-восточный",
    "e": "восточный", "se": "юго-восточный", "s": "южный",
    "sw": "юго-западный", "w": "западный", "c": "штиль",
}

CONDITION_EMOJI = {
    "clear": "☀️", "partly-cloudy": "⛅", "cloudy": "🌥", "overcast": "☁️",
    "drizzle": "🌦", "light-rain": "🌧", "rain": "🌧", "moderate-rain": "🌧",
    "heavy-rain": "🌧", "continuous-heavy-rain": "🌧", "showers": "🌧",
    "wet-snow": "🌨", "light-snow": "🌨", "snow": "❄️", "snow-showers": "❄️",
    "hail": "🌨", "thunderstorm": "⛈", "thunderstorm-with-rain": "⛈",
    "thunderstorm-with-hail": "⛈",
}

PART_NAMES = {"morning": "Утро", "day": "День", "evening": "Вечер", "night": "Ночь"}

# Порядок отображения частей суток
_PART_ORDER = {"morning": 0, "day": 1, "evening": 2, "night": 3}


def _normalize_parts(parts) -> list:
    """v2 API возвращает parts как dict; приводим к списку с полем part_name."""
    if isinstance(parts, dict):
        result = []
        for key, val in parts.items():
            if key.endswith("_short") or not isinstance(val, dict):
                continue
            item = dict(val)
            item.setdefault("part_name", key)
            result.append(item)
        result.sort(key=lambda p: _PART_ORDER.get(p.get("part_name", ""), 99))
        return result
    return list(parts) if parts else []


def _condition_ru(code: str) -> str:
    return CONDITIONS.get(code, code)


def _droplets(prec_prob: int) -> str:
    if prec_prob >= 75:
        filled = 3
    elif prec_prob >= 50:
        filled = 2
    elif prec_prob >= 25:
        filled = 1
    else:
        filled = 0
    return "💧" * filled + "◦" * (3 - filled)


def _build_recommendations(parts: list, fact: dict | None = None) -> list:
    tips = []
    feels_likes = []
    conditions = []
    max_prec = 0
    max_wind = 0.0

    if fact:
        if fact.get("feels_like") is not None:
            feels_likes.append(fact["feels_like"])
        conditions.append(fact.get("condition", ""))
        max_wind = max(max_wind, fact.get("wind_speed") or 0)

    for part in parts or []:
        if part.get("feels_like") is not None:
            feels_likes.append(part["feels_like"])
        conditions.append(part.get("condition", ""))
        max_prec = max(max_prec, part.get("prec_prob") or 0)
        max_wind = max(max_wind, part.get("wind_speed") or 0)

    if feels_likes:
        lo, hi = min(feels_likes), max(feels_likes)
        if lo <= -10:
            tips.append("сильный мороз — тёплая куртка и шапка обязательны")
        elif lo <= 5:
            tips.append("прохладно — одевайся теплее")
        if hi >= 28:
            tips.append("жарко — лёгкая одежда, не забудь воду")

    cond_str = " ".join(conditions)
    if any(x in cond_str for x in ("rain", "showers", "drizzle", "thunderstorm")):
        tips.append("ожидаются осадки — возьми зонт")
    elif max_prec >= 50:
        tips.append(f"вероятность осадков до {max_prec}% — на всякий случай возьми зонт")
    if any(x in cond_str for x in ("snow", "hail")):
        tips.append("снег/град — учти скользкую дорогу")
    if max_wind >= 10:
        tips.append("сильный ветер — учти при выборе одежды")
    if not tips:
        tips.append("ничего особенного, обычная погода")
    return tips


def _t(value) -> str:
    """Безопасное форматирование температуры — плейсхолдер вместо 'None°C', если API не вернул поле."""
    return str(value) if value is not None else "?"


def _format_weather(data: dict, city: str | None, day_offset: int = 0) -> str:
    label = city or "текущая точка"
    forecasts = data.get("forecasts", [])

    if day_offset == 0:
        fact = data.get("fact", {})
        today = forecasts[0] if forecasts else {}
        parts = _normalize_parts(today.get("parts", []))
        date_str = today.get("date", "")

        lines = [f"🌦 Погода: {label}" + (f" — сегодня, {date_str}" if date_str else "") + "\n"]
        emoji = CONDITION_EMOJI.get(fact.get("condition", ""), "")
        temp = fact.get("temp")
        feels_like = fact.get("feels_like")
        condition = _condition_ru(fact.get("condition", ""))
        wind_speed = fact.get("wind_speed")
        wind_dir = WIND_DIRS.get(fact.get("wind_dir", ""), fact.get("wind_dir", ""))
        pressure = fact.get("pressure_mm")
        humidity = fact.get("humidity")

        lines.append(f"{emoji} Сейчас: {_t(temp)}°C, ощущается как {_t(feels_like)}°C, {condition}")
        if wind_speed is not None:
            lines.append(f"Ветер: {wind_speed} м/с, {wind_dir}")
        if pressure is not None:
            lines.append(f"Давление: {pressure} мм рт.ст., влажность: {humidity}%")
        fact_for_tips = fact
        section_title = "Сегодня по времени суток:"
    else:
        if len(forecasts) <= day_offset:
            return (
                f"Прогноз для {label} на {day_offset} дн. вперёд недоступен — "
                "Яндекс.Погода не отдаёт данные на такой срок."
            )
        day = forecasts[day_offset]
        parts = _normalize_parts(day.get("parts", []))
        date_str = day.get("date", "")
        lines = [f"🌦 Погода: {label} — {date_str}\n"]
        fact_for_tips = None
        section_title = "По времени суток:"

    if parts:
        lines.append(section_title)
        for part in parts:
            name = PART_NAMES.get(part.get("part_name"), part.get("part_name"))
            emoji = CONDITION_EMOJI.get(part.get("condition", ""), "")
            t_min, t_max = part.get("temp_min"), part.get("temp_max")
            cond = _condition_ru(part.get("condition", ""))
            droplets = _droplets(part.get("prec_prob") or 0)
            lines.append(f"  {emoji} {name}: {_t(t_min)}…{_t(t_max)}°C, {cond}  {droplets}")

    tips = _build_recommendations(parts, fact_for_tips)
    lines.append("\n💡 " + "; ".join(tips))

    return "\n".join(lines)


async def handle_tool(name: str, inputs: dict, **kwargs) -> str:
    if name != "get_weather":
        return f"Неизвестный инструмент: {name}"

    get_active_key = kwargs.get("get_active_key")
    key = get_active_key("yandex_weather") if get_active_key else None
    if not key:
        return "Нет ключа Яндекс.Погоды. Добавь: /addkey yandex_weather KEY"

    lat = inputs.get("lat", DEFAULT_LAT)
    lon = inputs.get("lon", DEFAULT_LON)
    city = inputs.get("city")
    day_offset = max(0, min(int(inputs.get("day_offset", 0)), 6))
    limit = max(day_offset + 1, 2)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                API_URL,
                headers={"X-Yandex-Weather-Key": key},
                params={"lat": lat, "lon": lon, "lang": "ru_RU", "limit": limit},
            )
            if resp.status_code in (401, 403):
                return "Ключ Яндекс.Погоды недействителен или исчерпан. Проверь /addkey yandex_weather KEY"
            if resp.status_code == 429:
                return "Лимит запросов к Яндекс.Погоде исчерпан, попробуй позже"
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning(f"get_weather: ошибка запроса ({e})")
        return f"Не удалось получить погоду: {e}"

    return _format_weather(data, city, day_offset)
