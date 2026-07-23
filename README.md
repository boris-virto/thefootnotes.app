# thefootnotes

Телеграм-бот, который помогает не забывать всякие штуки. Кидаешь ему текст, голосовое,
скриншот билета или PDF — он разбирает смысл через Claude, сохраняет структурированное
напоминание и показывает всё на веб-дашборде.

## Что внутри

- **Бот** — python-telegram-bot (текст / войс / фото / PDF)
- **Транскрипция войсов** — OpenAI Whisper
- **Разбор смысла и чтение билетов** — Claude (Anthropic), с поддержкой vision и PDF
- **Хранилище** — SQLite (файл `data/thefootnotes.db`)
- **Дашборд** — FastAPI + одна HTML-страница
- Бот и веб работают **в одном процессе** (бот в режиме polling запускается вместе с сервером)

## Что нужно достать перед стартом

1. **Токен бота** — напиши [@BotFather](https://t.me/BotFather), `/newbot`, скопируй токен.
2. **Ключ Anthropic** — https://console.anthropic.com/ → API Keys.
3. **Ключ OpenAI** — https://platform.openai.com/ → API keys (нужен для войсов).
4. **Свой telegram user id** — напиши [@userinfobot](https://t.me/userinfobot), чтобы закрыть бота только для себя.

## Локальный запуск

```bash
cd thefootnotes
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # затем впиши ключи в .env

uvicorn app.main:app --reload --port 8000
```

- Дашборд: http://localhost:8000
- Бот заработает сразу после старта — напиши ему в Telegram.

## Деплой на VPS (Debian) + GitHub Actions

Полный пошаговый runbook — в [`deploy/DEPLOY.md`](deploy/DEPLOY.md). Кратко:

- бот и дашборд крутятся как systemd-сервис под непривилегированным пользователем;
- nginx отдаёт дашборд по HTTPS с паролем на домене `thefootnotes.app`;
- данные (SQLite + файлы) лежат на реальном диске и не теряются;
- каждый `git push` в `main` автоматически выкатывается на сервер (`.github/workflows/deploy.yml`);
- адрес базы вынесен в `DATABASE_URL` — переезд на PostgreSQL не требует правок кода.

## Структура

```
app/
  config.py      настройки из окружения
  db.py          модель Reminder + SQLite
  llm.py         разбор текста/картинок/PDF через Claude
  transcribe.py  войсы -> текст (Whisper)
  bot.py         хендлеры телеграм-бота
  main.py        FastAPI-дашборд + запуск бота
  templates/     HTML дашборда
```

## Что дальше (идеи)

- Напоминания обратно в Telegram по времени (APScheduler)
- Редактирование/удаление прямо из бота
- Постоянное хранилище (Postgres) при деплое
- Категории и фильтры на дашборде
