"""Транскрипция голосовых.

Основной путь — OpenAI Whisper API (быстро и точно). Если он недоступен (например,
транзиентный 500), падаем на ЛОКАЛЬНЫЙ Whisper через faster-whisper: медленнее, но
работает без интернета к OpenAI. Модель грузится лениво — при первом фактическом
использовании фолбэка (и один раз качается с HuggingFace).

Telegram присылает войсы в .oga (ogg/opus); и API, и локальный декодер (PyAV) это едят.
"""
from __future__ import annotations

import io
import logging

from openai import OpenAI

from .config import OPENAI_API_KEY, WHISPER_LOCAL_MODEL

logger = logging.getLogger(__name__)

# max_retries побольше дефолтных двух: Whisper иногда отдаёт транзиентный 500,
# который проходит с повтора (SDK сам ждёт с экспоненциальной паузой).
client = OpenAI(api_key=OPENAI_API_KEY, max_retries=4)

_local_model = None  # ленивый синглтон faster-whisper


def transcribe_remote(audio_bytes: bytes, filename: str = "voice.oga") -> str:
    """Распознавание через OpenAI Whisper API. Кидает исключение при сбое."""
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes),
        language="ru",
    )
    return result.text.strip()


def _get_local_model():
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel  # тяжёлый импорт — только при надобности

        logger.info("Загружаю локальную модель Whisper '%s' (CPU, int8)…", WHISPER_LOCAL_MODEL)
        _local_model = WhisperModel(WHISPER_LOCAL_MODEL, device="cpu", compute_type="int8")
    return _local_model


def transcribe_local(audio_bytes: bytes) -> str:
    """Распознавание локальной моделью (faster-whisper). Медленнее, но офлайн."""
    model = _get_local_model()
    segments, _info = model.transcribe(io.BytesIO(audio_bytes), language="ru")
    return "".join(segment.text for segment in segments).strip()


# Обратная совместимость: обычный путь = облачный.
transcribe = transcribe_remote
