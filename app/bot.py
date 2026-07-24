"""Телеграм-бот: принимает текст, войсы, фото и PDF и превращает их в напоминания."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

from . import db, llm, transcribe
from .config import (
    DIGEST_CHAT_ID,
    DIGEST_TIME,
    FILES_DIR,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    user_allowed,
)
from .llm import ExtractedReminder

logger = logging.getLogger(__name__)

CATEGORY_EMOJI = {"task": "✅", "event": "📅", "ticket": "🎫", "note": "📝"}

# Важность: строка от LLM -> число в БД, и отображение.
IMPORTANCE_FROM_LLM = {"high": 3, "normal": 2, "low": 1}
IMPORTANCE_EMOJI = {3: "🔴", 2: "🟡", 1: "🟢"}
STATUS_NAME = {"todo": "TODO", "doing": "В процессе", "done": "Сделано", "archived": "Архив"}

# Куда можно перевести карточку из текущего статуса (подпись, новый статус).
STATUS_TRANSITIONS = {
    "todo": [("▶️ В работу", "doing"), ("✅ Готово", "done")],
    "doing": [("✅ Готово", "done"), ("↩️ В TODO", "todo")],
    "done": [("🗄 В архив", "archived"), ("↩️ Вернуть", "doing")],
}

_URL_RE = re.compile(r"https?://\S+")


def _find_urls(text: str) -> list[str]:
    """Находит ссылки в тексте и отрезает хвостовую пунктуацию."""
    return [u.rstrip(").,;!?") for u in _URL_RE.findall(text)]


def _is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


# --- Регулярные напоминания ---------------------------------------------------

DEFAULT_REMIND_TIME = "09:00"

# В этой версии PTB run_daily считает: 0=вс, 1=пн, ... 6=сб.
_DAY_TO_PTB = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_DAY_RU = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда", "thu": "четверг",
    "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}


def _parse_time(hhmm: str | None) -> dtime:
    """'09:30' -> datetime.time с нужным часовым поясом. Кривой ввод -> 09:00."""
    try:
        hour, minute = (int(x) for x in (hhmm or DEFAULT_REMIND_TIME).split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    return dtime(hour=hour, minute=minute, tzinfo=ZoneInfo(TIMEZONE))


def recurrence_human(recurrence: str | None, remind_time: str | None) -> str:
    """Человекочитаемое описание регулярности для сообщений."""
    at = f" в {remind_time or DEFAULT_REMIND_TIME}"
    if recurrence == "daily":
        return f"каждый день{at}"
    if recurrence and recurrence.startswith("weekly:"):
        day = _DAY_RU.get(recurrence.split(":", 1)[1], "?")
        return f"каждую неделю ({day}){at}"
    if recurrence and recurrence.startswith("monthly:"):
        return f"каждое {recurrence.split(':', 1)[1]}-е число{at}"
    return ""


async def _fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback задачи: шлём напоминание в Telegram, если оно ещё активно."""
    reminder = await asyncio.to_thread(db.get_reminder, context.job.data)
    if not reminder or not reminder.remind_active or not reminder.chat_id:
        return
    emoji = CATEGORY_EMOJI.get(reminder.category, "📝")
    text = f"🔔 Напоминание: {emoji} <b>{reminder.title}</b>"
    if reminder.location:
        text += f"\n📍 {reminder.location}"
    await context.bot.send_message(reminder.chat_id, text, parse_mode="HTML")


def schedule_reminder(job_queue: JobQueue, reminder: db.Reminder) -> None:
    """Ставит (или переставляет) задачу для регулярного напоминания."""
    if not reminder.recurrence or not reminder.remind_active or not reminder.chat_id:
        return
    cancel_reminder(job_queue, reminder.id)  # убрать старую задачу, если была
    name = f"rem:{reminder.id}"
    when = _parse_time(reminder.remind_time)
    kind = reminder.recurrence
    if kind == "daily":
        job_queue.run_daily(_fire_reminder, time=when, data=reminder.id, name=name)
    elif kind.startswith("weekly:"):
        day = _DAY_TO_PTB.get(kind.split(":", 1)[1])
        if day is not None:
            job_queue.run_daily(_fire_reminder, time=when, days=(day,), data=reminder.id, name=name)
    elif kind.startswith("monthly:"):
        try:
            dom = int(kind.split(":", 1)[1])
            job_queue.run_monthly(_fire_reminder, when=when, day=dom, data=reminder.id, name=name)
        except ValueError:
            logger.warning("Некорректная периодичность monthly: %s", kind)


