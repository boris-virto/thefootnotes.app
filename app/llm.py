"""Разбор смысла через Claude: сырой текст / картинка / PDF -> структура напоминания."""
from __future__ import annotations

import base64
from datetime import date

import anthropic
from pydantic import BaseModel, Field

from .config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ExtractedReminder(BaseModel):
    """То, что модель должна вытащить из сообщения."""

    title: str = Field(description="Короткая суть напоминания, 3-8 слов")
    category: str = Field(
        default="note",
        description="Одна из: task (задача/дело), event (событие с датой), "
        "ticket (билет на концерт/выставку/поездку), note (просто заметка)",
    )
    event_date: str | None = Field(
        default=None, description="Дата события в формате YYYY-MM-DD, если она есть; иначе null"
    )
    event_time: str | None = Field(
        default=None, description="Время в формате HH:MM, если указано; иначе null"
    )
    location: str | None = Field(default=None, description="Место/адрес, если есть; иначе null")
    notes: str | None = Field(
        default=None,
        description="Полезные детали: тип/категория билета, цена, место в зале. "
        "НЕ указывай имя, email или номер заказа покупателя/владельца. Иначе null.",
    )
    recurrence: str | None = Field(
        default=None,
        description="Если просят напоминать регулярно, укажи: 'daily' (каждый день), "
        "'weekly:<день>' где день = mon/tue/wed/thu/fri/sat/sun "
        "(например 'weekly:mon' для каждого понедельника), либо 'monthly:<число>' "
        "(например 'monthly:1' — 1-го числа месяца). Если регулярность не просят — null.",
    )
    remind_time: str | None = Field(
        default=None, description="Во сколько напоминать, в формате HH:MM. Если не указано — null."
    )


SYSTEM = (
    "Ты помощник, который превращает заметки, голосовые и билеты в структурированные "
    "напоминания. Извлекай суть кратко и по-русски. Относительные даты ('завтра', "
    "'в пятницу', 'через неделю') переводи в абсолютные, отсчитывая от сегодняшней даты, "
    "которую тебе дадут. Если даты нет — оставляй null, не выдумывай. "
    "Исполнителя/артиста/название события указывай в title. "
    "НИКОГДА не включай в напоминание персональные данные покупателя или владельца билета "
    "(ФИО, email, номер заказа/брони) — это не артист и не полезная деталь."
)


def _parse(content_blocks: list[dict]) -> ExtractedReminder:
    today = date.today().isoformat()
    content_blocks = [
        {"type": "text", "text": f"Сегодня {today}. Разбери это в напоминание:"},
        *content_blocks,
    ]
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": content_blocks}],
        output_format=ExtractedReminder,
    )
    return response.parsed_output


def structure_text(text: str) -> ExtractedReminder:
    return _parse([{"type": "text", "text": text}])


def structure_image(image_bytes: bytes, media_type: str = "image/jpeg") -> ExtractedReminder:
    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    return _parse(
        [
            {
                "type": "text",
                "text": "Это скриншот/фото (например, билет). Извлеки событие, дату, время, место.",
            },
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
        ]
    )


def structure_pdf(pdf_bytes: bytes) -> ExtractedReminder:
    data = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return _parse(
        [
            {"type": "text", "text": "Это PDF (например, билет или бронь). Извлеки суть."},
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            },
        ]
    )


def structure_pdf_url(url: str) -> ExtractedReminder:
    """Читает PDF по публичной ссылке — Claude скачивает файл сам."""
    return _parse(
        [
            {"type": "text", "text": "Это PDF по ссылке (например, билет или бронь). Извлеки суть."},
            {"type": "document", "source": {"type": "url", "url": url}},
        ]
    )
