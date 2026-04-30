# AmneziaVPN Telegram Bot

Telegram-бот для управления VPN-профилями [AmneziaVPN](https://amnezia.org) через [amnezia-api](https://github.com/kyoresuas/amnezia-api). Поддерживает мультипрофили, админ-панель, Telegram Mini App и веб-сервис для выдачи конфигов по секретному ключу.

---

## Содержание

- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Зависимость: amnezia-api](#зависимость-amnezia-api)
- [Установка](#установка)
- [Конфигурация `.env`](#конфигурация-env)
- [Запуск](#запуск)
- [Nginx и Firewall](#nginx-и-firewall)
- [Структура файлов](#структура-файлов)
- [API-эндпоинты](#api-эндпоинты)
- [Команды бота](#команды-бота)

---

## Возможности

### Для пользователей
- 🔐 **Мультипрофили** — несколько независимых VPN-профилей
- 📥 **Получение конфига** — строка `vpn://…` и `.vpn`-файл для импорта в AmneziaVPN
- 👤 **Статус профиля** — онлайн/офлайн, трафик, время последнего подключения
- 🗑 **Удаление профиля** с синхронизацией в Amnezia API
- 🖥 **Статус сервера** — регион, протокол, количество клиентов
- 🔑 **Секретный ключ** (`/mykey`, `/newkey`) — для получения конфига на сайте без Telegram
- 🌐 **Telegram Mini App** — веб-интерфейс управления внутри Telegram

### Для администраторов
- 👥 Список пользователей с пагинацией (кликабельные ID → профиль в Telegram)
- 🃏 Карточка пользователя: профили, трафик, IP, статус
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

Три независимых процесса работают параллельно, используя общую SQLite-базу и `amnezia_client.py`:

```
┌─────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│      bot.py          │     │     miniapp.py        │     │    web_service.py    │
│  Telegram Bot        │     │  Telegram Mini App    │     │  Публичный сайт      │
│  (aiogram polling)   │     │  (Flask :4999)        │     │  (Flask :5000)       │
└────────┬─────────────┘     └──────────┬────────────┘     └──────────┬───────────┘
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

---

## Зависимость: amnezia-api

Бот работает поверх [amnezia-api](https://github.com/amnezia-vpn/amnezia-api) — REST-сервера, управляющего WireGuard-пирами на сервере с AmneziaVPN.

### Установка и запуск

```bash
git clone https://github.com/amnezia-vpn/amnezia-api.git
cd amnezia-api
pip install -r requirements.txt
sudo apt install screen
cp .env.example .env
nano .env
```

Минимальная конфигурация `.env` для amnezia-api:

```env
PORT=4001
FASTIFY_API_KEY=your_secret_api_key_here
DEFAULT_PROTOCOL=amneziawg2
```

```bash
bash start.sh   # запуск
bash stop.sh    # остановка
```

**Проверка:**

```bash
curl -H "x-api-key: your_secret_api_key_here" http://localhost:4001/healthz
# {"status":"ok"}
```

> **Важно:** amnezia-api должен быть запущен до старта бота. Адрес задаётся в `AMNEZIA_API_URL`.

---

## Установка

```bash
mkdir -p /root/bot && cd /root/bot
# scp или git clone ваш репозиторий

python3 -m venv /root/me
source /root/me/bin/activate
pip install -r requirements.txt

cp _env.example .env
nano .env
```

---

## Конфигурация `.env`

```env
# ── Telegram Bot ──────────────────────────────────────
BOT_TOKEN=                        # Токен от @BotFather
ADMIN_IDS=[123456789,987654321]   # Telegram ID администраторов
BOT_MODE=all                      # all — для всех, admin — только для администраторов
VPN_HOST=your.domain.com

# ── Amnezia API ───────────────────────────────────────
AMNEZIA_API_URL=http://localhost:4001
AMNEZIA_API_KEY=your_secret_api_key   # совпадает с FASTIFY_API_KEY
AMNEZIA_PROTOCOL=amneziawg2           # amneziawg2 или xray

# ── База данных ───────────────────────────────────────
DB_PATH=./bot_data.db
DB_ENCRYPTION_KEY=                # 44 символа: a-z A-Z 0-9 - _ =

# ── Mini App (Flask) ──────────────────────────────────
MINIAPP_HOST=0.0.0.0
MINIAPP_PORT=4999
MINIAPP_DEV_MODE=False            # True — отключить проверку подписи (только dev)

# ── Веб-сервис ────────────────────────────────────────
WEB_HOST=0.0.0.0
WEB_PORT=5000
SHORT_LINK_DOMAIN=your.domain.com
```

**Генерация `DB_ENCRYPTION_KEY`:**

```bash
python3 -c "
import secrets, string
chars = string.ascii_letters + string.digits + '-_='
print(''.join(secrets.choice(chars) for _ in range(44)))
"
```

**Получение `ADMIN_IDS`:** напишите боту [@userinfobot](https://t.me/userinfobot).

---

## Запуск

### Через screen

Создайте `start.sh`:

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
chmod +x start.sh && ./start.sh
```

Управление сессиями: `screen -ls`, `screen -r vpnbot`, `Ctrl+A D` — свернуть.

### Автозапуск при перезагрузке

```bash
crontab -e
# добавить:
@reboot sleep 5 && /root/bot/start.sh
```

---

## Nginx и Firewall

Nginx проксирует Mini App и веб-сервис. Бот (polling) не требует публичного доступа.

**SSL-сертификат:**

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d your.domain.com
```

**`/etc/nginx/sites-available/vpnbot`:**

```nginx
server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your.domain.com;

    ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options    "nosniff"                             always;
    add_header X-Frame-Options           "SAMEORIGIN"                          always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin"     always;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }

    # URL Mini App указывается в @BotFather: https://your.domain.com/app/
    location /app/ {
        proxy_pass         http://127.0.0.1:4999/;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 30s;
    }

    client_max_body_size 2M;
}
```

```bash
ln -s /etc/nginx/sites-available/vpnbot /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

**Firewall** — внутренние порты закрыть снаружи:

```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 51820/udp  # AmneziaWG (UDP)
ufw deny 4001/tcp    # amnezia-api
ufw deny 4999/tcp    # Mini App
ufw deny 5000/tcp    # Web Service
ufw enable
```

---

## Структура файлов

```
/root/bot/
├── bot.py              # Telegram-бот: хендлеры, middleware, polling
├── admin_handlers.py   # Хендлеры админ-панели
├── shared.py           # Утилиты, клавиатуры, хелперы
├── config.py           # Настройки из .env (pydantic-settings)
├── database.py         # Асинхронная SQLite (aiosqlite + шифрование)
├── amnezia_client.py   # HTTP-клиент к amnezia-api (aiohttp + retry)
├── miniapp.py          # Telegram Mini App (Flask :4999)
├── web_service.py      # Веб-сервис для конфигов (Flask :5000)
├── requirements.txt
├── .env
└── start.sh
```

---

## API-эндпоинты

### Amnezia API (`localhost:4001`)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/healthz` | Healthcheck |
| `GET` | `/clients` | Список всех клиентов |
| `POST` | `/clients` | Создание клиента |
| `PATCH` | `/clients` | Обновление статуса |
| `DELETE` | `/clients` | Удаление клиента |
| `GET` | `/server` | Информация о сервере |
| `GET` | `/server/load` | Нагрузка (CPU, RAM, диск) |

### Mini App (`localhost:4999`)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Главная страница |
| `GET` | `/api/me` | Профили текущего пользователя |
| `POST` | `/api/create` | Создать профиль |
| `DELETE` | `/api/profile/<id>` | Удалить профиль |
| `GET` | `/api/config/<id>` | Получить конфиг |
| `GET` | `/api/server` | Статус сервера |
| `GET` | `/api/ping` | Пинг до VPN-сервера |
| `POST` | `/api/validate_hash` | Валидация Telegram initData |

### Веб-сервис (`localhost:5000`)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Форма ввода секретного ключа |
| `GET` | `/<slug>` | Страница с конфигом (по короткой ссылке) |

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start`, `/menu` | Главное меню |
| `/mykey` | Показать секретный ключ |
| `/newkey` | Сгенерировать новый ключ |

---

## Правила именования профиля

- Максимум **16 символов**
- Только буквы (латиница или кириллица) и цифры — без пробелов и спецсимволов
- Имя **глобально уникально** в пределах сервера

---

## Зависимости Python

```
aiogram==3.17.0
aiohttp==3.11.18
aiosqlite==0.20.0
pydantic-settings==2.9.1
python-dotenv==1.1.0
flask==3.1.1
```

---

## Частые проблемы

**Бот не отвечает**
→ Проверьте `BOT_TOKEN` и убедитесь, что amnezia-api запущен: `curl http://localhost:4001/healthz`

**`Database timeout`**
→ База заблокирована другим процессом. Перезапустите все три процесса.

**`AMNEZIA_API_KEY` не принимается**
→ Значение должно совпадать с `FASTIFY_API_KEY` в `.env` amnezia-api.

**Mini App не открывается в Telegram**
→ Проверьте, что nginx проксирует `/app/` на порт `4999` и URL в @BotFather: `https://your.domain.com/app/`
