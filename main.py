"""Telegram bot with a 48-hour chat timer."""

from __future__ import annotations

import logging
import json
import os
import difflib
import re
from html import escape
from pathlib import Path
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import telebot
from telebot import types

from energy_catalog import CatalogUnavailable, EnergyDrink, search_energy_drink
from ai_search import ai_search_energy


TIMER_SECONDS = 48 * 60 * 60
COUNTDOWN_UPDATE_SECONDS = 60
START_BUTTON = "Запустить 48 часов"
STATUS_BUTTON = "Сколько осталось"
RESTART_BUTTON = "Запустить таймер заново"
STATS_BUTTON = "Статистика"
REMOVE_ENERGY_BUTTON = "Удалить энергетик"
TIMERS_FILE = Path(__file__).with_name("timers.json")
COLLECTION_FILE = Path(__file__).with_name("energy_collection.json")
CAFFEINE_DAILY_REFERENCE_MG = 400.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не задан секрет TELEGRAM_BOT_TOKEN. "
            "Добавьте токен в Secrets и перезапустите бота."
        )
    return token


bot = telebot.TeleBot(get_token())


@dataclass(frozen=True)
class TimerState:
    message_id: int
    end_at: float
    cancel_event: threading.Event


active_timers: dict[int, TimerState] = {}
timers_lock = threading.Lock()
energy_collection: dict[int, list[dict[str, Any]]] = {}
collection_lock = threading.Lock()


def save_timers_locked() -> None:
    data = {
        str(chat_id): {
            "message_id": state.message_id,
            "end_at": state.end_at,
        }
        for chat_id, state in active_timers.items()
        if not state.cancel_event.is_set()
    }
    temporary_file = TIMERS_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(TIMERS_FILE)


def restore_timers() -> None:
    if not TIMERS_FILE.exists():
        return

    try:
        data = json.loads(TIMERS_FILE.read_text(encoding="utf-8"))
        for chat_id, timer_data in data.items():
            state = TimerState(
                message_id=int(timer_data["message_id"]),
                end_at=float(timer_data["end_at"]),
                cancel_event=threading.Event(),
            )
            active_timers[int(chat_id)] = state
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Не удалось восстановить сохранённые таймеры")


def save_collection_locked() -> None:
    data = {str(chat_id): drinks for chat_id, drinks in energy_collection.items()}
    temporary_file = COLLECTION_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(COLLECTION_FILE)


def restore_collection() -> None:
    if not COLLECTION_FILE.exists():
        return

    try:
        data = json.loads(COLLECTION_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for chat_id, drinks in data.items():
                if isinstance(drinks, list):
                    energy_collection[int(chat_id)] = merge_collection_items(drinks)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Не удалось восстановить коллекцию энергетиков")


def main_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие",
    )
    markup.row(
        types.KeyboardButton(START_BUTTON),
        types.KeyboardButton(STATUS_BUTTON),
    )
    markup.row(types.KeyboardButton(RESTART_BUTTON))
    markup.row(types.KeyboardButton(STATS_BUTTON))
    markup.row(types.KeyboardButton(REMOVE_ENERGY_BUTTON))
    return markup


@bot.message_handler(commands=["start"])
def start(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "Нажми кнопку ниже:",
        reply_markup=main_keyboard(),
    )


def format_remaining(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d} ч. {minutes:02d} мин. {seconds:02d} сек."


def countdown_text(remaining: float) -> str:
    return (
        "⏱ <b>Таймер на 48 часов</b>\n"
        f"До окончания: <b>{format_remaining(remaining)}</b>"
    )


def format_amount(value: float | None) -> str:
    if value is None:
        return "нет данных в базе"
    if value.is_integer():
        return f"{int(value)} мг"
    return f"{value:.1f} мг"


def drink_details(drink: EnergyDrink) -> str:
    caffeine = format_amount(drink.caffeine_mg)
    if drink.caffeine_mg is None:
        caffeine_line = f"☕ Кофеин: <b>{caffeine}</b>"
    else:
        caffeine_percent = drink.caffeine_mg / CAFFEINE_DAILY_REFERENCE_MG * 100
        caffeine_line = (
            f"☕ Кофеин: <b>{caffeine}</b> "
            f"({caffeine_percent:.1f}% от ориентира {CAFFEINE_DAILY_REFERENCE_MG:.0f} мг/день)"
        )

    taurine = format_amount(drink.taurine_mg)
    taurine_line = f"Таурин: <b>{taurine}</b>"
    if drink.taurine_mg is not None:
        taurine_line += " (официальная суточная норма не установлена)"

    return (
        f"✅ <b>{escape(drink.name)}</b>\n"
        f"Бренд: {escape(drink.brand)}\n"
        f"Объём: {escape(drink.quantity)}\n\n"
        f"{caffeine_line}\n"
        f"{taurine_line}\n\n"
        f'<a href="{escape(drink.source_url, quote=True)}">Источник: Open Food Facts</a>'
    )


