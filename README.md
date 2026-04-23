# AmneziaVPN Telegram Bot

Telegram-бот для управления VPN-профилями AmneziaVPN с поддержкой мультипрофилей, админ-панели и Telegram Mini App.

## Возможности

- 🔐 Мультипрофили — каждый пользователь может иметь несколько VPN-профилей
- 📥 Получение конфигурационного файла (строка `vpn://` и `.vpn`-файл)
- 👤 Просмотр статуса каждого профиля (онлайн, трафик, последнее подключение)
- 🗑 Удаление собственных профилей
- 🖥 Статус сервера
- 🔔 Уведомления администратора при создании/удалении профиля
- ⚙️ Режим «только для администраторов» (`BOT_MODE=admin`)
- 🌐 Telegram Mini App — веб-интерфейс управления профилями прямо в Telegram

### Админ-панель

- 👥 Список пользователей с пагинацией (кликабельные ID → профиль в Telegram)
- 🃏 Карточка пользователя: все профили, трафик, IP, статус
- 🚫 Бан/разбан пользователей
- ⏸ Включение/отключение отдельных профилей
- 🗑 Удаление профилей с синхронизацией Amnezia API
- 📊 Статистика: трафик, топ пользователей, онлайн
- 🔍 Поиск по ID или имени профиля
- 📢 Рассылка сообщений всем пользователям
- 📋 Экспорт пользователей в CSV
- 🔎 Просмотр всех пиров Amnezia

## Структура файлов

```
bot/
├── bot.py              # Главный файл: хендлеры, регистрация, middleware
├── admin_handlers.py   # Все хендлеры админ-панели
├── shared.py           # Общие утилиты, клавиатуры, хелперы
├── config.py           # Загрузка настроек из .env (pydantic-settings)
├── database.py         # Асинхронная SQLite база (aiosqlite + шифрование)
├── amnezia_client.py   # HTTP-клиент для Amnezia Admin API (aiohttp + retry)
├── miniapp.py          # Telegram Mini App (Flask)
├── start.sh            # Скрипт запуска через screen
├── requirements.txt
├── .env
└── README.md
```

## Установка

```bash
# 1. Скопировать файлы бота
cd /root/bot

# 2. Создать виртуальное окружение
python3 -m venv /root/me
source /root/me/bin/activate

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Настроить .env
nano .env
```

## Конфигурация `.env`

| Переменная | Описание | Обязательно |
|---|---|---|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) | ✅ |
| `ADMIN_IDS` | Telegram ID администраторов через запятую: `123456,789012` | ✅ |
| `BOT_MODE` | `all` — для всех, `admin` — только для администраторов | — |
| `VPN_HOST` | Публичный адрес VPN-сервера (для отображения) | — |
| `AMNEZIA_API_URL` | URL Amnezia Admin API, например `http://127.0.0.1:4001` | ✅ |
| `AMNEZIA_API_KEY` | API-ключ (`FASTIFY_API_KEY` из `.env` amnezia-api) | ✅ |
| `AMNEZIA_PROTOCOL` | Протокол: `amneziawg2` или `xray` | — |
| `DB_PATH` | Путь к SQLite-базе | — |
| `DB_ENCRYPTION_KEY` | Ключ шифрования базы данных | ✅ |
| `MINIAPP_HOST` | Хост Flask-сервера Mini App | — |
| `MINIAPP_PORT` | Порт Flask-сервера Mini App (по умолчанию `5000`) | — |
| `MINIAPP_DEV_MODE` | `true` — пропускать проверку подписи Telegram (только для разработки) | — |

## Запуск

### Через screen (рекомендуется)

```bash
chmod +x /root/bot/start.sh
/root/bot/start.sh
```

`start.sh` запускает бота и Mini App в отдельных screen-сессиях:

```bash
screen -r vpnbot    # подключиться к боту
screen -r vpnmini   # подключиться к Mini App
screen -ls          # список всех сессий
# Ctrl+A, D — отключиться от сессии (процесс продолжает работать)
```

### Автозапуск при старте сервера

```bash
crontab -e
```

Добавить строку:

```
@reboot /root/bot/start.sh
```

### Вручную

```bash
source /root/me/bin/activate
cd /root/bot

python bot.py       # в одном терминале
python miniapp.py   # в другом терминале
```

## Правила именования профиля

- До **16 символов**
- Только буквы (латиница или кириллица) и цифры
- Имя глобально уникально

## API-эндпоинты Amnezia Admin API

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/clients` | Список всех клиентов и пиров |
| `POST` | `/clients` | Создание нового клиента |
| `PATCH` | `/clients` | Обновление статуса клиента |
| `DELETE` | `/clients` | Удаление клиента |
| `GET` | `/server` | Информация о сервере |
| `GET` | `/server/load` | Нагрузка (CPU, RAM, диск) |
| `GET` | `/healthz` | Healthcheck |

## Mini App эндпоинты (Flask)

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


Зависимости:

Для работы проекта требуется внешний AMNEZIA-API:
Репозиторий: https://github.com/kyoresuas/amnezia-api
