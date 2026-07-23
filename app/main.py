"""Единая точка входа: FastAPI-дашборд + телеграм-бот в одном процессе.

Бот работает в режиме polling и запускается вместе с веб-сервером через lifespan FastAPI,
поэтому в облаке достаточно развернуть один сервис.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db
from .bot import (
    CATEGORY_EMOJI,
    build_application,
    schedule_digest,
    schedule_event_reminder,
    schedule_reminder,
)
from .config import DIGEST_TIME, TELEGRAM_BOT_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    tg_app = None
    if TELEGRAM_BOT_TOKEN:
        tg_app = build_application()
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()

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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    reminders = db.list_reminders()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"reminders": reminders, "emoji": CATEGORY_EMOJI},
    )


@app.post("/done/{reminder_id}")
async def done(reminder_id: int):
    db.mark_done(reminder_id)
    return RedirectResponse("/", status_code=303)


@app.get("/file/{reminder_id}")
async def download_file(reminder_id: int):
    reminder = db.get_reminder(reminder_id)
    if not reminder or not reminder.file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = Path(reminder.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл больше недоступен")
    return FileResponse(path, filename=f"{reminder.title}{path.suffix}")
