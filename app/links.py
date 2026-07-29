"""Работа со ссылками в тексте заметки.

Ссылку из сообщения достаём регуляркой, а не через LLM: это дословные данные —
модель может их сократить, «поправить» или потерять, а битая ссылка на бронь
обесценивает всю карточку.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://\S+")

# Хвостовая пунктуация, которая обычно принадлежит предложению, а не ссылке.
_TRAILING = ").,;!?»\"'"


def find_urls(text: str | None) -> list[str]:
    """Все ссылки из текста, с отрезанной хвостовой пунктуацией."""
    if not text:
        return []
    return [u.rstrip(_TRAILING) for u in _URL_RE.findall(text)]


def first_url(text: str | None) -> str | None:
    """Главная (первая) ссылка из текста или None."""
    urls = find_urls(text)
    return urls[0] if urls else None


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")
