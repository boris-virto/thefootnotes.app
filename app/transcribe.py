"""Транскрипция голосовых через OpenAI Whisper.

Telegram присылает войсы в формате .oga (ogg/opus), который Whisper API принимает напрямую,
поэтому конвертация не нужна.
"""
from __future__ import annotations

from openai import OpenAI

from .config import OPENAI_API_KEY

# max_retries побольше дефолтных двух: Whisper иногда отдаёт транзиентный 500,
# который проходит с повтора (SDK сам ждёт с экспоненциальной паузой).
client = OpenAI(api_key=OPENAI_API_KEY, max_retries=4)


def transcribe(audio_bytes: bytes, filename: str = "voice.oga") -> str:
    result = client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, audio_bytes),
        language="ru",
    )
    return result.text.strip()