def cancel_reminder(job_queue: JobQueue, reminder_id: int) -> None:
    for job in job_queue.get_jobs_by_name(f"rem:{reminder_id}"):
        job.schedule_removal()


# --- Напоминание о событии за день до даты -----------------------------------

EVENT_PING_TIME = "09:00"


def _event_ping_time(reminder: db.Reminder) -> datetime | None:
    """Когда сработает пинг «за день до». None — если события/даты нет,
    оно выполнено, некуда слать, или момент уже прошёл."""
    if not reminder.event_date or reminder.done or not reminder.chat_id:
        return None
    fire = datetime.combine(
        reminder.event_date - timedelta(days=1), _parse_time(EVENT_PING_TIME)
    )
    if fire <= datetime.now(ZoneInfo(TIMEZONE)):
        return None
    return fire


async def _fire_event_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder = await asyncio.to_thread(db.get_reminder, context.job.data)
    if not reminder or reminder.done or not reminder.chat_id:
        return
    emoji = CATEGORY_EMOJI.get(reminder.category, "📝")
    text = f"⏰ Завтра: {emoji} <b>{reminder.title}</b>"
    if reminder.event_time:
        text += f"\n🕐 {reminder.event_time}"
    if reminder.location:
        text += f"\n📍 {reminder.location}"
    await context.bot.send_message(reminder.chat_id, text, parse_mode="HTML")


def schedule_event_reminder(job_queue: JobQueue, reminder: db.Reminder) -> bool:
    """Ставит одноразовый пинг на 09:00 за день до события.
    Возвращает True, если задача действительно поставлена."""
    fire = _event_ping_time(reminder)
    if fire is None:
        return False
    name = f"event:{reminder.id}"
    for job in job_queue.get_jobs_by_name(name):  # снять старую, если была
        job.schedule_removal()
    job_queue.run_once(_fire_event_reminder, when=fire, data=reminder.id, name=name)
    return True


# --- Утренний дайджест --------------------------------------------------------


def _digest_line(reminder: db.Reminder, *, with_date: bool = True) -> str:
    emoji = CATEGORY_EMOJI.get(reminder.category, "📝")
    imp = IMPORTANCE_EMOJI.get(reminder.importance, "") if reminder.importance == 3 else ""
    line = f"{imp}{' ' if imp else ''}{emoji} <b>{reminder.title}</b>"
    if with_date and reminder.event_date:
        line += f" — {reminder.event_date.strftime('%d.%m')}"
    if reminder.event_time:
        line += f" {reminder.event_time}"
    if reminder.location:
        line += f"\n   📍 {reminder.location}"
    return line


# Ритм напоминаний о будущих событиях в автодайджесте. «Корзины» по числу дней до
# события (по возрастанию): в пределах месяца (≤30) напоминаем каждый день, а рубежи
# 60 и 90 дней — по одному разу. Точную дату рубежа не ловим: как только событие
# впервые попадает в корзину, шлём напоминание и запоминаем рубеж (digest_milestone),
# поэтому пропущенная утром рассылка не теряет «разовое» напоминание.
DIGEST_DAILY_DAYS = 30
_DIGEST_BUCKETS = (30, 60, 90)  # по возрастанию; 30 = ежедневная зона


def _digest_bucket(days: int) -> int | None:
    """Наименьший рубеж, в который укладывается число дней до события.
    None — событие ещё слишком далеко (дальше самого дальнего рубежа)."""
    for bucket in _DIGEST_BUCKETS:
        if days <= bucket:
            return bucket
    return None


