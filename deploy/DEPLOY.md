# Деплой на VPS (Debian 13, Vultr) + авто-деплой через GitHub Actions

Итог:
- бот и дашборд — systemd-сервис под непривилегированным пользователем `thefootnotes` (с харденингом);
- nginx отдаёт дашборд по HTTPS с паролем на `https://thefootnotes.app`;
- данные (SQLite + файлы) лежат на диске в `/opt/thefootnotes/data` и не теряются;
- каждый `git push` в `main` автоматически выкатывается на сервер.

Значения-примеры: домен `thefootnotes.app`, путь `/opt/thefootnotes`, пользователь `thefootnotes`,
репозиторий `github.com/USER/thefootnotes` (подставь свой).

---

## Шаг 0. DNS

В DNS-настройках домена `thefootnotes.app` добавь запись на корень домена:

```
Тип: A    Имя: @    Значение: <публичный IP сервера>
```

IP сервера можно узнать на самом сервере: `curl -4 ifconfig.me`. Проверить, что применилось:
`ping thefootnotes.app` (должен резолвиться в IP сервера; может занять несколько минут).

> `.app` — TLD из списка HSTS preload: браузеры работают с ним только по HTTPS. У нас HTTPS
> настраивается через certbot (шаг 10), так что всё в порядке — просто plain-HTTP доступа не будет.