def add_to_collection(chat_id: int, drink: EnergyDrink) -> int:
    with collection_lock:
        drinks = energy_collection.setdefault(chat_id, [])
        for item in drinks:
            if str(item.get("code")) == drink.code:
                item["count"] = record_count(item) + 1
                save_collection_locked()
                return item["count"]

        record = drink.to_dict()
        record["count"] = 1
        drinks.append(record)
        save_collection_locked()
    return 1


def record_amount(record: dict[str, Any], key: str) -> float | None:
    value = record.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_count(record: dict[str, Any]) -> int:
    try:
        return max(1, int(record.get("count", 1)))
    except (TypeError, ValueError):
        return 1


def collection_item_key(record: dict[str, Any]) -> str:
    code = str(record.get("code", "")).strip()
    if code and code.lower() != "none":
        return f"code:{code}"
    return f"name:{str(record.get('name', 'Без названия')).strip().casefold()}"


def merge_collection_items(drinks: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, dict[str, Any]] = {}
    for drink in drinks:
        if not isinstance(drink, dict):
            continue
        item = dict(drink)
        item["count"] = record_count(item)
        key = collection_item_key(item)
        existing = positions.get(key)
        if existing is None:
            positions[key] = item
            merged.append(item)
        else:
            existing["count"] = record_count(existing) + item["count"]
    return merged


def collection_statistics(chat_id: int) -> str:
    with collection_lock:
        drinks = list(energy_collection.get(chat_id, []))

    if not drinks:
        return (
            "📊 <b>Статистика</b>\n\n"
            "Коллекция пока пуста. Просто напиши название энергетика в чат."
        )

    caffeine_values = [
        (value, record_count(drink))
        for drink in drinks
        if (value := record_amount(drink, "caffeine_mg")) is not None
    ]
    taurine_values = [
        (value, record_count(drink))
        for drink in drinks
        if (value := record_amount(drink, "taurine_mg")) is not None
    ]
    total_units = sum(record_count(drink) for drink in drinks)
    lines = [
        "📊 <b>Статистика коллекции</b>",
        f"Сохранено банок: <b>{total_units}</b>",
        f"Видов напитков: <b>{len(drinks)}</b>",
    ]

    if caffeine_values:
        caffeine_total = sum(value * count for value, count in caffeine_values)
        caffeine_percent = caffeine_total / CAFFEINE_DAILY_REFERENCE_MG * 100
        lines.append(
            f"Кофеин во всей коллекции: <b>{format_amount(caffeine_total)}</b>"
        )
        lines.append(
            f"Если выпить всё за день: <b>{caffeine_percent:.1f}%</b> "
            f"от ориентира {CAFFEINE_DAILY_REFERENCE_MG:.0f} мг "
            "для здорового взрослого."
        )
    else:
        lines.append("Кофеин: нет данных по сохранённым напиткам.")

    if taurine_values:
        taurine_total = sum(value * count for value, count in taurine_values)
        lines.append(f"Таурин во всей коллекции: <b>{format_amount(taurine_total)}</b>")
        lines.append("Для таурина официальная единая суточная норма не установлена.")
    else:
        lines.append("Таурин: нет данных по сохранённым напиткам.")

    names = [
        (
            f"• {escape(str(drink.get('name', 'Без названия')))}"
            f" × {record_count(drink)}"
            if record_count(drink) > 1
            else f"• {escape(str(drink.get('name', 'Без названия')))}"
        )
        for drink in drinks[-10:]
    ]
    lines.append("\n<b>Последние напитки:</b>\n" + "\n".join(names))
    return "\n".join(lines)


LOCAL_NAME_ALIASES = {
    "redbull": "red bull",
    "red bul": "red bull",
    "редбул": "red bull",
    "ред бул": "red bull",
    "monser": "monster",
    "monsster": "monster",
    "монстер": "monster",
    "монстр": "monster",
    "adrenalin": "adrenaline",
    "адреналин": "адреналин",
    "burnn": "burn",
    "берн": "burn",
    "rock star": "rockstar",
    "рокстар": "rockstar",
}
LOCAL_BRAND_WORDS = {
    "red",
    "bull",
    "monster",
    "energy",
    "burn",
    "adrenaline",
    "адреналин",
    "rockstar",
    "flash",
    "drive",
    "tornado",
    "hell",
    "gorilla",
    "nos",
    "vampire",
    "тигр",
}
GEMINI_MODEL_NAMES = (
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
)