def _auto_digest_decision(reminder: db.Reminder, today) -> tuple[bool, int | None]:
    """Для автодайджеста: (показывать ли, каким должен стать digest_milestone).
    События без даты / просроченные / сегодняшние показываем всегда и рубеж не трогаем.
    Будущие — по корзинам: ежедневная зона показывается каждый день, рубежи 60/90 —
    только если ещё не отправляли этот (или более близкий) рубеж."""
    d = reminder.event_date
    if d is None or d <= today:
        return True, reminder.digest_milestone
    bucket = _digest_bucket((d - today).days)
    if bucket is None:
        # Слишком далеко (или событие перенесли дальше 3 месяцев) — сбрасываем рубеж,
        # чтобы при возвращении в окно напоминания сработали заново.
        return False, None
    if bucket == DIGEST_DAILY_DAYS:
        return True, DIGEST_DAILY_DAYS
    stored = reminder.digest_milestone
    if stored is None or stored > bucket:
        return True, bucket  # этот рубеж ещё не отправляли
    return False, stored


def _format_digest(reminders: list[db.Reminder], today) -> str | None:
    """Собирает текст дайджеста, разбивая напоминания на секции по дате.
    Список уже должен быть отфильтрован вызывающим. Возвращает None, если пусто."""
    overdue, todays, upcoming, undated = [], [], [], []
    for r in reminders:
        if r.event_date is None:
            undated.append(r)
        elif r.event_date < today:
            overdue.append(r)
        elif r.event_date == today:
            todays.append(r)
        else:
            upcoming.append(r)
    if not (overdue or todays or upcoming or undated):
        return None

    parts = [f"☀️ <b>Доброе утро!</b> Напоминания на {today.strftime('%d.%m.%Y')}"]
    if todays:
        parts.append("\n<b>📌 Сегодня:</b>")
        parts += [_digest_line(r, with_date=False) for r in todays]
    if overdue:
        parts.append("\n<b>⚠️ Просрочено:</b>")
        parts += [_digest_line(r) for r in sorted(overdue, key=lambda r: r.event_date)]
    if upcoming:
        parts.append("\n<b>🗓 Скоро:</b>")
        parts += [_digest_line(r) for r in sorted(upcoming, key=lambda r: r.event_date)]
    if undated:
        parts.append("\n<b>📝 Без даты:</b>")
        parts += [_digest_line(r, with_date=False) for r in undated]
    return "\n".join(parts)


