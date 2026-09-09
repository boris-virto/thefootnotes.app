# GitHub Actions → VPS → systemd

Целевой сервер: Debian 13, x86_64, Python 3.13, nginx; пользователь `thefootnotes`.
Бот и HTTP работают в одном процессе. Docker на VPS не нужен.

## Как проходит выпуск

`.github/workflows/deploy.yml` запускается на PR, push main и вручную на main.

1. Устанавливает `requirements-dev.lock` с проверкой хэшей; запускает тесты, pip check,
   compileall и настоящий Uvicorn без Telegram/LLM.
2. Собирает артефакт конкретного commit: код, production lock и Linux wheels.
   Проверяет отдельный production venv без dev-пакетов. Даже PR проверяет эту сборку.
3. Только для main, после CI, скачивает этот артефакт в job `Deploy production`.
4. Передаёт его через SSH с проверкой закреплённого host key. Сервер проверяет checksum,
   SHA, Python/architecture и устанавливает wheels без доступа к PyPI в отдельный venv.
5. Останавливает единственный процесс, создаёт backup SQLite и вложений, проверяет миграцию
   на копии БД, мигрирует рабочую БД, атомарно переключает `current`, запускает systemd.
6. Проверяет `/health/ready` локально и через публичный домен, включая SHA релиза.
   При сбое возвращает старый код. Job остаётся красным даже после успешного отката.

GitHub concurrency и серверный flock исключают одновременные переключения. Номер workflow
не позволяет старому запуску заменить более новый успешный релиз. Запущенный deploy не отменяем.

## Постоянные пути

```
/opt/thefootnotes/
  .env                    # существующие ключи, никогда не входят в CI artifact
  data/                   # существующая SQLite, files/, whisper-models/
  current -> releases/...
  previous -> releases/...
  releases/<sha>-<run>-<id>/  # код, RELEASE, .venv, wheels
  incoming/<run>-<attempt>/  # доставленный архив и checksum
  backups/<sha>-<id>/        # database.sqlite, files/, manifest.json
```

WorkingDirectory остаётся `/opt/thefootnotes`, поэтому старые относительные пути вложений
`data/files/...` продолжают работать. `--app-dir current` выбирает код релиза. DB/FILES_DIR
не переносим. Процедура проверяет существование SQLite внутри DATA_DIR и отказывается
незаметно создавать пустую БД при неверных настройках. Пользовательские внешние пути и
PostgreSQL требуют отдельной адаптации runbook.

## Разовая настройка существующего VPS

Сначала запустить CI на PR и проверить diff. Не сливать новую конфигурацию в main, пока
сервер не подготовлен: merge запускает deploy.

1. Убедиться, что `python3` — 3.13, архитектура x86_64, установлены python3-venv, sudo,
   nginx и достаточно места для старого/нового venv, wheelhouse и копии данных.
2. Передать проверенную папку `deploy/` отдельно, например `/tmp/footnotes-bootstrap/`.
   Не делать `git pull` в рабочем production-каталоге.
3. Под root выполнить:

   ```bash
   bash /tmp/footnotes-bootstrap/bootstrap.sh
   ```

   Скрипт сохраняет текущий код как `legacy-*`, ссылается на старый venv, сохраняет старый
   unit, устанавливает новый и перезапускает существующую версию. Данные не переносит.
   При неудачном старте возвращает старый unit. Повторное выполнение с current запрещено.
   При первом откате на legacy проверяется `/login`; следующие релизы проверяют readiness + SHA.
4. Проверить `systemctl status thefootnotes`, `/login`, существующие карточки и бота.
5. Deploy-пользователю разрешены только `systemctl start/stop/restart thefootnotes` через sudo.
   Deploy имеет доступ к своему коду и данным; root shell GitHub не получает.

## GitHub

Существующие secrets сохраняются: `SSH_HOST` (IP VPS, не адрес Cloudflare proxy),
`SSH_USER`, `SSH_KEY`. Добавляется **SSH_KNOWN_HOSTS** — проверенная строка ключа сервера
в формате known_hosts для SSH_HOST. Сверить fingerprint через доверенное подключение
или консоль VPS. Не использовать слепой ssh-keyscan непосредственно в workflow.

