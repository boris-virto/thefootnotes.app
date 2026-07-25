"""Единая обработка ошибок бота.

Идея: любая ошибка в обработчике не должна «пропадать молча». Пользователю уходит
понятное сообщение, владельцу (ADMIN_TELEGRAM_ID) — уведомление с трейсбеком. Если
ошибку словил сам админ, трейсбек показываем ему прямо в ответе.
"""
from __future__ import annotations

import html
import logging
import traceback

from . import config

logger = logging.getLogger(__name__)

GENERIC_USER_MESSAGE = "⚠️ Что-то пошло не так при обработке. Попробуй ещё раз чуть позже."

# Телеграм режет сообщения на 4096 символов — оставляем запас на обёртку <pre>.
_TB_LIMIT = 3500


def format_traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _is_admin(user) -> bool:
    return bool(user and config.ADMIN_TELEGRAM_ID and user.id == config.ADMIN_TELEGRAM_ID)


def _describe_user(user) -> str:
    if not user:
        return "—"
    handle = user.username or user.first_name or ""
    return f"{user.id} {handle}".strip()


async def _safe_send(func, *args, **kwargs) -> None:
    """Отправка, которая сама не свалится (иначе ошибка в обработчике ошибок)."""
    try:
        await func(*args, **kwargs)
    except Exception:
        logger.exception("Не удалось отправить сообщение об ошибке")


async def report_error(update, context, exc: BaseException, *, where: str,
                       user_message: str | None = None) -> None:
    """Единая точка: логируем, отвечаем пользователю, уведомляем админа.

    update/context могут быть неполными (например, из глобального error-handler'а),
    поэтому читаем атрибуты защитно.
    """
    logger.error("Ошибка в %s", where, exc_info=exc)
    tb = format_traceback(exc)[-_TB_LIMIT:]

    user = getattr(update, "effective_user", None)
    message = getattr(update, "effective_message", None)
    admin_affected = _is_admin(user)

    # 1) Ответ пользователю. Админу к сообщению прикладываем трейсбек.
    if message is not None:
        text = user_message or GENERIC_USER_MESSAGE
        if admin_affected:
            await _safe_send(
                message.reply_text,
                f"{text}\n\n<pre>{html.escape(tb)}</pre>",
                parse_mode="HTML",
            )
        else:
            await _safe_send(message.reply_text, text)

    # 2) Уведомление админу — если ошибка не у самого админа и есть куда слать.
    bot = getattr(context, "bot", None)
    if config.ADMIN_TELEGRAM_ID and not admin_affected and bot is not None:
        note = (
            f"🛠 Ошибка в <b>{html.escape(where)}</b>\n"
            f"Пользователь: {html.escape(_describe_user(user))}\n"
            f"<pre>{html.escape(tb)}</pre>"
        )
        await _safe_send(bot.send_message, config.ADMIN_TELEGRAM_ID, note, parse_mode="HTML")