async def _send_morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ежедневной задачи: рассылает список актуальных напоминаний."""
    reminders = await asyncio.to_thread(db.list_active)  # todo + в процессе
    if not reminders:
        return
    today = datetime.now(ZoneInfo(TIMEZONE)).date()

    # Решаем, что показать сегодня, и запоминаем отправленные рубежи.
    visible = []
    for r in reminders:
        show, milestone = _auto_digest_decision(r, today)
        if milestone != r.digest_milestone:
            await asyncio.to_thread(db.set_digest_milestone, r.id, milestone)
        if show:
            visible.append(r)
    if not visible:
        return
    reminders = visible

    # Кому и что слать.
    if DIGEST_CHAT_ID is not None:
        groups = {DIGEST_CHAT_ID: reminders}
    else:
        chat_ids = {r.chat_id for r in reminders if r.chat_id is not None}
        if not chat_ids:
            logger.info("Дайджест: некому слать (нет chat_id и DIGEST_CHAT_ID не задан).")
            return
        if len(chat_ids) == 1:
            # Единственный адресат получает всё, включая старые записи без chat_id.
            groups = {next(iter(chat_ids)): reminders}
        else:
            # Несколько чатов — каждому только его, чтобы не смешивать чужое.
            groups = {cid: [r for r in reminders if r.chat_id == cid] for cid in chat_ids}

    for chat_id, items in groups.items():
        text = _format_digest(items, today)
        if not text:
            continue
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось отправить дайджест в чат %s", chat_id)


def schedule_digest(job_queue: JobQueue) -> None:
    """Ставит ежедневную задачу дайджеста на DIGEST_TIME (перепланировав старую)."""
    for job in job_queue.get_jobs_by_name("digest"):
        job.schedule_removal()
    job_queue.run_daily(_send_morning_digest, time=_parse_time(DIGEST_TIME), name="digest")


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _save(
    extracted: ExtractedReminder,
    *,
    source: str,
    raw_text: str | None,
    file_path: str | None,
    chat_id: int,
):
    recurrence = extracted.recurrence
    remind_time = extracted.remind_time or (DEFAULT_REMIND_TIME if recurrence else None)
    importance = IMPORTANCE_FROM_LLM.get((extracted.importance or "normal").lower(), 2)
    return db.add_reminder(
        title=extracted.title,
        category=extracted.category or "note",
        event_date=_parse_date(extracted.event_date),
        event_time=extracted.event_time,
        location=extracted.location,
        notes=extracted.notes,
        source=source,
        raw_text=raw_text,
        file_path=file_path,
        chat_id=chat_id,
        recurrence=recurrence,
        remind_time=remind_time,
        importance=importance,
    )


async def _save_and_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    extracted: ExtractedReminder,
    *,
    source: str,
    raw_text: str | None = None,
    file_path: str | None = None,
) -> db.Reminder:
    """Сохраняет напоминание и, если оно регулярное, ставит задачу в расписание."""
    reminder = await asyncio.to_thread(
        _save,
        extracted,
        source=source,
        raw_text=raw_text,
        file_path=file_path,
        chat_id=update.effective_chat.id,
    )
    if reminder.recurrence and reminder.remind_active:
        schedule_reminder(context.application.job_queue, reminder)
    if reminder.event_date:
        schedule_event_reminder(context.application.job_queue, reminder)
    return reminder


def _confirmation(reminder: db.Reminder) -> str:
    emoji = CATEGORY_EMOJI.get(reminder.category, "📝")
    imp = IMPORTANCE_EMOJI.get(reminder.importance, "")
    prefix = f"{imp} " if imp else ""
    lines = [f"{prefix}{emoji} <b>{reminder.title}</b>"]
    if reminder.event_date:
        when = reminder.event_date.strftime("%d.%m.%Y")
        if reminder.event_time:
            when += f" в {reminder.event_time}"
        lines.append(f"🗓 {when}")
    if reminder.location:
        lines.append(f"📍 {reminder.location}")
    if reminder.notes:
        lines.append(f"💬 {reminder.notes}")
    if _event_ping_time(reminder):
        lines.append("⏰ Напомню за день до события.")
    if reminder.recurrence and reminder.remind_active:
        lines.append(f"\n🔔 Напоминаю: {recurrence_human(reminder.recurrence, reminder.remind_time)}")
        lines.append("Выключить: /reminders")
    else:
        lines.append("\nСохранил. Посмотреть всё: /list")
    return "\n".join(lines)


async def _guard(update: Update) -> bool:
    """Проверяем доступ; при отказе отвечаем и возвращаем False."""
    user = update.effective_user
    if user and user_allowed(user.id):
        return True
    if update.message:
        await update.message.reply_text(
            f"Доступ закрыт. Твой user id: {user.id if user else '?'} — "
            "добавь его в ALLOWED_USER_IDS."
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.reply_text(
        "Привет! Кидай сюда что нужно не забыть — текстом, голосом, "
        "скриншотом билета или PDF. Я разберу и сохраню.\n\n"
        "Могу напоминать регулярно — просто напиши «напоминать каждый понедельник в 9:00».\n\n"
        "Команды:\n/list — доска задач с кнопками статуса\n"
        "/digest — утренний дайджест прямо сейчас\n"
        "/reminders — регулярные напоминания и выключатель\n"
        "/files — скачать сохранённые билеты и файлы"
    )


def _list_line(reminder: db.Reminder) -> str:
    """Одна карточка для /list: важность, категория, название, дата и статус."""
    emoji = CATEGORY_EMOJI.get(reminder.category, "📝")
    imp = IMPORTANCE_EMOJI.get(reminder.importance, "🟡")
    line = f"{imp} {emoji} <b>{reminder.title}</b>"
    if reminder.event_date:
        line += f" — {reminder.event_date.strftime('%d.%m')}"
        if reminder.event_time:
            line += f" {reminder.event_time}"
    line += f"\n<i>{STATUS_NAME.get(reminder.status, reminder.status)}</i>"
    return line


def _status_keyboard(reminder: db.Reminder) -> InlineKeyboardMarkup:
    """Кнопки перехода статуса, зависят от текущего статуса карточки."""
    row = [
        InlineKeyboardButton(text, callback_data=f"st:{reminder.id}:{status}")
        for text, status in STATUS_TRANSITIONS.get(reminder.status, [])
    ]
    return InlineKeyboardMarkup([row]) if row else InlineKeyboardMarkup([])


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    reminders = await asyncio.to_thread(db.list_board)  # todo + в процессе + сделано
    if not reminders:
        await update.message.reply_text("Пока пусто. Кинь мне первое напоминание 🙂")
        return
    for r in reminders:
        await update.message.reply_text(
            _list_line(r), parse_mode="HTML", reply_markup=_status_keyboard(r)
        )


async def on_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not (query.from_user and user_allowed(query.from_user.id)):
        await query.answer("Доступ закрыт")
        return
    _, sid, status = query.data.split(":")
    reminder_id = int(sid)
    await asyncio.to_thread(db.set_status, reminder_id, status)
    reminder = await asyncio.to_thread(db.get_reminder, reminder_id)
    await query.answer(f"Статус: {STATUS_NAME.get(status, status)}")

    # Пинг «за день до» нужен только активным задачам с будущей датой.
    jq = context.application.job_queue
    if status in ("done", "archived"):
        for job in jq.get_jobs_by_name(f"event:{reminder_id}"):
            job.schedule_removal()
    elif reminder:
        schedule_event_reminder(jq, reminder)

    try:
        if not reminder or status == "archived":
            title = reminder.title if reminder else ""
            await query.edit_message_text(f"🗄 В архив: {title}")
        else:
            await query.edit_message_text(
                _list_line(reminder), parse_mode="HTML", reply_markup=_status_keyboard(reminder)
            )
    except BadRequest as e:
        # Двойной тап на ту же кнопку -> «message is not modified». Это не ошибка.
        if "not modified" not in str(e).lower():
            raise


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать дайджест прямо сейчас (тот же, что уходит утром)."""
    if not await _guard(update):
        return
    reminders = await asyncio.to_thread(db.list_active)
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    # Ручной вызов — показываем всё будущее целиком, без «раз в месяц».
    text = _format_digest(reminders, today) or "Пока нечего показать — список пуст 🙂"
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    text = update.message.text
    await update.message.chat.send_action("typing")

    urls = _find_urls(text)
    pdf_urls = [u for u in urls if _is_pdf_url(u)]

    # Ссылка на PDF -> Claude читает файл по ссылке.
    if pdf_urls:
        url = pdf_urls[0]
        try:
            extracted = await asyncio.to_thread(llm.structure_pdf_url, url)
        except Exception:
            logger.exception("Не удалось прочитать PDF по ссылке")
            await update.message.reply_text(
                "Не смог открыть PDF по ссылке — возможно, он за логином или недоступен. "
                "Попробуй прикрепить файл через 📎."
            )
            return
        reminder = await _save_and_schedule(update, context, extracted, source="pdf", raw_text=url)
        await update.message.reply_text(_confirmation(reminder), parse_mode="HTML")
        return

    # Сообщение — это просто ссылка, но не на PDF: не сохраняем мусор.
    if len(urls) == 1 and text.strip() == urls[0]:
        await update.message.reply_text(
            "Это ссылка не на PDF. Пришли PDF файлом через 📎 или дай прямую ссылку "
            "на .pdf. Обычные веб-страницы я пока читать не умею."
        )
        return

    # Обычный текст (в т.ч. заметка со ссылкой внутри).
    extracted = await asyncio.to_thread(llm.structure_text, text)
    reminder = await _save_and_schedule(update, context, extracted, source="text", raw_text=text)
    await update.message.reply_text(_confirmation(reminder), parse_mode="HTML")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.chat.send_action("typing")
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
    audio = bytes(await tg_file.download_as_bytearray())

    text = await asyncio.to_thread(transcribe.transcribe, audio)
    extracted = await asyncio.to_thread(llm.structure_text, text)
    reminder = await _save_and_schedule(update, context, extracted, source="voice", raw_text=text)
    await update.message.reply_text(
        f"🎙 Расшифровал: <i>{text}</i>\n\n{_confirmation(reminder)}", parse_mode="HTML"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    await update.message.chat.send_action("typing")
    photo = update.message.photo[-1]  # самое большое разрешение
    tg_file = await context.bot.get_file(photo.file_id)
    image = bytes(await tg_file.download_as_bytearray())

    path = FILES_DIR / f"{photo.file_unique_id}.jpg"
    path.write_bytes(image)

    extracted = await asyncio.to_thread(llm.structure_image, image, "image/jpeg")
    reminder = await _save_and_schedule(update, context, extracted, source="photo", file_path=str(path))
    await update.message.reply_text(_confirmation(reminder), parse_mode="HTML")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Пока умею читать только PDF из документов.")
        return
    await update.message.chat.send_action("typing")
    tg_file = await context.bot.get_file(doc.file_id)
    pdf = bytes(await tg_file.download_as_bytearray())

    path = FILES_DIR / f"{doc.file_unique_id}.pdf"
    path.write_bytes(pdf)

    extracted = await asyncio.to_thread(llm.structure_pdf, pdf)
    reminder = await _save_and_schedule(update, context, extracted, source="pdf", file_path=str(path))
    await update.message.reply_text(_confirmation(reminder), parse_mode="HTML")


async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    chat_id = update.effective_chat.id
    active = [
        r for r in await asyncio.to_thread(db.list_active_recurring) if r.chat_id == chat_id
    ]
    if not active:
        await update.message.reply_text("Активных регулярных напоминаний нет.")
        return
    for r in active:
        emoji = CATEGORY_EMOJI.get(r.category, "📝")
        text = f"{emoji} <b>{r.title}</b>\n🔔 {recurrence_human(r.recurrence, r.remind_time)}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔕 Выключить", callback_data=f"stop:{r.id}")]]
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def on_stop_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Выключено")
    if not (query.from_user and user_allowed(query.from_user.id)):
        return
    reminder_id = int(query.data.split(":", 1)[1])
    await asyncio.to_thread(db.set_remind_active, reminder_id, False)
    cancel_reminder(context.application.job_queue, reminder_id)
    reminder = await asyncio.to_thread(db.get_reminder, reminder_id)
    title = reminder.title if reminder else ""
    await query.edit_message_text(f"🔕 Напоминание выключено: {title}")


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    chat_id = update.effective_chat.id
    items = [
        r
        for r in await asyncio.to_thread(db.list_with_files)
        if r.chat_id in (None, chat_id)
    ]
    if not items:
        await update.message.reply_text("Сохранённых файлов пока нет — пришли фото или PDF билета.")
        return
    for r in items:
        emoji = CATEGORY_EMOJI.get(r.category, "📝")
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📎 Скачать", callback_data=f"file:{r.id}")]]
        )
        await update.message.reply_text(
            f"{emoji} <b>{r.title}</b>", parse_mode="HTML", reply_markup=keyboard
        )


async def on_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not (query.from_user and user_allowed(query.from_user.id)):
        return
    reminder_id = int(query.data.split(":", 1)[1])
    reminder = await asyncio.to_thread(db.get_reminder, reminder_id)
    if not reminder or not reminder.file_path:
        await context.bot.send_message(query.message.chat_id, "Файл не найден.")
        return
    path = Path(reminder.file_path)
    if not path.exists():
        await context.bot.send_message(
            query.message.chat_id, "Файл больше недоступен на сервере."
        )
        return
    data = await asyncio.to_thread(path.read_bytes)
    filename = f"{reminder.title}{path.suffix}"
    await context.bot.send_document(query.message.chat_id, document=data, filename=filename)


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("digest", digest_cmd))
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CallbackQueryHandler(on_stop_reminder, pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(on_send_file, pattern=r"^file:"))
    app.add_handler(CallbackQueryHandler(on_set_status, pattern=r"^st:"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