Environment: `production`, разрешённая deployment branch — main. PR jobs не используют
production secrets. Для main включить required check **Tests and startup**, запрет force
push и удаление ветки. Ручное одобрение каждого выпуска необязательно.

Actions закреплены по полным SHA; обновлять их отдельными PR. CI использует GitHub-hosted
Ubuntu 24.04, Python 3.13; wheels должны быть совместимы с Debian 13 x86_64.

## Откат и миграции

`app/db.py:init_db()` пока остаётся мигратором. В production `MIGRATE_ON_START=false`,
миграции выполняются явно в окне остановки. Они должны оставаться обратно совместимыми
с предыдущим релизом: добавление колонок/таблиц, затем отдельным будущим выпуском удаление
старых структур. Проверка миграции на копии не доказывает совместимость произвольного старого кода.

Если миграция падает **до запуска нового процесса**, deploy восстанавливает pre-deploy БД
и запускает старый код. Если новый процесс уже запускался, откат меняет **только код**:
автоматический возврат старой БД мог бы потерять новые записи. При несовместимой схеме
понадобится ручное восстановление; workflow покажет ошибку readiness старого релиза.

Для обычного ручного отката предпочтительно revert проблемного commit через PR: новый CI
соберёт и проверит нужную версию. Emergency-переключение previous допустимо только после
проверки совместимости схемы и под тем же `.deploy.lock`.

## Backup и место на диске

Каждый deploy сохраняет согласованную SQLite-копию через Backup API и вложения при
остановленном writer. `whisper-models` — восстанавливаемый cache, в backup не включён.
Секреты .env должны иметь отдельную защищённую копию; в архив приложения они не попадают.

Локальный backup не защищает от потери VPS. Нужен отдельный offsite storage и регулярное
расписание; destination/credentials не задаются этим workflow. Проверка восстановления:
развернуть database.sqlite и files/ в отдельной временной среде с BOT_ENABLED=false,
проверить integrity_check, миграции, чтение карточек и вложений.

Релизы и backups автоматически не удаляются: до выбора retention проверять свободное место
и удалять вручную только ненужные архивы, не current/previous. При нехватке места на подготовке
работающий сервис остаётся нетронутым; ошибка backup запускает старый процесс.

## Зависимости

Файлы requirements*.txt — входные требования; requirements*.lock — конкретные версии и хэши.
Первый lock сохраняет версии из проверенного окружения; Linux-зависимости разрешены отдельно.
Обновление выполнять отдельным PR (uv 0.9.2):

```bash
uv pip compile requirements-dev.txt --python-version 3.13 --python-platform x86_64-manylinux_2_39 --generate-hashes -o requirements-dev.lock
uv pip compile requirements.txt -c requirements-dev.lock --python-version 3.13 --python-platform x86_64-manylinux_2_39 --generate-hashes -o requirements.lock
```

CI и production требуют wheel для каждой зависимости. Отсутствующий Linux wheel блокирует
выпуск до остановки production. Тяжёлый faster-whisper пока остаётся частью production.

## nginx / TLS / эксплуатация

Существующий nginx не меняется: proxy_pass на 127.0.0.1:8000, HTTPS.
С Cloudflare использовать `nginx-thefootnotes-cloudflare.conf`, Origin Certificate,
SSL/TLS **Full (strict)** и edge redirect HTTPS; SSH_HOST — адрес самого VPS.
Без Cloudflare — `nginx-thefootnotes.conf` и сертификат certbot.
Вход в приложение — Telegram или код, nginx basic-auth не нужен.

- Логи: `journalctl -u thefootnotes -n 100 --no-pager`.
- Readiness: `curl --fail http://127.0.0.1:8000/health/ready`.
- Статус: `systemctl status thefootnotes --no-pager`.
- Не включать несколько Uvicorn workers и не запускать второй poller с production token.
- Staging: отдельная БД и отдельный Telegram bot token.
- В GitHub включить персональные уведомления о failed Actions runs.
