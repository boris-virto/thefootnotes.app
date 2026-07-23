"""Единая точка входа: FastAPI-дашборд + телеграм-бот в одном процессе.

Бот работает в режиме polling и запускается вместе с веб-сервером через lifespan FastAPI,
поэтому в облаке достаточно развернуть один сервис.

Доступ к дашборду — через вход по Telegram (Login Widget): вместо пароля в nginx
пользователь логинится своим телеграм-аккаунтом, а мы сверяем его id с ALLOWED_USER_IDS.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .auth import verify_telegram_login
from .bot import (
    CATEGORY_EMOJI,
    build_application,
    schedule_digest,
    schedule_event_reminder,
    schedule_reminder,
)
from .config import (
    DIGEST_TIME,
    SESSION_SECRET,
    TELEGRAM_BOT_TOKEN,
    user_allowed,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    tg_app = None
    app.state.bot_username = None
    if TELEGRAM_BOT_TOKEN:
        tg_app = build_application()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()

        # Имя бота нужно для кнопки входа Telegram Login Widget.
        me = await tg_app.bot.get_me()
        app.state.bot_username = me.username

        # Восстанавливаем задачи из базы после рестарта.
        recurring = 0
        for reminder in db.list_active_recurring():
            schedule_reminder(tg_app.job_queue, reminder)
            recurring += 1
        events = 0
        for reminder in db.list_upcoming_events():
            if schedule_event_reminder(tg_app.job_queue, reminder):
                events += 1

        # Ежедневный утренний дайджест со списком напоминаний.
        schedule_digest(tg_app.job_queue)

        logger.info(
            "Телеграм-бот запущен (polling). Восстановлено: регулярных %d, событий %d. "
            "Утренний дайджест в %s.",
            recurring,
            events,
            DIGEST_TIME,
        )
    else:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен, работает только дашборд.")

    try:
        yield
    finally:
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
            logger.info("Телеграм-бот остановлен.")


app = FastAPI(lifespan=lifespan)
# Сессия в подписанной куке (HttpOnly, Secure).
# SameSite=None обязателен: Telegram Login Widget возвращает пользователя на /auth/telegram
# редиректом из своего домена (oauth.telegram.org) — при Lax браузер не отдаёт куку на
# этом cross-site переходе, и вход не «прилипает». None требует Secure (у нас включён).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="tf_session",
    https_only=True,
    same_site="none",
    max_age=30 * 24 * 60 * 60,  # держим вход месяц
)


def _logged_in(request: Request) -> bool:
    uid = request.session.get("uid")
    return uid is not None and user_allowed(int(uid))


def _require_login(request: Request) -> RedirectResponse | None:
    """None — если пользователь вошёл; иначе редирект на страницу входа."""
    if _logged_in(request):
        return None
    return RedirectResponse("/login", status_code=303)


# --- Вход через Telegram -----------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if _logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"bot_username": request.app.state.bot_username},
    )


@app.get("/auth/telegram")
async def auth_telegram(request: Request):
    """Callback Telegram Login Widget: проверяем подпись и открываем сессию."""
    data = dict(request.query_params)
    if not verify_telegram_login(data, TELEGRAM_BOT_TOKEN):
        raise HTTPException(status_code=403, detail="Подпись Telegram не прошла проверку")

    uid = int(data["id"])
    if not user_allowed(uid):
        raise HTTPException(
            status_code=403, detail=f"Доступ закрыт: user id {uid} не в списке разрешённых"
        )

    request.session["uid"] = uid
    request.session["name"] = data.get("first_name") or data.get("username") or str(uid)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Дашборд (требует входа) -------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if redirect := _require_login(request):
        return redirect
    reminders = db.list_reminders()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "reminders": reminders,
            "emoji": CATEGORY_EMOJI,
            "user_name": request.session.get("name"),
        },
    )


@app.post("/done/{reminder_id}")
async def done(request: Request, reminder_id: int):
    if redirect := _require_login(request):
        return redirect
    db.mark_done(reminder_id)
    return RedirectResponse("/", status_code=303)


@app.get("/file/{reminder_id}")
async def download_file(request: Request, reminder_id: int):
    if redirect := _require_login(request):
        return redirect
    reminder = db.get_reminder(reminder_id)
    if not reminder or not reminder.file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = Path(reminder.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл больше недоступен")
    return FileResponse(path, filename=f"{reminder.title}{path.suffix}")
