import logging
import os
import re
from datetime import datetime
from pathlib import Path

import telebot
from dotenv import load_dotenv
from telebot import types

load_dotenv(Path(__file__).resolve().parent / ".env")

from countries import resolve_place
from current_api import ExchangeRateError, convert_amount
import storage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot: telebot.TeleBot | None = None

MENU_ITEMS = (
    ("Создать новое путешествие", "menu:newtrip"),
    ("Мои путешествия", "menu:trips"),
    ("Баланс", "menu:balance"),
    ("История расходов", "menu:history"),
    ("Изменить курс", "menu:setrate"),
)

CATEGORIES = (
    ("transport", "Транспорт"),
    ("housing", "Жильё"),
    ("food", "Еда"),
    ("fun", "Развлечения"),
)
CATEGORY_TITLES = dict(CATEGORIES)

YES_WORDS = {"да", "yes", "ок", "ok", "подходит", "ага"}
NO_WORDS = {"нет", "no", "не", "не подходит"}
GENERIC_ERROR = "Что-то пошло не так на моей стороне. Попробуйте ещё раз."


def main_menu() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    for title, callback_data in MENU_ITEMS:
        keyboard.add(types.InlineKeyboardButton(title, callback_data=callback_data))
    return keyboard


def rate_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("✅ Да", callback_data="rate:yes"),
        types.InlineKeyboardButton("❌ Нет", callback_data="rate:no"),
    )
    keyboard.add(types.InlineKeyboardButton("В меню", callback_data="menu:home"))
    return keyboard


def expense_keyboard(token: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("✅ Да", callback_data=f"expense:yes:{token}"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"expense:no:{token}"),
    )
    return keyboard


def category_keyboard(token: str) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    buttons = [
        types.InlineKeyboardButton(title, callback_data=f"expense:cat:{code}:{token}")
        for code, title in CATEGORIES
    ]
    keyboard.row(buttons[0], buttons[1])
    keyboard.row(buttons[2], buttons[3])
    keyboard.add(
        types.InlineKeyboardButton("❌ Не записывать", callback_data=f"expense:no:{token}")
    )
    return keyboard


def category_title(code: str) -> str:
    return CATEGORY_TITLES.get(code, "Без категории")


def send(chat_id: int, text: str, markup=None) -> None:
    bot.send_message(chat_id, text, reply_markup=markup)


def format_amount(value: float) -> str:
    rounded = round(float(value), 2)
    sign = "-" if rounded < 0 else ""
    rounded = abs(rounded)
    if abs(rounded - round(rounded)) < 0.001:
        body = f"{int(round(rounded)):,}"
    else:
        body = f"{rounded:,.2f}"
    return sign + body.replace(",", " ")


