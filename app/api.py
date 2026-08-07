"""JSON API для мобильных клиентов: приложение на iPhone и шорткаты iOS.

Отдельный слой от HTML-дашборда по трём причинам:
  * клиенту нужен JSON и честный 401, а не редирект на страницу входа;
  * авторизация идёт по токену в заголовке, а не по сессионной куке (см. app/tokens.py);
  * время отдаём с явным UTC и вместе с курсором server_time, чтобы клиент мог
    спрашивать «что изменилось после X», не полагаясь на свои часы.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, ingest, tokens
from .config import user_allowed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api"])

# Колонки доски: ключ статуса -> подпись. Здесь же их берёт HTML-дашборд, чтобы
# приложение и веб не разъехались в названиях.
COLUMNS = [
    ("todo", "TODO"),
    ("doing", "В процессе"),
    ("done", "Сделано"),
]

# Ограничения на загрузку. Файл целиком уезжает в модель в base64, поэтому важен и
# размер (память процесса), и количество (стоимость запроса).
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_FILES = 10

# Голосовое из приложения приходит как обычный файл — по типу решаем, что это аудио,
# и отправляем в распознавание, а не в vision.
AUDIO_PREFIX = "audio/"
AUDIO_TYPES = (
    "audio/m4a", "audio/x-m4a", "audio/mp4", "audio/mpeg", "audio/mpga",
    "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm", "video/mp4",
)


# --- Авторизация --------------------------------------------------------------


def current_user(
    request: Request, authorization: str | None = Header(default=None)
) -> int:
    """user id владельца запроса: сначала Bearer-токен, потом сессионная кука.

    Кука оставлена, чтобы дашборд мог дёргать те же эндпоинты из браузера, где
    пользователь уже вошёл, — без второго механизма авторизации в JS.
    """
    if authorization and authorization.lower().startswith("bearer "):
        user_id = tokens.verify_token(authorization[7:].strip())
        if user_id is not None:
            return user_id
        raise HTTPException(
            status_code=401,
            detail="Токен недействителен — привяжи устройство заново через /pair",
            headers={"WWW-Authenticate": "Bearer"},
        )

    uid = request.session.get("uid")
    if uid is not None and user_allowed(int(uid)):
        return int(uid)
    raise HTTPException(
        status_code=401,
        detail="Требуется вход: токен устройства или сессия дашборда",
        headers={"WWW-Authenticate": "Bearer"},
    )


# --- Схемы --------------------------------------------------------------------


def _utc(moment: datetime | None) -> datetime | None:
    """В базе время лежит без смещения (всегда UTC) — на выходе помечаем это явно,
    иначе клиент разберёт метку как своё локальное время и промахнётся на часовой пояс."""
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class CardOut(BaseModel):
    id: int
    title: str
    category: str
    status: str
    importance: int = Field(ge=1, le=3)
    event_date: date | None = None
    event_time: str | None = None
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    source: str
    recurrence: str | None = None
    remind_time: str | None = None
    remind_active: bool
    # Ссылки на вложения — по ним же клиент понимает, сколько файлов у карточки.
    files: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, reminder: db.Reminder) -> "CardOut":
        return cls(
            id=reminder.id,
            title=reminder.title,
            category=reminder.category,
            status=reminder.status,
            importance=reminder.importance,
            event_date=reminder.event_date,
            event_time=reminder.event_time,
            location=reminder.location,
            notes=reminder.notes,
            url=reminder.url,
            source=reminder.source,
            recurrence=reminder.recurrence,
            remind_time=reminder.remind_time,
            remind_active=reminder.remind_active,
            files=[
                f"{router.prefix}/cards/{reminder.id}/files/{i}"
                for i in range(len(reminder.file_paths))
            ],
            created_at=_utc(reminder.created_at),
            updated_at=_utc(reminder.updated_at or reminder.created_at),
        )


class ColumnOut(BaseModel):
    key: str
    label: str
    cards: list[CardOut]


class BoardOut(BaseModel):
    columns: list[ColumnOut]
    # Курсор для следующего запроса «что изменилось»: берём время сервера, а не клиента.
    server_time: datetime


class CardsOut(BaseModel):
    cards: list[CardOut]
    server_time: datetime


class PairIn(BaseModel):
    code: str
    device_name: str = "устройство"


class PairOut(BaseModel):
    token: str
    user_id: int


class CardPatch(BaseModel):
    status: str | None = None
    importance: int | None = None
    remind_active: bool | None = None


class CaptureOut(BaseModel):
    card: CardOut
    # Заполняется только для голосовых: клиент показывает, что именно он услышал.
    transcript: str | None = None


class MeOut(BaseModel):
    user_id: int
    server_time: datetime


class DeviceOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


# --- Привязка устройства ------------------------------------------------------


@router.post("/pair", response_model=PairOut)
async def pair(body: PairIn) -> PairOut:
    """Обмен кода из бота на долгий токен. Единственный эндпоинт без авторизации."""
    issued = tokens.redeem_code(body.code, body.device_name)
    if issued is None:
        raise HTTPException(
            status_code=400, detail="Код неверный или истёк — запроси новый командой /pair"
        )
    token, user_id = issued
    logger.info("Привязано устройство «%s» для пользователя %s", body.device_name, user_id)
    return PairOut(token=token, user_id=user_id)


@router.get("/me", response_model=MeOut)
async def me(user_id: int = Depends(current_user)) -> MeOut:
    """Проверка токена «на живость» — клиент зовёт её при запуске."""
    return MeOut(user_id=user_id, server_time=db.utcnow())


@router.get("/devices", response_model=list[DeviceOut])
async def devices(user_id: int = Depends(current_user)) -> list[DeviceOut]:
    return [
        DeviceOut(
            id=t.id, name=t.name, created_at=_utc(t.created_at), last_used_at=_utc(t.last_used_at)
        )
        for t in db.list_device_tokens(user_id)
    ]


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: int, user_id: int = Depends(current_user)) -> dict:
    if not db.revoke_device_token(user_id, device_id):
        raise HTTPException(status_code=404, detail="Устройство не найдено")
    return {"ok": True}


# --- Чтение доски -------------------------------------------------------------


@router.get("/board", response_model=BoardOut)
async def board(user_id: int = Depends(current_user)) -> BoardOut:
    grouped: dict[str, list[db.Reminder]] = {key: [] for key, _ in COLUMNS}
    for reminder in db.list_board():  # уже отсортировано: по сроку, затем по важности
        grouped.setdefault(reminder.status, []).append(reminder)
    return BoardOut(
        columns=[
            ColumnOut(key=key, label=label, cards=[CardOut.of(r) for r in grouped.get(key, [])])
            for key, label in COLUMNS
        ],
        server_time=db.utcnow(),
    )


@router.get("/cards", response_model=CardsOut)
async def cards(
    user_id: int = Depends(current_user),
    since: datetime | None = None,
    status: str | None = None,
) -> CardsOut:
    """Карточки списком. since=<ISO-время> отдаёт только изменённые после него —
    включая архивные, чтобы клиент узнал об убранных с доски."""
    if status is not None and status not in db.VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Недопустимый статус")

    if since is not None:
        items = db.list_changed_since(since)
    elif status == "archived":
        items = db.list_archived()
    else:
        items = db.list_board()
    if status is not None:
        items = [r for r in items if r.status == status]
    return CardsOut(cards=[CardOut.of(r) for r in items], server_time=db.utcnow())


@router.get("/cards/{card_id}", response_model=CardOut)
async def card(card_id: int, user_id: int = Depends(current_user)) -> CardOut:
    reminder = db.get_reminder(card_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    return CardOut.of(reminder)


@router.get("/cards/{card_id}/files/{index}")
async def card_file(card_id: int, index: int, user_id: int = Depends(current_user)):
    reminder = db.get_reminder(card_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    path = ingest.file_for(reminder, index)
    if path is None:
        raise HTTPException(status_code=404, detail="Файл не найден или больше недоступен")
    suffix = f" ({index + 1})" if len(reminder.file_paths) > 1 else ""
    return FileResponse(path, filename=f"{reminder.title}{suffix}{path.suffix}")


# --- Изменение ----------------------------------------------------------------


@router.patch("/cards/{card_id}", response_model=CardOut)
async def patch_card(
    card_id: int, body: CardPatch, user_id: int = Depends(current_user)
) -> CardOut:
    if db.get_reminder(card_id) is None:
        raise HTTPException(status_code=404, detail="Карточка не найдена")
    if body.status is not None:
        if body.status not in db.VALID_STATUSES:
            raise HTTPException(status_code=400, detail="Недопустимый статус")
        db.set_status(card_id, body.status)
    if body.importance is not None:
        db.set_importance(card_id, body.importance)
    if body.remind_active is not None:
        db.set_remind_active(card_id, body.remind_active)
    return CardOut.of(db.get_reminder(card_id))


# --- Захват -------------------------------------------------------------------


def _check_upload(upload: UploadFile, data: bytes) -> None:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл «{upload.filename}» больше "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} МБ — такой не осилю",
        )


@router.post("/capture", response_model=CaptureOut)
async def capture(
    user_id: int = Depends(current_user),
    text: str | None = Form(default=None),
    files: list[UploadFile] = File(default=[]),
) -> CaptureOut:
    """Единая точка приёма: текст, картинки/PDF или голосовое.

    chat_id ставим равным user id: в личном чате Telegram они совпадают, поэтому
    карточка, созданная с телефона, тоже получит пинг «за день до события» в чат.
    """
    files = [f for f in files if f.filename or f.content_type]
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"Больше {MAX_FILES} файлов за раз не беру")

    try:
        if files:
            payload = []
            for upload in files:
                data = await upload.read()
                _check_upload(upload, data)
                payload.append((upload, data))

            media_types = {(u.content_type or "").lower() for u, _ in payload}
            is_audio = any(
                mt.startswith(AUDIO_PREFIX) or mt in AUDIO_TYPES for mt in media_types
            )
            if is_audio:
                if len(payload) > 1:
                    raise HTTPException(
                        status_code=400, detail="Голосовое присылай по одному файлу"
                    )
                result = await ingest.ingest_audio(payload[0][1], chat_id=user_id)
            else:
                result = await ingest.ingest_files(
                    [
                        ingest.IngestFile(data, (upload.content_type or "").lower())
                        for upload, data in payload
                    ],
                    source="pdf" if media_types == {ingest.PDF_TYPE} else "photo",
                    caption=text,
                    chat_id=user_id,
                    where="api-capture",
                )
        elif text:
            result = await ingest.ingest_text(text, chat_id=user_id)
        else:
            raise HTTPException(status_code=400, detail="Пусто: пришли текст или файл")
    except ingest.Rejected as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ingest.Failed as e:
        logger.error("Захват из API не удался (%s)", e.where, exc_info=e.original)
        raise HTTPException(status_code=502, detail=e.user_message) from e

    return CaptureOut(card=CardOut.of(result.reminder), transcript=result.transcript)