> **Домен на Cloudflare?** Если A-запись стоит под проксёй (оранжевое облако), `ping thefootnotes.app`
> покажет **IP Cloudflare**, а не твоего сервера — это нормально, а не ошибка. При этом меняются шаги
> **8 (nginx)** и **10 (сертификат)**, а для авто-деплоя в `SSH_HOST` пойдёт сырой IP. Полностью
> см. приложение [**«Cloudflare (оранжевое облако)»**](#cloudflare-оранжевое-облако) внизу — оно
> заменяет шаги 8 и 10 и меняет секрет `SSH_HOST`.

---

## Часть 1. Разовая настройка сервера (под root)

### 1. Пакеты

```bash
apt update
apt install -y git python3 python3-venv python3-pip nginx certbot python3-certbot-nginx apache2-utils
```

### 2. Пользователь `thefootnotes`

Отдельный непривилегированный пользователь: под ним и работает сервис, и заходит деплой.

```bash
useradd -m -d /home/thefootnotes -s /bin/bash thefootnotes
```

### 3. Клонируем репозиторий в /opt/thefootnotes

```bash
mkdir -p /opt/thefootnotes
chown thefootnotes:thefootnotes /opt/thefootnotes
# Публичный репозиторий — по https, без ключей:
sudo -u thefootnotes git clone https://github.com/USER/thefootnotes.git /opt/thefootnotes
# (Если репозиторий приватный — см. раздел «Приватный репозиторий» внизу.)

# Папка данных должна существовать до первого старта (из-за строгого харденинга):
sudo -u thefootnotes mkdir -p /opt/thefootnotes/data/files
```

### 4. Виртуальное окружение и зависимости

```bash
sudo -u thefootnotes python3 -m venv /opt/thefootnotes/.venv
sudo -u thefootnotes /opt/thefootnotes/.venv/bin/pip install --upgrade pip
sudo -u thefootnotes /opt/thefootnotes/.venv/bin/pip install -r /opt/thefootnotes/requirements.txt
```

### 5. Файл .env с ключами

```bash
sudo -u thefootnotes nano /opt/thefootnotes/.env
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
chmod 600 /opt/thefootnotes/.env
```

### 6. Право рестартовать сервис без пароля (узкий sudo)

Чтобы деплой мог перезапускать сервис, но не более того:

```bash
echo 'thefootnotes ALL=(root) NOPASSWD: /usr/bin/systemctl restart thefootnotes' \
  > /etc/sudoers.d/thefootnotes
chmod 440 /etc/sudoers.d/thefootnotes
```

### 7. systemd-сервис

```bash
cp /opt/thefootnotes/deploy/thefootnotes.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now thefootnotes
systemctl status thefootnotes --no-pager
journalctl -u thefootnotes -n 30 --no-pager   # должно быть «Телеграм-бот запущен»
```

С этого момента **бот уже работает** — можно писать ему в Telegram.

### 8. nginx

```bash
cp /opt/thefootnotes/deploy/nginx-thefootnotes.conf /etc/nginx/sites-available/thefootnotes
ln -sf /etc/nginx/sites-available/thefootnotes /etc/nginx/sites-enabled/thefootnotes
```

> Дефолтный сайт nginx (`/etc/nginx/sites-enabled/default`) НЕ удаляем — если на сервере
> есть другие домены, он их не сломает. Наш конфиг ловит только `thefootnotes.app`.

### 9. Пароль на дашборд

```bash
htpasswd -c /etc/nginx/.htpasswd-thefootnotes boris   # спросит и подтвердит пароль
```

### 10. HTTPS-сертификат

```bash
nginx -t && systemctl reload nginx
certbot --nginx -d thefootnotes.app    # спросит email и согласие; сам настроит 443 и редирект
```

> **Cloudflare с проксёй?** Пропусти шаги 8 и 10 — они не сработают (certbot по HTTP-01 достучится
> до Cloudflare, а не до nginx). Вместо них выполни приложение [«Cloudflare (оранжевое облако)»](#cloudflare-оранжевое-облако).

### 11. Файрвол, если включён ufw

```bash
ufw status                       # если "inactive" — пропусти
ufw allow 22 && ufw allow 80 && ufw allow 443
```

Проверка: открой **https://thefootnotes.app** → логин/пароль из шага 9 → дашборд.

---

## Часть 2. Авто-деплой через GitHub Actions

Workflow уже лежит в `.github/workflows/deploy.yml`. Он при каждом пуше в `main` заходит
на сервер по SSH и делает `git pull` + `pip install` + рестарт сервиса.

### 1. Ключ для деплоя (SSH)

**На своём компе** создай отдельную пару ключей для CI:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/thefootnotes_deploy -N "" -C "github-actions-thefootnotes"
```

**Публичную** часть добавь пользователю `thefootnotes` на сервере:

```bash
# скопируй содержимое ~/.ssh/thefootnotes_deploy.pub, затем на сервере:
sudo -u thefootnotes mkdir -p /home/thefootnotes/.ssh
sudo -u thefootnotes tee -a /home/thefootnotes/.ssh/authorized_keys   # вставь строку, Ctrl+D
chmod 700 /home/thefootnotes/.ssh && chmod 600 /home/thefootnotes/.ssh/authorized_keys
chown -R thefootnotes:thefootnotes /home/thefootnotes/.ssh
```

### 2. Секреты в GitHub

В репозитории → Settings → Secrets and variables → Actions → New repository secret:

| Имя | Значение |
|-----|----------|
| `SSH_HOST` | `thefootnotes.app` (**Cloudflare с проксёй → сырой IP сервера**, см. ниже) |
| `SSH_USER` | `thefootnotes` |
| `SSH_KEY`  | содержимое **приватного** файла `~/.ssh/thefootnotes_deploy` (целиком) |

> **Cloudflare с проксёй?** Порт 22 (SSH) Cloudflare не проксирует — подключение к `thefootnotes.app:22`
> уйдёт на edge Cloudflare и повиснет. Поэтому в `SSH_HOST` укажи **публичный IP сервера** (деплою домен
> не нужен, ему надо просто достучаться до машины). Веб при этом остаётся под проксёй.

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
3. В `.env` прописать `DATABASE_URL=postgresql+psycopg://thefootnotes:пароль@localhost:5432/thefootnotes`.
4. Для миграций схемы подключить Alembic (сейчас авто-миграция колонок работает только для SQLite).

Старые данные из SQLite при желании перельём отдельным скриптом.

---

## Приватный репозиторий (если не хочешь делать публичным)

Для `git pull` на сервере нужен read-only доступ к репозиторию:

1. На сервере: `sudo -u thefootnotes ssh-keygen -t ed25519 -f /home/thefootnotes/.ssh/github -N ""`
2. Содержимое `/home/thefootnotes/.ssh/github.pub` добавь в GitHub → репозиторий → Settings →
   Deploy keys (read-only).
3. Настрой git на использование этого ключа и клонируй по SSH:
   `git@github.com:USER/thefootnotes.git`.

---

## Cloudflare (оранжевое облако)

Это приложение — для случая, когда домен управляется Cloudflare и A-запись `@` стоит **под проксёй
(оранжевое облако)**. Оно **заменяет шаги 8 и 10** основной инструкции и меняет секрет `SSH_HOST`.
Всё остальное (юзер, клон, venv, `.env`, sudo, systemd, htpasswd, ufw) делается как в основной части.

Как это устроено: HTTPS для браузера обеспечивает сам Cloudflare (у edge есть сертификат на домен —
и требование `.app` про обязательный HTTPS закрывается автоматически). Между Cloudflare и нашим
сервером шифрование настраиваем через **Cloudflare Origin Certificate**. certbot не нужен.

### CF-1. Режим SSL/TLS

Cloudflare → **SSL/TLS → Overview** → выбери **Full (strict)**.

> Не оставляй «Flexible»: origin будет редиректить на HTTPS, а Cloudflare ходить по HTTP — получится
> бесконечный редирект.

### CF-2. Origin-сертификат

Cloudflare → **SSL/TLS → Origin Server → Create Certificate** → оставь настройки по умолчанию
(RSA, срок 15 лет, hostnames `thefootnotes.app` и `*.thefootnotes.app`) → **Create**.

Cloudflare покажет два блока — **Origin Certificate** и **Private Key**. Сохрани их на сервере:

```bash
mkdir -p /etc/ssl/cloudflare
nano /etc/ssl/cloudflare/thefootnotes.pem   # вставь блок Origin Certificate
nano /etc/ssl/cloudflare/thefootnotes.key   # вставь блок Private Key
chmod 600 /etc/ssl/cloudflare/thefootnotes.key
```

### CF-3. nginx (замена шага 8)

Используем готовый конфиг с блоком 443 и путями к origin-сертификату:

```bash
cp /opt/thefootnotes/deploy/nginx-thefootnotes-cloudflare.conf /etc/nginx/sites-available/thefootnotes
ln -sf /etc/nginx/sites-available/thefootnotes /etc/nginx/sites-enabled/thefootnotes
nginx -t && systemctl reload nginx
```

Шаг 10 (certbot) **пропускаем полностью** — сертификат уже на месте.

### CF-4. Файрвол (опционально, но желательно)

Раз весь веб-трафик идёт через Cloudflare, порты 80/443 можно открыть только для диапазонов
Cloudflare, чтобы origin не дёргали напрямую. Актуальные диапазоны: <https://www.cloudflare.com/ips/>.
Порт 22 при этом оставь открытым (нужен для деплоя). Если возиться не хочется — обычный
`ufw allow 80,443` из шага 11 тоже работает.

### CF-5. Секрет `SSH_HOST` = IP

В GitHub-секретах (Часть 2, шаг 2) в `SSH_HOST` поставь **публичный IP сервера**, а не `thefootnotes.app`
— Cloudflare не проксирует порт 22. Проверь со своего компа:

```bash
ssh -i ~/.ssh/thefootnotes_deploy thefootnotes@<IP-сервера> "echo ok"
```

**Проверка:** открой **https://thefootnotes.app** → логин/пароль из шага 9 → дашборд.
В Cloudflare → SSL/TLS → Overview статус должен быть без ошибок сертификата origin.

---

## Полезное

- Логи в реальном времени: `journalctl -u thefootnotes -f`
- Ручной перезапуск: `systemctl restart thefootnotes`
- Бэкап: скопировать `/opt/thefootnotes/data/`
- Сертификат продлевается сам; проверить таймер: `systemctl list-timers | grep certbot`
