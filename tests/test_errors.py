"""Тесты единой обработки ошибок report_error."""
from types import SimpleNamespace

import pytest

from app import errors


class Recorder:
    """Асинхронный «шпион»: запоминает вызовы."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _make(user_id):
    message = SimpleNamespace(reply_text=Recorder())
    user = SimpleNamespace(id=user_id, username="tester", first_name="Test")
    update = SimpleNamespace(effective_user=user, effective_message=message)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=Recorder()))
    return update, context, message, context.bot.send_message


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(errors.config, "ADMIN_TELEGRAM_ID", 999)
    return 999


async def test_regular_user_gets_message_and_admin_notified(admin):
    update, context, message, admin_send = _make(user_id=111)
    await errors.report_error(update, context, ValueError("boom"), where="voice")

    # пользователю — понятное сообщение без трейсбека
    assert len(message.reply_text.calls) == 1
    user_text = message.reply_text.calls[0][0][0]
    assert "boom" not in user_text  # трейсбек обычному юзеру не показываем

    # админу — уведомление с трейсбеком
    assert len(admin_send.calls) == 1
    args, kwargs = admin_send.calls[0]
    assert args[0] == 999
    assert "boom" in args[1] and kwargs.get("parse_mode") == "HTML"


async def test_custom_user_message(admin):
    update, context, message, _ = _make(user_id=111)
    await errors.report_error(update, context, ValueError("x"), where="photo",
                              user_message="🖼 Не смог разобрать фото.")
    assert message.reply_text.calls[0][0][0] == "🖼 Не смог разобрать фото."


async def test_admin_sees_traceback_inline_and_no_extra_notify(admin):
    update, context, message, admin_send = _make(user_id=999)  # ошибка у самого админа
    await errors.report_error(update, context, ValueError("kaboom"), where="text")

    # админу трейсбек прямо в ответ
    user_text = message.reply_text.calls[0][0][0]
    assert "kaboom" in user_text and "<pre>" in user_text
    # отдельного уведомления не шлём — это тот же чат
    assert admin_send.calls == []


async def test_no_admin_configured_no_notify(monkeypatch):
    monkeypatch.setattr(errors.config, "ADMIN_TELEGRAM_ID", None)
    update, context, message, admin_send = _make(user_id=111)
    await errors.report_error(update, context, ValueError("x"), where="voice")
    assert len(message.reply_text.calls) == 1  # пользователю ответили
    assert admin_send.calls == []  # но админа нет — не шлём


async def test_no_message_still_notifies_admin(admin):
    # update без effective_message (например, служебный апдейт)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=Recorder()))
    update = SimpleNamespace(effective_user=None, effective_message=None)
    await errors.report_error(update, context, RuntimeError("srv"), where="job")
    assert len(context.bot.send_message.calls) == 1
    assert "srv" in context.bot.send_message.calls[0][0][1]
