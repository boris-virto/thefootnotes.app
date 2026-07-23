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
from .config import FILES_DIR, TELEGRAM_BOT_TOKEN, TIMEZONE, user_allowed
from .llm import ExtractedReminder

logger = logging.getLogger(__name__)

CATEGORY_EMOJI = {"task": "✅", "event": "📅", "ticket": "🎫", "note": "📝"}

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
    lines = [f"{emoji} <b>{reminder.title}</b>"]
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
        "Команды:\n/list — показать напоминания\n"
        "/reminders — регулярные напоминания и выключатель\n"
        "/files — скачать сохранённые билеты и файлы"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    reminders = await asyncio.to_thread(db.list_reminders)
    if not reminders:
        await update.message.reply_text("Пока пусто. Кинь мне первое напоминание 🙂")
        return
    blocks = []
    for r in reminders:
        emoji = CATEGORY_EMOJI.get(r.category, "📝")
        line = f"{emoji} <b>{r.title}</b>"
        if r.event_date:
            line += f" — {r.event_date.strftime('%d.%m')}"
            if r.event_time:
                line += f" {r.event_time}"
        blocks.append(line)
    await update.message.reply_text("\n".join(blocks), parse_mode="HTML")


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
    app.add_handler(CommandHandler("reminders", reminders_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CallbackQueryHandler(on_stop_reminder, pattern=r"^stop:"))
    app.add_handler(CallbackQueryHandler(on_send_file, pattern=r"^file:"))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
