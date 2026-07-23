# Деплой на VPS (Debian 13, Vultr) + авто-деплой через GitHub Actions

Итог:
- бот и дашборд — systemd-сервис под непривилегированным пользователем `remember` (с харденингом);
- nginx отдаёт дашборд по HTTPS с паролем на `https://remember.amog.us.kg`;
- данные (SQLite + файлы) лежат на диске в `/opt/remember_stuff/data` и не теряются;
- каждый `git push` в `main` автоматически выкатывается на сервер.

Значения-примеры: поддомен `remember.amog.us.kg`, путь `/opt/remember_stuff`, пользователь `remember`,
репозиторий `github.com/USER/remember_stuff` (подставь свой).

---

## Шаг 0. DNS

В панели, где управляешь доменом `amog.us.kg`, добавь запись:

```
Тип: A    Имя: remember    Значение: <публичный IP сервера>
```

IP сервера можно узнать на самом сервере: `curl -4 ifconfig.me`. Проверить, что применилось:
`ping remember.amog.us.kg` (должен резолвиться в IP сервера; может занять несколько минут).

---

## Часть 1. Разовая настройка сервера (под root)

### 1. Пакеты

```bash
apt update
apt install -y git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx apache2-utils
```

### 2. Пользователь `remember`

Отдельный непривилегированный пользователь: под ним и работает сервис, и заходит деплой.

```bash
useradd -m -d /home/remember -s /bin/bash remember
```

### 3. Клонируем репозиторий в /opt/remember_stuff

```bash
mkdir -p /opt/remember_stuff
chown remember:remember /opt/remember_stuff
# Публичный репозиторий — по https, без ключей:
sudo -u remember git clone https://github.com/USER/remember_stuff.git /opt/remember_stuff
# (Если репозиторий приватный — см. раздел «Приватный репозиторий» внизу.)

# Папка данных должна существовать до первого старта (из-за строгого харденинга):
sudo -u remember mkdir -p /opt/remember_stuff/data/files
```

### 4. Виртуальное окружение и зависимости

```bash
sudo -u remember python3 -m venv /opt/remember_stuff/.venv
sudo -u remember /opt/remember_stuff/.venv/bin/pip install --upgrade pip
sudo -u remember /opt/remember_stuff/.venv/bin/pip install -r /opt/remember_stuff/requirements.txt
```

### 5. Файл .env с ключами

```bash
sudo -u remember nano /opt/remember_stuff/.env
```

Вставь (значения возьми из локального `.env`):

```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
ALLOWED_USER_IDS=130359870
ANTHROPIC_MODEL=claude-haiku-4-5
TIMEZONE=Europe/Belgrade
DATA_DIR=data
```

Закрыть доступ:

```bash
chmod 600 /opt/remember_stuff/.env
```

### 6. Право рестартовать сервис без пароля (узкий sudo)

Чтобы деплой мог перезапускать сервис, но не более того:

```bash
echo 'remember ALL=(root) NOPASSWD: /usr/bin/systemctl restart remember-stuff' \
  > /etc/sudoers.d/remember
chmod 440 /etc/sudoers.d/remember
```

### 7. systemd-сервис

```bash
cp /opt/remember_stuff/deploy/remember-stuff.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now remember-stuff
systemctl status remember-stuff --no-pager
journalctl -u remember-stuff -n 30 --no-pager   # должно быть «Телеграм-бот запущен»
```

С этого момента **бот уже работает** — можно писать ему в Telegram.

### 8. nginx

```bash
cp /opt/remember_stuff/deploy/nginx-remember.conf /etc/nginx/sites-available/remember
ln -sf /etc/nginx/sites-available/remember /etc/nginx/sites-enabled/remember
```

> Дефолтный сайт nginx (`/etc/nginx/sites-enabled/default`) НЕ удаляем — домен `amog.us.kg`
> может использоваться под другое. Наш конфиг ловит только поддомен `remember.amog.us.kg`.

### 9. Пароль на дашборд

```bash
htpasswd -c /etc/nginx/.htpasswd-remember boris   # спросит и подтвердит пароль
```

### 10. HTTPS-сертификат

```bash
nginx -t && systemctl reload nginx
certbot --nginx -d remember.amog.us.kg    # спросит email и согласие; сам настроит 443 и редирект
```

### 11. Файрвол, если включён ufw

```bash
ufw status                       # если "inactive" — пропусти
ufw allow 22 && ufw allow 80 && ufw allow 443
```

Проверка: открой **https://remember.amog.us.kg** → логин/пароль из шага 9 → дашборд.

---

## Часть 2. Авто-деплой через GitHub Actions

Workflow уже лежит в `.github/workflows/deploy.yml`. Он при каждом пуше в `main` заходит
на сервер по SSH и делает `git pull` + `pip install` + рестарт сервиса.

### 1. Ключ для деплоя (SSH)

**На своём компе** создай отдельную пару ключей для CI:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/remember_deploy -N "" -C "github-actions-remember"
```

**Публичную** часть добавь пользователю `remember` на сервере:

```bash
# скопируй содержимое ~/.ssh/remember_deploy.pub, затем на сервере:
sudo -u remember mkdir -p /home/remember/.ssh
sudo -u remember tee -a /home/remember/.ssh/authorized_keys   # вставь строку, Ctrl+D
chmod 700 /home/remember/.ssh && chmod 600 /home/remember/.ssh/authorized_keys
chown -R remember:remember /home/remember/.ssh
```

### 2. Секреты в GitHub

В репозитории → Settings → Secrets and variables → Actions → New repository secret:

| Имя | Значение |
|-----|----------|
| `SSH_HOST` | `remember.amog.us.kg` |
| `SSH_USER` | `remember` |
| `SSH_KEY`  | содержимое **приватного** файла `~/.ssh/remember_deploy` (целиком) |

### 3. Готово

Теперь любой `git push` в `main` автоматически выкатывается. Прогресс — во вкладке **Actions**
репозитория. Запустить вручную можно там же (Run workflow).

---

## Обновление кода

Просто:

```bash
git add -A && git commit -m "..." && git push
```

GitHub Actions сам зальёт на сервер и перезапустит сервис. `data/` и `.env` не трогаются.

---

## Переезд на PostgreSQL (когда понадобится)

Код уже готов: адрес базы берётся из `DATABASE_URL` (по умолчанию SQLite). Чтобы переехать:

1. Поставить Postgres на сервере, создать базу и пользователя.
2. Добавить драйвер: `pip install "psycopg[binary]"` (и в `requirements.txt`).
3. В `.env` прописать `DATABASE_URL=postgresql+psycopg://remember:пароль@localhost:5432/remember`.
4. Для миграций схемы подключить Alembic (сейчас авто-миграция колонок работает только для SQLite).

Старые данные из SQLite при желании перельём отдельным скриптом.

---

## Приватный репозиторий (если не хочешь делать публичным)

Для `git pull` на сервере нужен read-only доступ к репозиторию:

1. На сервере: `sudo -u remember ssh-keygen -t ed25519 -f /home/remember/.ssh/github -N ""`
2. Содержимое `/home/remember/.ssh/github.pub` добавь в GitHub → репозиторий → Settings →
   Deploy keys (read-only).
3. Настрой git на использование этого ключа и клонируй по SSH:
   `git@github.com:USER/remember_stuff.git`.

---

## Полезное

- Логи в реальном времени: `journalctl -u remember-stuff -f`
- Ручной перезапуск: `systemctl restart remember-stuff`
- Бэкап: скопировать `/opt/remember_stuff/data/`
- Сертификат продлевается сам; проверить таймер: `systemctl list-timers | grep certbot`
