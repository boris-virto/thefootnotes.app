"""Захват: сырой вход (текст, файлы, аудио) -> разбор Claude -> сохранённая карточка.

Раньше это жило внутри хендлеров телеграм-бота, вперемешку с ответными сообщениями и
скачиванием по file_id. Теперь это отдельный слой без единого упоминания Telegram:
им пользуются и бот, и JSON API мобильного клиента — чтобы карточка из шортката на
телефоне разбиралась ровно так же, как присланная в чат.

Ошибки разделены на два вида:
  * Rejected — пользователь прислал то, что мы осознанно не берём (например ссылку на
    обычную веб-страницу). Это не сбой: текст ошибки можно показывать как есть.
  * Failed — что-то сломалось (модель, сеть, распознавание). Несёт `where` для логов и
    `user_message` для человека, а исходное исключение остаётся в __cause__.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db, links, llm, transcribe
from .config import FILES_DIR
from .llm import ExtractedReminder

logger = logging.getLogger(__name__)

# Важность: строка от LLM -> число в БД.
IMPORTANCE_FROM_LLM = {"high": 3, "normal": 2, "low": 1}

DEFAULT_REMIND_TIME = "09:00"

# Что умеем разбирать как файл. Всё остальное отвергаем до обращения к модели.
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
PDF_TYPE = "application/pdf"
SUPPORTED_TYPES = (*IMAGE_TYPES, PDF_TYPE)

_SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    PDF_TYPE: ".pdf",
}


class Rejected(Exception):
    """Вход осознанно не принят. Текст исключения предназначен пользователю."""


class Failed(Exception):
    """Сбой при разборе. where — метка для логов, user_message — текст для человека."""

    def __init__(self, user_message: str, *, where: str):
        super().__init__(user_message)
        self.user_message = user_message
        self.where = where

    @property
    def original(self) -> BaseException:
        """Исходное исключение (для трейсбека), если оно было."""
        return self.__cause__ or self


@dataclass
class IngestFile:
    """Один входящий файл. path=None — значит сохранить его должны мы сами."""

    data: bytes
    media_type: str
    path: str | None = None


@dataclass
class IngestResult:
    reminder: db.Reminder
    # Расшифровка голосового — её показывают пользователю вместе с карточкой.
    transcript: str | None = None
    file_paths: list[str] = field(default_factory=list)


# --- Планирование напоминаний -------------------------------------------------

# Ставить задачи в расписание умеет только бот (у него JobQueue), но карточки создаются
# и из API. Поэтому бот при старте регистрирует здесь свой планировщик, а слой захвата
# просто вызывает его после сохранения — и не знает, что внутри Telegram.
Scheduler = Callable[[db.Reminder], None]

_scheduler: Scheduler | None = None


def set_scheduler(scheduler: Scheduler | None) -> None:
    global _scheduler
    _scheduler = scheduler


def _schedule(reminder: db.Reminder) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler(reminder)
    except Exception:
        # Карточка уже сохранена — из-за упавшего планировщика терять её нельзя.
        logger.exception("Не удалось поставить напоминание для карточки %s", reminder.id)


# --- Сохранение ---------------------------------------------------------------


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def save_extracted(
    extracted: ExtractedReminder,
    *,
    source: str,
    raw_text: str | None,
    chat_id: int | None,
    file_paths: list[str] | None = None,
) -> db.Reminder:
    """Кладёт разобранную моделью структуру в базу (синхронно)."""
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
        # Ссылку берём из исходного текста регуляркой — дословно, без участия модели.
        url=links.first_url(raw_text),
        source=source,
        raw_text=raw_text,
        file_paths=list(file_paths or []),
        chat_id=chat_id,
        recurrence=recurrence,
        remind_time=remind_time,
        importance=importance,
    )


async def _save_and_schedule(
    extracted: ExtractedReminder,
    *,
    source: str,
    raw_text: str | None,
    chat_id: int | None,
    file_paths: list[str] | None = None,
) -> db.Reminder:
    reminder = await asyncio.to_thread(
        save_extracted,
        extracted,
        source=source,
        raw_text=raw_text,
        chat_id=chat_id,
        file_paths=file_paths,
    )
    _schedule(reminder)
    return reminder


def store_file(data: bytes, media_type: str) -> str:
    """Сохраняет файл в FILES_DIR под именем от его содержимого.

    Имя = sha256 содержимого: один и тот же билет, присланный дважды, не занимает
    место второй раз (у файлов из Telegram эту роль играет file_unique_id).
    """
    suffix = _SUFFIX_BY_TYPE.get(media_type, "")
    path = FILES_DIR / f"{hashlib.sha256(data).hexdigest()[:32]}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def _materialize(files: list[IngestFile]) -> list[str]:
    """Досохраняет те файлы, у которых ещё нет пути на диске."""
    paths = []
    for f in files:
        paths.append(f.path or store_file(f.data, f.media_type))
    return paths


# --- Точки входа --------------------------------------------------------------


async def ingest_text(text: str, *, chat_id: int | None = None) -> IngestResult:
    """Текстовая заметка. Ссылка на PDF читается моделью по ссылке."""
    if not (text or "").strip():
        raise Rejected("Пустое сообщение — нечего сохранять.")

    urls = links.find_urls(text)
    pdf_urls = [u for u in urls if links.is_pdf_url(u)]

    # Ссылка на PDF -> Claude читает файл по ссылке.
    if pdf_urls:
        url = pdf_urls[0]
        try:
            extracted = await asyncio.to_thread(llm.structure_pdf_url, url)
        except Exception as e:
            raise Failed(
                "Не смог открыть PDF по ссылке — возможно, он за логином или "
                "недоступен. Попробуй прикрепить файл через 📎.",
                where="pdf-url",
            ) from e
        reminder = await _save_and_schedule(
            extracted, source="pdf", raw_text=url, chat_id=chat_id
        )
        return IngestResult(reminder)

    # Сообщение — это просто ссылка, но не на PDF: не сохраняем мусор.
    if len(urls) == 1 and text.strip() == urls[0]:
        raise Rejected(
            "Это ссылка не на PDF. Пришли PDF файлом через 📎 или дай прямую ссылку "
            "на .pdf. Обычные веб-страницы я пока читать не умею."
        )

    # Обычный текст (в т.ч. заметка со ссылкой внутри).
    try:
        extracted = await asyncio.to_thread(llm.structure_text, text)
    except Exception as e:
        raise Failed(
            "Не смог разобрать сообщение в напоминание. Попробуй ещё раз.",
            where="text",
        ) from e
    reminder = await _save_and_schedule(
        extracted, source="text", raw_text=text, chat_id=chat_id
    )
    return IngestResult(reminder)


async def ingest_files(
    files: list[IngestFile],
    *,
    source: str,
    caption: str | None = None,
    chat_id: int | None = None,
    where: str = "files",
    user_message: str | None = None,
) -> IngestResult:
    """Фото и PDF. Несколько файлов = ОДНА карточка: два билета на один концерт —
    это одно мероприятие. Модель видит их вместе и решает сама."""
    if not files:
        raise Rejected("Файлов нет — нечего разбирать.")
    unsupported = {f.media_type for f in files} - set(SUPPORTED_TYPES)
    if unsupported:
        raise Rejected(
            f"Не умею читать {', '.join(sorted(unsupported))}. "
            "Пришли фото (JPEG/PNG) или PDF."
        )

    paths = await asyncio.to_thread(_materialize, files)
    try:
        extracted = await asyncio.to_thread(
            llm.structure_files, [(f.data, f.media_type) for f in files]
        )
    except Exception as e:
        raise Failed(
            user_message
            or (
                f"📎 Получил файлов: {len(files)}, но не смог разобрать их в "
                "напоминание. Попробуй прислать по одному."
            ),
            where=where,
        ) from e
    reminder = await _save_and_schedule(
        extracted, source=source, raw_text=caption, chat_id=chat_id, file_paths=paths
    )
    return IngestResult(reminder, file_paths=paths)


async def transcribe_audio(
    audio: bytes, *, on_local_fallback: Callable[[], Awaitable[None]] | None = None
) -> str:
    """Аудио -> текст. Сначала облачный Whisper, при сбое — локальная модель.

    on_local_fallback вызывается перед медленным локальным проходом: пользователя
    надо предупредить о задержке, а не заставлять смотреть в пустой чат.
    """
    try:
        return await asyncio.to_thread(transcribe.transcribe_remote, audio)
    except Exception as e:
        logger.warning("OpenAI Whisper недоступен, фолбэк на локальную модель: %s", e)
        if on_local_fallback is not None:
            await on_local_fallback()
        try:
            return await asyncio.to_thread(transcribe.transcribe_local, audio)
        except Exception as e2:
            raise Failed(
                "🎙 Не смог распознать голосовое ни в облаке, ни локально. "
                "Попробуй ещё раз или пришли текстом.",
                where="voice-transcribe-local",
            ) from e2


async def ingest_audio(
    audio: bytes,
    *,
    chat_id: int | None = None,
    on_local_fallback: Callable[[], Awaitable[None]] | None = None,
) -> IngestResult:
    """Голосовое: распознаём, затем разбираем расшифровку как обычный текст."""
    text = await transcribe_audio(audio, on_local_fallback=on_local_fallback)
    try:
        extracted = await asyncio.to_thread(llm.structure_text, text)
    except Exception as e:
        raise Failed(
            f"🎙 Расшифровал: «{text}»\n\n…но не смог разобрать в напоминание. "
            "Попробуй ещё раз.",
            where="voice-structure",
        ) from e
    reminder = await _save_and_schedule(
        extracted, source="voice", raw_text=text, chat_id=chat_id
    )
    return IngestResult(reminder, transcript=text)


def file_for(reminder: db.Reminder, index: int) -> Path | None:
    """n-й существующий на диске файл карточки (у мероприятия бывает несколько билетов)."""
    paths = reminder.file_paths if reminder else []
    if not paths or not 0 <= index < len(paths):
        return None
    path = Path(paths[index])
    return path if path.exists() else None