def format_rate(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def format_when(value: str) -> str:
    moment = datetime.fromisoformat(value)
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def balance_line(trip: dict) -> str:
    return (
        f"Остаток: {format_amount(trip['dest_balance'])} {trip['to_currency']} = "
        f"{format_amount(trip['home_balance'])} {trip['from_currency']}"
    )


def rate_line(trip: dict) -> str:
    manual = " (ваш курс)" if trip["rate_is_manual"] else ""
    return (
        f"Курс: 1 {trip['to_currency']} = {format_rate(trip['rate'])} "
        f"{trip['from_currency']}{manual}"
    )


def parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        return None
    return float(cleaned)


def no_trip_text() -> str:
    return (
        "Сначала создайте путешествие или выберите его в разделе «Мои путешествия»."
    )


def begin_new_trip(chat_id: int, user_id: int, intro: str = "") -> None:
    storage.set_state(user_id, "newtrip_from", {})
    prefix = f"{intro}\n\n" if intro else ""
    send(
        chat_id,
        prefix + "Из какой страны выезжаете? Напишите страну, например: Россия.",
        main_menu(),
    )


def ask_amount(chat_id: int, draft: dict) -> None:
    send(
        chat_id,
        (
            f"Курс сохранён: 1 {draft['to_currency']} = {format_rate(draft['rate'])} "
            f"{draft['from_currency']}.\n"
            f"Какую сумму берёте с собой в {draft['from_currency']}? "
            "Я конвертирую её через сервис курсов и создам баланс в обеих валютах.\n"
            "Например: 50000"
        ),
        main_menu(),
    )


def show_trips(chat_id: int, user_id: int) -> None:
    storage.clear_state(user_id)
    trips = storage.list_trips(user_id)
    if not trips:
        send(
            chat_id,
            "У вас пока нет путешествий. Нажмите «Создать новое путешествие».",
            main_menu(),
        )
        return
    active = storage.get_active_trip(user_id)
    active_id = active["id"] if active else None
    keyboard = types.InlineKeyboardMarkup()
    for trip in trips:
        mark = "✅ " if trip["id"] == active_id else ""
        label = (
            f"{mark}{trip['title']} · {format_amount(trip['dest_balance'])} "
            f"{trip['to_currency']}"
        )
        keyboard.add(
            types.InlineKeyboardButton(label[:60], callback_data=f"switch:{trip['id']}")
        )
    keyboard.add(types.InlineKeyboardButton("В меню", callback_data="menu:home"))
    send(chat_id, "Ваши путешествия. Нажмите, чтобы переключить кошелёк:", keyboard)


def show_balance(chat_id: int, user_id: int) -> None:
    storage.clear_state(user_id)
    trip = storage.get_active_trip(user_id)
    if trip is None:
        send(chat_id, no_trip_text(), main_menu())
        return
    send(
        chat_id,
        (
            f"Путешествие «{trip['title']}»: {trip['from_country']} → {trip['to_country']}\n"
            f"{rate_line(trip)}\n"
            f"{balance_line(trip)}"
        ),
        main_menu(),
    )


def show_history(chat_id: int, user_id: int) -> None:
    storage.clear_state(user_id)
    trip = storage.get_active_trip(user_id)
    if trip is None:
        send(chat_id, no_trip_text(), main_menu())
        return
    expenses = storage.list_expenses(user_id, trip["id"], limit=21)
    if not expenses:
        send(
            chat_id,
            f"По путешествию «{trip['title']}» расходов пока нет.\n{balance_line(trip)}",
            main_menu(),
        )
        return
    visible = expenses[:20]
    lines = [f"История расходов — «{trip['title']}»:", *category_summary(user_id, trip)]
    for item in visible:
        lines.append(
            f"{format_when(item['created_at'])} — {category_title(item.get('category') or '')} — "
            f"{format_amount(item['dest_amount'])} {item['to_currency']} = "
            f"{format_amount(item['home_amount'])} {item['from_currency']}"
        )
    if len(expenses) > 20:
        lines.append("Показаны последние 20 записей.")
    lines.append(balance_line(trip))
    send(chat_id, "\n".join(lines), main_menu())


def category_summary(user_id: int, trip: dict) -> list[str]:
    totals = {
        (item["category"] or ""): item
        for item in storage.expense_totals(user_id, trip["id"])
    }
    lines = ["По категориям:"]
    shown: set[str] = set()
    for code, title in CATEGORIES:
        item = totals.get(code)
        if item is None:
            continue
        shown.add(code)
        lines.append(
            f"{title} — {format_amount(item['dest_amount'])} {trip['to_currency']} = "
            f"{format_amount(item['home_amount'])} {trip['from_currency']}"
        )
    for code, item in totals.items():
        if code in shown:
            continue
        lines.append(
            f"{category_title(code)} — {format_amount(item['dest_amount'])} {trip['to_currency']} = "
            f"{format_amount(item['home_amount'])} {trip['from_currency']}"
        )
    return lines


def begin_setrate(chat_id: int, user_id: int) -> None:
    trip = storage.get_active_trip(user_id)
    if trip is None:
        storage.clear_state(user_id)
        send(chat_id, no_trip_text(), main_menu())
        return
    storage.set_state(user_id, "setrate", {"trip_id": trip["id"]})
    send(
        chat_id,
        (
            f"Путешествие «{trip['title']}».\n"
            f"Сейчас 1 {trip['to_currency']} = {format_rate(trip['rate'])} "
            f"{trip['from_currency']}.\n"
            f"Введите новый курс: сколько {trip['from_currency']} дают за 1 {trip['to_currency']}.\n"
            "Например: 12.8\n"
            "Он будет использоваться для следующих расходов. Уже записанный баланс не пересчитается."
        ),
        main_menu(),
    )


def welcome_text(user_id: int, first_name: str) -> str:
    hello = f"Привет, {first_name}!" if first_name else "Привет!"
    lines = [
        hello,
        "Я мини-кошелёк для путешествия.",
        "Можно завести несколько поездок и переключаться между ними.",
        "Число в чате я считаю расходом в валюте страны, где вы находитесь.",
        "После подтверждения выберите категорию: транспорт, жильё, еда или развлечения.",
    ]
    trip = storage.get_active_trip(user_id)
    if trip is None:
        lines.append("Давайте создадим первое путешествие.")
    else:
        lines.append(f"Сейчас активно «{trip['title']}».")
        lines.append(balance_line(trip))
        lines.append("Можно создать ещё одно путешествие.")
    return "\n".join(lines)


def handle_from_country(chat_id: int, user_id: int, text: str) -> None:
    if len(text.strip()) > 60:
        send(chat_id, "Слишком длинное название. Напишите страну короче, например: Россия.")
        return
    place = resolve_place(text)
    if place is None:
        send(
            chat_id,
            "Не знаю такую страну. Напишите страну, например Россия, или код валюты, например RUB.",
        )
        return
    name, code = place
    storage.set_state(
        user_id,
        "newtrip_to",
        {"from_country": name, "from_currency": code},
    )
    send(
        chat_id,
        f"Страна отправления: {name} ({code}).\nКуда направляетесь? Например: Китай.",
    )


def handle_to_country(chat_id: int, user_id: int, text: str, draft: dict) -> None:
    if len(text.strip()) > 60:
        send(chat_id, "Слишком длинное название. Напишите страну короче, например: Китай.")
        return
    place = resolve_place(text)
    if place is None:
        send(
            chat_id,
            "Не знаю такую страну. Напишите страну, например Китай, или код валюты, например CNY.",
        )
        return
    name, code = place
    if name == draft.get("from_country") and code == draft.get("from_currency"):
        send(chat_id, "Страна назначения совпадает со страной отправления. Выберите другую.")
        return
    quoted = convert_amount(1, code, draft["from_currency"])
    draft.update(
        {
            "to_country": name,
            "to_currency": code,
            "rate": quoted["result"],
            "rate_is_manual": False,
        }
    )
    storage.set_state(user_id, "newtrip_confirm", draft)
    send(
        chat_id,
        (
            f"Для {draft['from_country']} доступна валюта {draft['from_currency']}, "
            f"для {name} — {code}.\n"
            f"Текущий курс: 1 {code} = {format_rate(quoted['result'])} {draft['from_currency']}.\n"
            "Подходит этот курс?"
        ),
        rate_keyboard(),
    )


def accept_api_rate(chat_id: int, user_id: int) -> None:
    state, draft = storage.get_state(user_id)
    if state != "newtrip_confirm" or "rate" not in draft:
        send(chat_id, "Это предложение курса уже неактуально. Создайте путешествие заново.", main_menu())
        return
    draft["rate_is_manual"] = False
    storage.set_state(user_id, "newtrip_amount", draft)
    ask_amount(chat_id, draft)


def ask_manual_rate(chat_id: int, user_id: int) -> None:
    state, draft = storage.get_state(user_id)
    if state != "newtrip_confirm":
        send(chat_id, "Это предложение курса уже неактуально. Создайте путешествие заново.", main_menu())
        return
    storage.set_state(user_id, "newtrip_manual", draft)
    send(
        chat_id,
        (
            "Хорошо, введите свой курс по местному обменнику.\n"
            f"Сколько {draft['from_currency']} дают за 1 {draft['to_currency']}?\n"
            f"Например, 12.8 означает, что 1 {draft['to_currency']} = 12.8 {draft['from_currency']}."
        ),
        main_menu(),
    )


def handle_manual_rate(chat_id: int, user_id: int, text: str, draft: dict) -> None:
    rate = read_positive(chat_id, text, "Не получилось прочитать курс. Напишите число, например 12.8.")
    if rate is None:
        return
    draft["rate"] = rate
    draft["rate_is_manual"] = True
    storage.set_state(user_id, "newtrip_amount", draft)
    ask_amount(chat_id, draft)


def handle_initial_amount(chat_id: int, user_id: int, text: str, draft: dict) -> None:
    amount = read_positive(
        chat_id,
        text,
        f"Не получилось прочитать сумму. Напишите число в {draft['from_currency']}, например 50000.",
    )
    if amount is None:
        return
    quoted = convert_amount(amount, draft["from_currency"], draft["to_currency"])
    trip = storage.create_trip(
        user_id=user_id,
        from_country=draft["from_country"],
        to_country=draft["to_country"],
        from_currency=draft["from_currency"],
        to_currency=draft["to_currency"],
        rate=draft["rate"],
        rate_is_manual=bool(draft["rate_is_manual"]),
        home_balance=amount,
        dest_balance=quoted["result"],
    )
    manual_note = ""
    if trip["rate_is_manual"]:
        manual_note = (
            "\nСтартовый баланс посчитан сервисом курсов. "
            "Введённый курс сохранён для следующих расходов."
        )
    send(
        chat_id,
        (
            f"Путешествие «{trip['title']}» создано.\n"
            f"Валюты: {trip['from_country']} — {trip['from_currency']}, "
            f"{trip['to_country']} — {trip['to_currency']}.\n"
            f"{rate_line(trip)}\n"
            f"Стартовая сумма {format_amount(trip['home_balance'])} {trip['from_currency']} "
            f"конвертирована в {format_amount(trip['dest_balance'])} {trip['to_currency']}.\n"
            f"{balance_line(trip)}"
            f"{manual_note}\n\n"
            f"Теперь отправьте число — я пойму его как расход в {trip['to_currency']}."
        ),
        main_menu(),
    )


def read_positive(chat_id: int, text: str, invalid_message: str) -> float | None:
    amount = parse_amount(text)
    if amount is None:
        send(chat_id, invalid_message)
        return None
    if amount <= 0:
        send(chat_id, "Нужно число больше нуля.")
        return None
    if amount > 1_000_000_000_000:
        send(chat_id, "Слишком большая сумма. Введите число поменьше.")
        return None
    return amount


def handle_setrate(chat_id: int, user_id: int, text: str) -> None:
    trip = storage.get_active_trip(user_id)
    if trip is None:
        storage.clear_state(user_id)
        send(chat_id, no_trip_text(), main_menu())
        return
    rate = read_positive(chat_id, text, "Не получилось прочитать курс. Напишите число, например 12.8.")
    if rate is None:
        return
    updated = storage.update_rate(user_id, trip["id"], rate)
    storage.clear_state(user_id)
    if updated is None:
        send(chat_id, "Не удалось сохранить курс: путешествие не найдено.", main_menu())
        return
    send(
        chat_id,
        (
            f"Новый курс сохранён: 1 {updated['to_currency']} = "
            f"{format_rate(updated['rate'])} {updated['from_currency']}.\n"
            "Следующие расходы посчитаю по нему.\n"
            f"{balance_line(updated)}"
        ),
        main_menu(),
    )


def handle_expense(chat_id: int, user_id: int, text: str) -> None:
    amount = parse_amount(text)
    if amount is None:
        send(
            chat_id,
            "Я понимаю сумму числом, например 100. Или выберите действие в меню.",
            main_menu(),
        )
        return
    if amount <= 0:
        send(chat_id, "Нужна сумма больше нуля.", main_menu())
        return
    if amount > 1_000_000_000_000:
        send(chat_id, "Слишком большая сумма. Введите число поменьше.", main_menu())
        return
    trip = storage.get_active_trip(user_id)
    if trip is None:
        send(chat_id, no_trip_text(), main_menu())
        return
    if trip["rate_is_manual"]:
        home_amount = round(amount * trip["rate"], 2)
        rate = trip["rate"]
    else:
        quoted = convert_amount(amount, trip["to_currency"], trip["from_currency"])
        home_amount = round(quoted["result"], 2)
        rate = quoted["rate"]
    token = storage.save_pending(user_id, trip["id"], amount, home_amount, rate)
    warning = ""
    if amount > trip["dest_balance"] + 0.009:
        warning = "\nСумма больше остатка. Если записать расход, баланс станет отрицательным."
    send(
        chat_id,
        (
            f"{format_amount(amount)} {trip['to_currency']} = "
            f"{format_amount(home_amount)} {trip['from_currency']}\n"
            f"Учесть как расход?{warning}"
        ),
        expense_keyboard(token),
    )


def ask_expense_category(chat_id: int, user_id: int, token: str) -> None:
    pending = storage.get_pending(user_id, token)
    if pending is None:
        send(chat_id, "Эта сумма уже обработана или устарела. Отправьте её ещё раз, если нужно.", main_menu())
        return
    send(
        chat_id,
        (
            f"{format_amount(pending['dest_amount'])} {pending['to_currency']} = "
            f"{format_amount(pending['home_amount'])} {pending['from_currency']}\n"
            "К какой категории отнести расход?"
        ),
        category_keyboard(token),
    )


def confirm_expense(chat_id: int, user_id: int, token: str, category: str) -> None:
    title = CATEGORY_TITLES.get(category)
    if title is None:
        send(chat_id, "Выберите категорию кнопкой: транспорт, жильё, еда или развлечения.")
        return
    result = storage.apply_expense(user_id, token, category)
    if result is None:
        send(chat_id, "Эта сумма уже обработана или устарела. Отправьте её ещё раз, если нужно.", main_menu())
        return
    send(
        chat_id,
        (
            f"Записала расход — {title}: {format_amount(result['dest_amount'])} {result['to_currency']} = "
            f"{format_amount(result['home_amount'])} {result['from_currency']}\n"
            f"{balance_line(result)}"
        ),
        main_menu(),
    )


def decline_expense(chat_id: int, user_id: int, token: str) -> None:
    removed = storage.discard_pending(user_id, token)
    if not removed:
        send(chat_id, "Эта сумма уже обработана.", main_menu())
        return
    send(chat_id, "Хорошо, эту сумму не записываю.", main_menu())


def switch_trip(chat_id: int, user_id: int, trip_id: int) -> None:
    trip = storage.set_active_trip(user_id, trip_id)
    if trip is None:
        send(chat_id, "Это путешествие не найдено.", main_menu())
        return
    send(
        chat_id,
        f"Активное путешествие: «{trip['title']}».\n{rate_line(trip)}\n{balance_line(trip)}",
        main_menu(),
    )


def dispatch_text(message: telebot.types.Message) -> None:
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if text.startswith("/"):
        send(chat_id, "Такой команды нет. Выберите действие в меню.", main_menu())
        return
    state, draft = storage.get_state(user_id)
    if state == "newtrip_from":
        handle_from_country(chat_id, user_id, text)
    elif state == "newtrip_to":
        handle_to_country(chat_id, user_id, text, draft)
    elif state == "newtrip_confirm":
        answer = text.lower().replace("ё", "е")
        if answer in YES_WORDS:
            accept_api_rate(chat_id, user_id)
        elif answer in NO_WORDS:
            ask_manual_rate(chat_id, user_id)
        else:
            send(chat_id, "Нажмите «Да» или «Нет» под курсом. Если курс не подходит, можно ввести свой.")
    elif state == "newtrip_manual":
        handle_manual_rate(chat_id, user_id, text, draft)
    elif state == "newtrip_amount":
        handle_initial_amount(chat_id, user_id, text, draft)
    elif state == "setrate":
        handle_setrate(chat_id, user_id, text)
    else:
        handle_expense(chat_id, user_id, text)


def clear_markup(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        logger.debug("Не удалось убрать кнопки", exc_info=True)


def register() -> None:
    @bot.message_handler(commands=["start"])
    def on_start(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        user_id = message.from_user.id
        storage.ensure_user(user_id)
        intro = welcome_text(user_id, message.from_user.first_name or "")
        if storage.get_active_trip(user_id) is None and not storage.list_trips(user_id):
            begin_new_trip(message.chat.id, user_id, intro)
            return
        send(message.chat.id, intro, main_menu())

    @bot.message_handler(commands=["newtrip"])
    def on_newtrip(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        begin_new_trip(message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["switch"])
    def on_switch(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        show_trips(message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["balance"])
    def on_balance(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        show_balance(message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["history"])
    def on_history(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        show_history(message.chat.id, message.from_user.id)

    @bot.message_handler(commands=["setrate"])
    def on_setrate(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        begin_setrate(message.chat.id, message.from_user.id)

    @bot.message_handler(content_types=["text"])
    def on_text(message: telebot.types.Message) -> None:
        if message.from_user is None:
            return
        storage.ensure_user(message.from_user.id)
        try:
            dispatch_text(message)
        except ExchangeRateError as exc:
            send(message.chat.id, str(exc), main_menu())
        except Exception:
            logger.exception("Ошибка обработки сообщения")
            send(message.chat.id, GENERIC_ERROR, main_menu())

    @bot.message_handler(
        content_types=["photo", "sticker", "voice", "document", "audio", "video", "location", "contact"]
    )
    def on_other(message: telebot.types.Message) -> None:
        send(
            message.chat.id,
            "Я понимаю текстовые сообщения: страну, курс или сумму расхода.",
            main_menu(),
        )

    @bot.callback_query_handler(func=lambda call: True)
    def on_callback(call: telebot.types.CallbackQuery) -> None:
        if call.from_user is None:
            return
        try:
            bot.answer_callback_query(call.id)
        except Exception as exc:
            logger.error("Не удалось ответить на нажатие кнопки: %s", type(exc).__name__)
        user_id = call.from_user.id
        chat_id = call.message.chat.id if call.message else call.from_user.id
        storage.ensure_user(user_id)
        data = call.data or ""
        try:
            if data in {"rate:yes", "rate:no", "expense:yes", "expense:no"} or data.startswith("expense:"):
                clear_markup(call)
            if data == "menu:home":
                storage.clear_state(user_id)
                send(chat_id, "Главное меню. Выберите действие:", main_menu())
            elif data == "menu:newtrip":
                begin_new_trip(chat_id, user_id)
            elif data == "menu:trips":
                show_trips(chat_id, user_id)
            elif data == "menu:balance":
                show_balance(chat_id, user_id)
            elif data == "menu:history":
                show_history(chat_id, user_id)
            elif data == "menu:setrate":
                begin_setrate(chat_id, user_id)
            elif data.startswith("switch:"):
                switch_trip(chat_id, user_id, int(data.split(":", 1)[1]))
            elif data == "rate:yes":
                accept_api_rate(chat_id, user_id)
            elif data == "rate:no":
                ask_manual_rate(chat_id, user_id)
            elif data.startswith("expense:yes:"):
                ask_expense_category(chat_id, user_id, data.split(":", 2)[2])
            elif data.startswith("expense:cat:"):
                _, _, category, token = data.split(":", 3)
                confirm_expense(chat_id, user_id, token, category)
            elif data.startswith("expense:no:"):
                decline_expense(chat_id, user_id, data.split(":", 2)[2])
            else:
                send(chat_id, "Эта кнопка больше не работает. Откройте меню ещё раз.", main_menu())
        except ExchangeRateError as exc:
            send(chat_id, str(exc), main_menu())
        except (ValueError, IndexError):
            send(chat_id, "Не удалось разобрать кнопку. Откройте меню ещё раз.", main_menu())
        except Exception:
            logger.exception("Ошибка обработки кнопки")
            send(chat_id, GENERIC_ERROR, main_menu())


def main() -> None:
    global bot
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Добавьте TELEGRAM_BOT_TOKEN второй строкой в файл .env и запустите бота снова.")
        return
    storage.init_db()
    bot = telebot.TeleBot(token)
    register()
    try:
        bot.get_me()
    except Exception as exc:
        logger.error("Telegram не принял токен: %s", type(exc).__name__)
        print("Не удалось подключиться к Telegram. Проверьте TELEGRAM_BOT_TOKEN в файле .env.")
        return
    print("Бот запущен. Остановить: Ctrl+C.")
    bot.infinity_polling(skip_pending=True, timeout=30)


if __name__ == "__main__":
    main()
