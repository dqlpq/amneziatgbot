# AmneziaVPN Telegram Bot

Telegram-бот для управления VPN-профилями [AmneziaVPN](https://amnezia.org) через [amnezia-api]([https://github.com/amnezia-vpn/amnezia-api](https://github.com/kyoresuas/amnezia-api)). Поддерживает мультипрофили, полноценную админ-панель, Telegram Mini App и публичный веб-сервис для выдачи конфигов по секретному ключу.

---

## Содержание

- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Зависимость: amnezia-api](#зависимость-amnezia-api)
- [Установка](#установка)
- [Конфигурация `.env`](#конфигурация-env)
- [Запуск](#запуск)
- [Nginx](#nginx)
- [Структура файлов](#структура-файлов)
- [API-эндпоинты](#api-эндпоинты)
- [Правила именования профиля](#правила-именования-профиля)

---

## Возможности

### Для пользователей
- 🔐 **Мультипрофили** — несколько независимых VPN-профилей на одного пользователя
- 📥 **Получение конфига** — строка `vpn://…` и `.vpn`-файл для импорта в AmneziaVPN
- 👤 **Статус профиля** — онлайн/офлайн, входящий/исходящий трафик, время последнего подключения
- 🗑 **Удаление профиля** — с синхронизацией в Amnezia API
- 🖥 **Статус сервера** — регион, протокол, количество клиентов
- 🔑 **Секретный ключ** (`/mykey`, `/newkey`) — для самостоятельного получения конфига на веб-сайте без Telegram
- 🌐 **Telegram Mini App** — веб-интерфейс управления прямо внутри Telegram

### Для администраторов
- 👥 Список пользователей с пагинацией (кликабельные ID → профиль в Telegram)
- 🃏 Карточка пользователя: все профили, трафик, IP, статус
- 🚫 Бан / разбан пользователей
- ⏸ Включение / отключение отдельных профилей
- 🗑 Удаление профилей с синхронизацией Amnezia API
- 📊 Статистика: трафик, топ пользователей, онлайн
- 🔍 Поиск по Telegram ID или имени профиля
- 📢 Рассылка сообщений всем пользователям
- 📋 Экспорт пользователей в CSV
- 🔎 Просмотр всех пиров Amnezia
- ⚙️ Режим «только для администраторов» (`BOT_MODE=admin`)

---

## Архитектура

Бот состоит из трёх независимых процессов, которые работают параллельно:

```
┌─────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│      bot.py          │     │     miniapp.py        │     │    web_service.py    │
│  Telegram Bot        │     │  Telegram Mini App    │     │  Публичный сайт      │
│  (aiogram polling)   │     │  (Flask :4999)        │     │  (Flask :5000)       │
└────────┬─────────────┘     └──────────┬────────────┘     └──────────┬───────────┘
         │                              │                              │
         └──────────────────────────────┴──────────────────────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │       amnezia_client.py      │
                         │  HTTP-клиент к amnezia-api   │
                         └──────────────┬───────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │       amnezia-api            │
                         │  (Fastify REST :4001)        │
                         └─────────────────────────────┘
```

Все три процесса используют одну SQLite-базу (`bot_data.db`) с шифрованием и общий `amnezia_client.py` для взаимодействия с Amnezia API.

---

## Зависимость: amnezia-api

Бот работает **поверх** [amnezia-api](https://github.com/amnezia-vpn/amnezia-api) — отдельного REST-сервера, который управляет WireGuard-пирами на сервере с AmneziaVPN.

### Установка amnezia-api

```bash
# 1. Клонировать репозиторий
git clone https://github.com/amnezia-vpn/amnezia-api.git
cd amnezia-api

# 2. Установить зависимости
npm install

# 3. Настроить .env
cp .env.example .env
nano .env
```

Минимальная конфигурация `.env` для amnezia-api:

```env
# Порт, на котором слушает API (по умолчанию 4001)
PORT=4001

# Ключ авторизации — задайте любую длинную случайную строку
FASTIFY_API_KEY=your_secret_api_key_here

# Протокол: amneziawg2 или xray
DEFAULT_PROTOCOL=amneziawg2
```

### Запуск amnezia-api

```bash
# Через screen
screen -S amnezia-api
node index.js
# Ctrl+A, D — свернуть

# Или через PM2
npm install -g pm2
pm2 start index.js --name amnezia-api
pm2 save && pm2 startup
```

### Проверка работоспособности

```bash
curl -H "x-api-key: your_secret_api_key_here" http://localhost:4001/healthz
# Должен вернуть: {"status":"ok"}

curl -H "x-api-key: your_secret_api_key_here" http://localhost:4001/clients
# Список всех клиентов
```

> **Важно:** amnezia-api должен быть запущен **до** старта бота. Бот подключается к нему по адресу из `AMNEZIA_API_URL` в `.env`.

---

## Установка

```bash
# 1. Скопировать файлы бота на сервер
mkdir -p /root/bot
# scp или git clone ваш репозиторий

# 2. Создать виртуальное окружение Python
python3 -m venv /root/me
source /root/me/bin/activate

# 3. Установить зависимости
cd /root/bot
pip install -r requirements.txt

# 4. Настроить конфигурацию
cp _env.example .env
nano .env
```

---

## Конфигурация `.env`

Скопируйте `_env.example` в `.env` и заполните значения:

```env
# ==================== Telegram Bot ====================
BOT_TOKEN=                        # Токен от @BotFather
ADMIN_IDS=[123456789,987654321]   # Telegram ID администраторов через запятую
BOT_MODE=all                      # all — для всех, admin — только для администраторов
VPN_HOST=your.domain.com          # Публичный адрес сервера (для отображения)

# ==================== Amnezia API ====================
AMNEZIA_API_URL=http://localhost:4001   # Адрес amnezia-api
AMNEZIA_API_KEY=your_secret_api_key    # FASTIFY_API_KEY из .env amnezia-api
AMNEZIA_PROTOCOL=amneziawg2            # amneziawg2 или xray

# ==================== База данных ====================
DB_PATH=./bot_data.db
DB_ENCRYPTION_KEY=                # 44 символа: a-z A-Z 0-9 - _ =

# ==================== Mini App (Flask) ====================
MINIAPP_HOST=0.0.0.0
MINIAPP_PORT=4999
MINIAPP_DEV_MODE=False            # True — отключить проверку подписи Telegram (только dev)

# ==================== Веб-сервис ====================
WEB_HOST=0.0.0.0
WEB_PORT=5000

# ==================== Короткие ссылки ====================
SHORT_LINK_DOMAIN=your.domain.com  # Домен публичного сайта
```

### Как сгенерировать `DB_ENCRYPTION_KEY`

```bash
python3 -c "
import secrets, string
chars = string.ascii_letters + string.digits + '-_='
print(''.join(secrets.choice(chars) for _ in range(44)))
"
```

### Как получить `ADMIN_IDS`

Напишите боту [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш Telegram ID.

---

## Запуск

### Через screen (рекомендуется)

Создайте файл `start.sh`:

```bash
#!/bin/bash
source /root/me/bin/activate
cd /root/bot

screen -dmS vpnbot  python bot.py
screen -dmS vpnmini python miniapp.py
screen -dmS vpnweb  python web_service.py

echo "Запущено."
echo "  screen -r vpnbot   — бот"
echo "  screen -r vpnmini  — Mini App"
echo "  screen -r vpnweb   — веб-сервис"
```

```bash
chmod +x /root/bot/start.sh
/root/bot/start.sh
```

Управление сессиями:

```bash
screen -ls             # Список запущенных сессий
screen -r vpnbot       # Подключиться к боту
screen -r vpnmini      # Подключиться к Mini App
screen -r vpnweb       # Подключиться к веб-сервису
# Ctrl+A, D — свернуть сессию (процесс продолжает работать)
```

### Автозапуск при перезагрузке сервера

```bash
crontab -e
```

Добавьте строку:

```
@reboot sleep 5 && /root/bot/start.sh
```

### Запуск вручную (для отладки)

```bash
source /root/me/bin/activate
cd /root/bot

python bot.py          # Терминал 1
python miniapp.py      # Терминал 2
python web_service.py  # Терминал 3
```

---

## Nginx

Nginx используется как обратный прокси перед Flask-сервисами. Бот (aiogram polling) не требует публичного доступа — только Mini App и веб-сервис.

> **Перед настройкой** получите SSL-сертификат:
> ```bash
> apt install certbot python3-certbot-nginx
> certbot --nginx -d your.domain.com
> ```

### Конфигурация

Создайте файл `/etc/nginx/sites-available/vpnbot` (замените `your.domain.com` на ваш домен):

```nginx
# ──────────────────────────────────────────────
# Редирект HTTP → HTTPS
# ──────────────────────────────────────────────
server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}

# ──────────────────────────────────────────────
# Основной HTTPS-блок
# ──────────────────────────────────────────────
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Заголовки безопасности
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options    "nosniff"                             always;
    add_header X-Frame-Options           "SAMEORIGIN"                          always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin"     always;

    # ── Публичный веб-сервис (выдача конфигов по ключу) ──────────────
    # Обрабатывает все запросы по умолчанию
    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }

    # ── Telegram Mini App ─────────────────────────────────────────────
    # Доступна по /app/ — именно этот URL указывается в @BotFather
    # как ссылка на Mini App: https://your.domain.com/app/
    location /app/ {
        proxy_pass         http://127.0.0.1:4999/;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # WebSocket (если Mini App использует ws://)
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";

        proxy_read_timeout 30s;
    }

    # ── Защита Amnezia API (не должен быть доступен снаружи) ─────────
    # Блокируем прямой доступ к порту 4001 через nginx
    location /api-internal/ {
        deny all;
        return 403;
    }

    # ── Размер загружаемых файлов ─────────────────────────────────────
    client_max_body_size 2M;
}
```

Активация и проверка:

```bash
ln -s /etc/nginx/sites-available/vpnbot /etc/nginx/sites-enabled/
nginx -t                # Проверить конфигурацию
systemctl reload nginx  # Применить
```

### Firewall

Закройте порты Flask и amnezia-api от внешнего доступа — они должны быть доступны **только локально**:

```bash
# Разрешить SSH, HTTP, HTTPS
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp

# Заблокировать прямой доступ к внутренним сервисам снаружи
ufw deny 4001/tcp   # amnezia-api
ufw deny 4999/tcp   # Mini App (Flask)
ufw deny 5000/tcp   # Web Service (Flask)

ufw enable
```

> **Порт AmneziaWG** (UDP, обычно 51820 или настраивается в amnezia-api) должен быть открыт для VPN-трафика:
> ```bash
> ufw allow 51820/udp
> ```

---

## Структура файлов

```
/root/bot/
├── bot.py              # Главный процесс: хендлеры, middleware, polling
├── admin_handlers.py   # Хендлеры админ-панели
├── shared.py           # Общие утилиты, клавиатуры, хелперы
├── config.py           # Настройки из .env (pydantic-settings)
├── database.py         # Асинхронная SQLite-база (aiosqlite + шифрование)
├── amnezia_client.py   # HTTP-клиент к amnezia-api (aiohttp + retry)
├── miniapp.py          # Telegram Mini App (Flask, порт 4999)
├── web_service.py      # Публичный сайт для конфигов (Flask, порт 5000)
├── requirements.txt
├── .env
├── start.sh
└── README.md
```

---

## API-эндпоинты

### Amnezia Admin API (amnezia-api)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/healthz` | Healthcheck |
| `GET` | `/clients` | Список всех клиентов и пиров |
| `POST` | `/clients` | Создание нового клиента |
| `PATCH` | `/clients` | Обновление статуса клиента |
| `DELETE` | `/clients` | Удаление клиента |
| `GET` | `/server` | Информация о сервере |
| `GET` | `/server/load` | Нагрузка (CPU, RAM, диск) |

### Mini App (Flask, порт 4999)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Главная страница Mini App |
| `GET` | `/api/me` | Профили текущего пользователя |
| `POST` | `/api/create` | Создать профиль |
| `DELETE` | `/api/profile/<id>` | Удалить свой профиль |
| `GET` | `/api/config/<id>` | Получить конфиг профиля |
| `GET` | `/api/server` | Статус сервера |
| `GET` | `/api/ping` | Пинг до VPN-сервера (ICMP, серверная сторона) |
| `POST` | `/api/validate_hash` | Валидация Telegram initData |

### Публичный веб-сервис (Flask, порт 5000)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Форма ввода секретного ключа |
| `GET` | `/<slug>` | Страница с конфигом (по короткой ссылке) |

---

## Правила именования профиля

- Максимум **16 символов**
- Только буквы (латиница или кириллица) и цифры — без пробелов и спецсимволов
- Имя **глобально уникально** в пределах одного сервера

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/menu` | Открыть меню (то же что `/start`) |
| `/mykey` | Показать секретный ключ для веб-сайта |
| `/newkey` | Сгенерировать новый секретный ключ |

---

## Зависимости Python

```
aiogram==3.17.0          # Telegram Bot API
aiohttp==3.11.18         # HTTP-клиент к amnezia-api
aiosqlite==0.20.0        # Асинхронная SQLite
pydantic-settings==2.9.1 # Загрузка .env
python-dotenv==1.1.0     # .env поддержка
flask==3.1.1             # Mini App + веб-сервис
```

---

## Частые проблемы

**Бот не отвечает после запуска**
- Проверьте `BOT_TOKEN` в `.env`
- Убедитесь, что amnezia-api запущен: `curl http://localhost:4001/healthz`

**Ошибка `Database timeout`**
- База данных заблокирована другим процессом. Перезапустите все три процесса бота.

**`AMNEZIA_API_KEY` не принимается**
- Значение в `AMNEZIA_API_KEY` должно совпадать с `FASTIFY_API_KEY` в `.env` amnezia-api.

**Mini App не открывается в Telegram**
- Убедитесь, что nginx корректно проксирует `/app/` на порт `4999`.
- Проверьте, что URL Mini App в @BotFather указан как `https://your.domain.com/app/`.