def normalize_with_free_ai(query: str) -> tuple[str, str, bool]:
    """Normalize common typos locally, without a paid AI API or API key."""
    cleaned = re.sub(r"\s+", " ", query.replace("ё", "е").strip())
    alias_key = cleaned.casefold()
    if alias_key in LOCAL_NAME_ALIASES:
        normalized = LOCAL_NAME_ALIASES[alias_key]
    else:
        words = cleaned.split(" ")
        normalized_words: list[str] = []
        for word in words:
            folded = word.casefold()
            if folded in LOCAL_BRAND_WORDS or len(folded) < 4:
                normalized_words.append(word)
                continue
            match = difflib.get_close_matches(
                folded,
                LOCAL_BRAND_WORDS,
                n=1,
                cutoff=0.78,
            )
            normalized_words.append(match[0] if match else word)
        normalized = " ".join(normalized_words)

    changed = normalized.casefold() != cleaned.casefold()
    return normalized, normalized if changed else "", changed


def normalize_with_gemini(query: str) -> tuple[str, str, str]:
    """Use Gemini for name cleanup, with a no-cost local fallback."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        normalized, hint, changed = normalize_with_free_ai(query)
        return normalized, hint, "локальный помощник" if changed else "без изменений"

    prompt = (
        "Ты исправляешь только название энергетического напитка для поиска в "
        "Open Food Facts. Исправь опечатки и очевидную транслитерацию, но не "
        "выдумывай бренд, вкус, объём, кофеин или таурин. Верни только JSON "
        'в формате {"search_query":"...", "canonical_hint":"..."}. '
        "search_query должен быть коротким запросом, а canonical_hint — "
        "наиболее вероятным правильным названием или пустой строкой.\n\n"
        f"Название пользователя: {query}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    try:
        last_error: Exception | None = None
        for model_name in GEMINI_MODEL_NAMES:
            for attempt in range(2):
                try:
                    request = Request(
                        (
                            "https://generativelanguage.googleapis.com/v1beta/models/"
                            f"{model_name}:generateContent"
                        ),
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "x-goog-api-key": api_key,
                        },
                        method="POST",
                    )
                    with urlopen(request, timeout=20) as response:
                        result = json.load(response)
                    text = result["candidates"][0]["content"]["parts"][0]["text"]
                    normalized = json.loads(text)
                    search_query = normalized.get("search_query", query)
                    canonical_hint = normalized.get("canonical_hint", "")
                    if not isinstance(search_query, str) or not search_query.strip():
                        raise ValueError("Gemini returned an empty search query")
                    if not isinstance(canonical_hint, str):
                        canonical_hint = ""
                    return search_query.strip(), canonical_hint.strip(), "Gemini"
                except HTTPError as error:
                    last_error = error
                    if error.code in {429, 500, 503, 504} and attempt == 0:
                        time.sleep(0.8)
                        continue
                    break
                except (OSError, TimeoutError) as error:
                    last_error = error
                    if attempt == 0:
                        time.sleep(0.8)
                        continue
                    break
        raise RuntimeError("No Gemini model responded") from last_error
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ):
        logger.warning("Gemini недоступен, используется локальная коррекция")
        normalized, hint, changed = normalize_with_free_ai(query)
        return normalized, hint, "локальный помощник" if changed else "без изменений"


def remove_keyboard(chat_id: int) -> types.InlineKeyboardMarkup | None:
    with collection_lock:
        drinks = list(energy_collection.get(chat_id, []))

    if not drinks:
        return None

    markup = types.InlineKeyboardMarkup()
    for index, drink in enumerate(drinks):
        name = str(drink.get("name", "Без названия"))
        count = record_count(drink)
        label = f"Удалить: {name[:30]}"
        if count > 1:
            label += f" × {count}"
        markup.add(
            types.InlineKeyboardButton(
                label,
                callback_data=f"energy_select:{index}",
            )
        )
    return markup


def quantity_keyboard(index: int, count: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    for amount in range(1, min(count, 9) + 1):
        markup.add(
            types.InlineKeyboardButton(
                f"Убрать {amount}",
                callback_data=f"energy_remove:{index}:{amount}",
            )
        )
    if count > 9:
        markup.add(
            types.InlineKeyboardButton(
                f"Убрать всё ({count})",
                callback_data=f"energy_remove:{index}:{count}",
            )
        )
    return markup


def remove_collection_amount(
    chat_id: int,
    index: int,
    amount: int,
) -> tuple[str, int, int] | None:
    with collection_lock:
        drinks = energy_collection.get(chat_id, [])
        if index < 0 or index >= len(drinks):
            return None

        drink = drinks[index]
        count = record_count(drink)
        removed_amount = min(max(1, amount), count)
        name = str(drink.get("name", "Без названия"))
        remaining = count - removed_amount
        if remaining:
            drink["count"] = remaining
        else:
            drinks.pop(index)
        if not drinks:
            energy_collection.pop(chat_id, None)
        save_collection_locked()
        return name, removed_amount, remaining


def timer_worker(chat_id: int, state: TimerState) -> None:
    while True:
        remaining = state.end_at - time.time()
        if remaining <= 0 or state.cancel_event.wait(
            min(COUNTDOWN_UPDATE_SECONDS, remaining)
        ):
            break

        try:
            bot.edit_message_text(
                countdown_text(state.end_at - time.time()),
                chat_id=chat_id,
                message_id=state.message_id,
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Не удалось обновить таймер для чата %s", chat_id)

    with timers_lock:
        if active_timers.get(chat_id) == state:
            active_timers.pop(chat_id, None)
            save_timers_locked()

    if not state.cancel_event.is_set():
        try:
            bot.edit_message_text(
                "✅ <b>48 часов прошли.</b> Таймер завершён.",
                chat_id=chat_id,
                message_id=state.message_id,
                parse_mode="HTML",
            )
            bot.send_message(chat_id, "🔔 Время вышло!")
        except Exception:
            logger.warning("Не удалось отправить завершение для чата %s", chat_id)


def send_status(chat_id: int) -> None:
    with timers_lock:
        state = active_timers.get(chat_id)

    if state is None or state.cancel_event.is_set():
        bot.send_message(
            chat_id,
            "Активного таймера нет.",
            reply_markup=main_keyboard(),
        )
        return

    bot.send_message(
        chat_id,
        countdown_text(state.end_at - time.time()),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["status"])
def status(message: types.Message) -> None:
    send_status(message.chat.id)


@bot.message_handler(func=lambda message: message.text == STATUS_BUTTON)
def status_button(message: types.Message) -> None:
    send_status(message.chat.id)


@bot.message_handler(commands=["stats"])
def stats_command(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        collection_statistics(message.chat.id),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == STATS_BUTTON)
def stats_button(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        collection_statistics(message.chat.id),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == REMOVE_ENERGY_BUTTON)
def remove_energy_button(message: types.Message) -> None:
    markup = remove_keyboard(message.chat.id)
    if markup is None:
        bot.send_message(
            message.chat.id,
            "Коллекция пока пуста.",
            reply_markup=main_keyboard(),
        )
        return

    bot.send_message(
        message.chat.id,
        "Выбери напиток, который нужно убрать:",
        reply_markup=markup,
    )


@bot.callback_query_handler(
    func=lambda call: bool(
        call.data
        and (
            call.data.startswith("energy_select:")
            or call.data.startswith("energy_delete:")
            or call.data.startswith("energy_remove:")
        )
    )
)
def delete_energy_callback(call: types.CallbackQuery) -> None:
    if call.data.startswith("energy_remove:"):
        parts = call.data.split(":")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Не удалось определить количество.")
            return
        try:
            index = int(parts[1])
            amount = int(parts[2])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить количество.")
            return
        _complete_collection_removal(call, index, amount)
        return

    try:
        index = int(call.data.split(":", 1)[1])
    except (AttributeError, ValueError):
        bot.answer_callback_query(call.id, "Не удалось определить напиток.")
        return

    chat_id = call.message.chat.id if call.message else None
    if chat_id is None:
        bot.answer_callback_query(call.id, "Чат не найден.")
        return

    with collection_lock:
        drinks = energy_collection.get(chat_id, [])
        if index < 0 or index >= len(drinks):
            bot.answer_callback_query(call.id, "Этот пункт уже удалён.")
            return
        drink = drinks[index]
        name = str(drink.get("name", "Без названия"))
        count = record_count(drink)

    if call.data.startswith("energy_delete:") or count == 1:
        _complete_collection_removal(call, index, 1)
        return

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"<b>{escape(name)}</b>\nВ коллекции: <b>{count}</b> шт.\n\nСколько убрать?",
        chat_id=chat_id,
        message_id=call.message.message_id,
        parse_mode="HTML",
        reply_markup=quantity_keyboard(index, count),
    )


def _complete_collection_removal(
    call: types.CallbackQuery,
    index: int,
    amount: int,
) -> None:
    chat_id = call.message.chat.id if call.message else None
    if chat_id is None:
        bot.answer_callback_query(call.id, "Чат не найден.")
        return

    result = remove_collection_amount(chat_id, index, amount)
    if result is None:
        bot.answer_callback_query(call.id, "Этот пункт уже удалён.")
        return

    name, removed_amount, remaining = result
    bot.answer_callback_query(call.id, "Удалено из коллекции.")
    remaining_text = f"\nОсталось: <b>{remaining}</b> шт." if remaining else ""
    bot.edit_message_text(
        f"🗑 Удалено: <b>{escape(name)}</b> × {removed_amount}{remaining_text}",
        chat_id=chat_id,
        message_id=call.message.message_id,
        parse_mode="HTML",
    )
    bot.send_message(
        chat_id,
        "Коллекция обновлена.",
        reply_markup=main_keyboard(),
    )


def is_energy_query(message: types.Message) -> bool:
    text = (message.text or "").strip()
    buttons = {
        START_BUTTON,
        STATUS_BUTTON,
        RESTART_BUTTON,
        STATS_BUTTON,
        REMOVE_ENERGY_BUTTON,
    }
    return bool(text) and not text.startswith("/") and text not in buttons


def process_energy_query(chat_id: int, query: str) -> None:
    search_query, _, _ = normalize_with_gemini(query)

    try:
        drink = search_energy_drink(search_query)
        if drink is None and search_query != query:
            drink = ai_search_energy(query) or search_energy_drink(query)
    except CatalogUnavailable:
        bot.send_message(
            chat_id,
            "Не удалось связаться с базой продуктов. Попробуй ещё раз через минуту.",
            reply_markup=main_keyboard(),
        )
        return

    if drink is None:
        bot.send_message(
            chat_id,
            "Не нашёл подходящий напиток. Попробуй написать название подробнее.",
            reply_markup=main_keyboard(),
        )
        return

    count = add_to_collection(chat_id, drink)
    prefix = (
        f"Теперь в коллекции этого напитка: <b>{count}</b> шт.\n\n"
        if count > 1
        else ""
    )
    bot.send_message(
        chat_id,
        prefix + drink_details(drink),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=is_energy_query)
def energy_query(message: types.Message) -> None:
    query = (message.text or "").strip()
    chat_id = message.chat.id
    bot.send_message(
        chat_id,
        "ИИ в поиске :)",
        reply_markup=main_keyboard(),
    )
    threading.Thread(
        target=process_energy_query,
        args=(chat_id, query),
        name=f"energy-search-{chat_id}",
        daemon=True,
    ).start()


def start_timer(chat_id: int, restart: bool = False) -> None:
    previous_state: TimerState | None = None
    with timers_lock:
        previous_state = active_timers.get(chat_id)
        if previous_state is not None and not restart:
            bot.send_message(
                chat_id,
                "Таймер для этого чата уже запущен.",
                reply_markup=main_keyboard(),
            )
            return

        if previous_state is not None:
            previous_state.cancel_event.set()
            active_timers.pop(chat_id, None)
            save_timers_locked()

    if previous_state is not None:
        try:
            bot.edit_message_text(
                "🔄 <b>Таймер перезапущен.</b>",
                chat_id=chat_id,
                message_id=previous_state.message_id,
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Не удалось обновить старое сообщение для чата %s", chat_id)

    end_at = time.time() + TIMER_SECONDS
    countdown_message = bot.send_message(
        chat_id,
        countdown_text(TIMER_SECONDS),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    state = TimerState(
        message_id=countdown_message.id,
        end_at=end_at,
        cancel_event=threading.Event(),
    )

    with timers_lock:
        active_timers[chat_id] = state
        save_timers_locked()

    threading.Thread(
        target=timer_worker,
        args=(chat_id, state),
        daemon=True,
        name=f"timer-{chat_id}",
    ).start()


@bot.message_handler(func=lambda message: message.text == START_BUTTON)
def handle_click(message: types.Message) -> None:
    start_timer(message.chat.id)


@bot.message_handler(func=lambda message: message.text == RESTART_BUTTON)
def restart_button(message: types.Message) -> None:
    start_timer(message.chat.id, restart=True)


if __name__ == "__main__":
    restore_timers()
    restore_collection()
    for restored_chat_id, restored_state in active_timers.items():
        threading.Thread(
            target=timer_worker,
            args=(restored_chat_id, restored_state),
            daemon=True,
            name=f"timer-{restored_chat_id}",
        ).start()

    logger.info("Бот запускается...")
    bot.infinity_polling(skip_pending=True)