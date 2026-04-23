"""
shared.py — общие утилиты, хелперы и клавиатуры.
"""

import html
import time
from math import ceil
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import settings
from database import MAX_PROFILES_PER_USER

PAGE_SIZE = 10  # пользователей на страницу в списке


# ──────────────────────────── Права ─────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def is_allowed(user_id: int) -> bool:
    return True if settings.BOT_MODE != "admin" else is_admin(user_id)


# ──────────────────────────── Форматирование ────────────────────────

def fmt_bytes(b: float) -> str:
    if not b:
        return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ТБ"


def fmt_handshake(ts: int) -> str:
    if not ts:
        return "никогда"
    delta = int(time.time()) - ts
    if delta < 0:
        return "только что"
    if delta < 60:
        return f"{delta} сек. назад"
    if delta < 3600:
        return f"{delta // 60} мин. назад"
    if delta < 86400:
        return f"{delta // 3600} ч. назад"
    return f"{delta // 86400} д. назад"


def menu_text(user_data: dict | None, notice: str = "") -> str:
    """
    user_data = {telegram_id, banned, profiles: [...]} или None.
    """
    prefix = f"<i>{html.escape(notice)}</i>\n\n" if notice else ""

    if user_data is None:
        return f"{prefix}🏠 <b>Главное меню</b>\n\n❌ Профилей нет"

    profiles = user_data.get("profiles", [])
    banned = user_data.get("banned", False)

    if not profiles:
        return f"{prefix}🏠 <b>Главное меню</b>\n\n❌ Профилей нет"

    lines = []
    for p in profiles:
        name = html.escape(p["vpn_name"])
        if banned or p.get("disabled"):
            icon = "🚫"
        else:
            icon = "✅"
        lines.append(f"{icon} <b>{name}</b>")

    status_block = "\n".join(lines)
    can_add = len(profiles) < MAX_PROFILES_PER_USER
    limit_note = "" if can_add else f"\n<i>Лимит профилей: {MAX_PROFILES_PER_USER}</i>"

    return (
        f"{prefix}🏠 <b>Главное меню</b>\n\n"
        f"📋 Ваши профили:\n{status_block}{limit_note}"
    )


# ──────────────────────────── Aiogram helpers ───────────────────────

async def safe_edit(msg: Message, text: str,
                    reply_markup=None, parse_mode=ParseMode.HTML) -> None:
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def delete_messages(bot: Bot, chat_id: int, msg_ids: list[int]) -> None:
    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


async def push_side_msg(state: FSMContext, msg_id: int) -> None:
    data = await state.get_data()
    ids: list[int] = data.get("side_msgs", [])
    ids.append(msg_id)
    await state.update_data(side_msgs=ids)


async def pop_side_msgs(state: FSMContext) -> list[int]:
    data = await state.get_data()
    ids: list[int] = data.get("side_msgs", [])
    await state.update_data(side_msgs=[])
    return ids


# ──────────────────────────── Peer utils ────────────────────────────

def find_peer_in_clients(clients_data: dict | None, username: str) -> dict | None:
    if not clients_data:
        return None
    for item in clients_data.get("items", []):
        if item.get("username") == username:
            peers = item.get("peers", [])
            return peers[0] if peers else None
    return None


def count_online_peers(clients_data: dict | None) -> tuple[int, int]:
    """Возвращает (онлайн, всего пиров)."""
    if not clients_data:
        return 0, 0
    total = online = 0
    for item in clients_data.get("items", []):
        for peer in item.get("peers", []):
            total += 1
            if peer.get("online"):
                online += 1
    return online, total


# ──────────────────────────── Пагинация (пользователи) ──────────────

def paginate_users(users: list[dict], page: int) -> tuple[list[dict], int]:
    total_pages = max(1, ceil(len(users) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    return users[start:start + PAGE_SIZE], total_pages


def build_users_page_text(users_page: list[dict], page: int,
                           total_pages: int, total: int,
                           global_offset: int) -> str:
    rows = []
    for i, u in enumerate(users_page, global_offset + 1):
        banned_tag = "  🚫" if u.get("banned") else ""
        profiles = u.get("profiles", [])
        profile_names = ", ".join(html.escape(p["vpn_name"]) for p in profiles) or "—"
        tg_id = u['telegram_id']
        rows.append(
            f"{i}. <a href='tg://user?id={tg_id}'>{tg_id}</a>{banned_tag}\n"
            f"   Профили: <b>{profile_names}</b>\n"
            f"   С: {u.get('created_at', '—')}"
        )
    body   = "\n\n".join(rows) if rows else "Список пуст."
    header = (
        f"👥 <b>Пользователи</b> — {total} чел. "
        f"<i>(стр. {page + 1}/{total_pages})</i>"
    )
    footer = "<i>👁 карточка · 🚫/✅ бан · 🗑 удалить профиль</i>"
    return f"{header}\n\n{body}\n\n{footer}"


# ──────────────────────────── Клавиатуры ────────────────────────────

def kb_main(has_profiles: bool, can_create: bool, admin: bool = False) -> InlineKeyboardMarkup:
    rows = []

    if has_profiles:
        rows.append([
            InlineKeyboardButton(text="👤 Мои профили",  callback_data="my_profiles"),
            InlineKeyboardButton(text="🖥 Сервер",        callback_data="server_status"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🖥 Статус сервера", callback_data="server_status"),
        ])

    if can_create:
        rows.append([
            InlineKeyboardButton(text="🚀 Создать VPN-профиль", callback_data="create_vpn"),
        ])

    if admin:
        rows.append([
            InlineKeyboardButton(text="🔧 Панель управления", callback_data="admin_panel"),
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_profile_select(profiles: list[dict], action: str) -> InlineKeyboardMarkup:
    """
    Кнопки выбора конкретного профиля.
    action: 'get_config' | 'my_info'
    """
    rows = []
    for p in profiles:
        name = html.escape(p["vpn_name"])
        dis = " 🚫" if p.get("disabled") else ""
        rows.append([
            InlineKeyboardButton(
                text=f"📋 {name}{dis}",
                callback_data=f"{action}_profile:{p['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_my_profiles(profiles: list[dict]) -> InlineKeyboardMarkup:
    """Меню «Мои профили» — список с кнопками просмотра и удаления."""
    rows = []
    for p in profiles:
        name = html.escape(p["vpn_name"])
        dis = " 🚫" if p.get("disabled") else ""
        rows.append([
            InlineKeyboardButton(
                text=f"👁 {name}{dis}",
                callback_data=f"my_info_profile:{p['id']}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"user_del_profile:{p['id']}",
            ),
        ])
    rows.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_user_del_confirm(profile_id: int, vpn_name: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления профиля пользователем."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"user_del_profile_do:{profile_id}",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data="my_profiles"),
        ],
    ])


def kb_admin_panel() -> InlineKeyboardMarkup:
    """Главное меню админ-панели."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_list:0"),
            InlineKeyboardButton(text="📈 Статистика",   callback_data="admin_stats:0"),
        ],
        [
            InlineKeyboardButton(text="🔍 Поиск",        callback_data="admin_search"),
            InlineKeyboardButton(text="📢 Рассылка",     callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton(text="🔒 Блокировки",   callback_data="admin_ban_menu"),
            InlineKeyboardButton(text="🔎 Пиры Amnezia", callback_data="admin_all_peers"),
        ],
        [
            InlineKeyboardButton(text="📋 Экспорт CSV",  callback_data="admin_export_csv"),
        ],
        [
            InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main"),
        ],
    ])


def kb_admin_ban_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Заблокировать всех", callback_data="admin_ban_all"),
            InlineKeyboardButton(text="✅ Разблокировать всех", callback_data="admin_unban_all"),
        ],
        [
            InlineKeyboardButton(text="🔙 Назад в панель", callback_data="admin_panel"),
        ],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])


def kb_confirm_create(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Создать",   callback_data=f"confirm_create:{name}"),
            InlineKeyboardButton(text="❌ Отменить",  callback_data="cancel"),
        ],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")],
    ])


def kb_back_to_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔧 Панель управления", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🏠 В главное меню",    callback_data="back_main")],
    ])


def kb_server_status() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="server_status")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")],
    ])


# ──────────────────────────── Админ: список пользователей ───────────

def kb_admin_list(users_page: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    for u in users_page:
        banned    = u.get("banned", False)
        ban_icon  = "✅" if banned else "🚫"
        ban_label = "Разбан" if banned else "Бан"
        tg_id     = u["telegram_id"]
        rows.append([
            InlineKeyboardButton(
                text=f"👁 ID {tg_id}",
                callback_data=f"admin_user_card:{tg_id}:{page}",
            ),
            InlineKeyboardButton(
                text=f"{ban_icon} {ban_label}",
                callback_data=f"admin_ban_toggle:{tg_id}:{page}",
            ),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_list:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"· {page + 1}/{total_pages} ·", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_list:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="🔍 Поиск", callback_data="admin_search"),
        InlineKeyboardButton(text="🔧 Панель", callback_data="admin_panel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_user_card(tg_id: int, banned: bool, page: int,
                 profiles: list[dict]) -> InlineKeyboardMarkup:
    """Карточка пользователя: бан + кнопки управления профилями."""
    ban_text = "✅ Разбанить" if banned else "🚫 Заблокировать"
    rows = [
        [
            InlineKeyboardButton(
                text=ban_text,
                callback_data=f"admin_ban_toggle:{tg_id}:{page}",
            ),
            InlineKeyboardButton(
                text="✉️ Написать",
                callback_data=f"admin_msg_user:{tg_id}:{page}",
            ),
        ]
    ]

    # Кнопки профилей
    for p in profiles:
        name = html.escape(p["vpn_name"])
        dis = p.get("disabled", False)
        tog_icon = "✅ Вкл" if dis else "⏸ Откл"
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {name}",
                callback_data=f"admin_del_profile:{p['id']}:{tg_id}:{page}",
            ),
            InlineKeyboardButton(
                text=tog_icon,
                callback_data=f"admin_toggle_profile:{p['id']}:{tg_id}:{page}",
            ),
            InlineKeyboardButton(
                text="📊 Стат",
                callback_data=f"admin_profile_stat:{p['id']}:{tg_id}:{page}",
            ),
        ])

    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin_user_card:{tg_id}:{page}"),
        InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin_list:{page}"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔧 Панель", callback_data="admin_panel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_del_profile_confirm(profile_id: int, tg_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🗑 Подтвердить удаление",
                callback_data=f"admin_del_profile_do:{profile_id}:{tg_id}:{page}",
            ),
        ],
        [
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"admin_user_card:{tg_id}:{page}"),
        ],
    ])


def kb_broadcast_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 Отправить всем", callback_data="admin_broadcast_do"),
            InlineKeyboardButton(text="❌ Отменить",        callback_data="cancel"),
        ],
    ])


def kb_stats_refresh() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats:0")],
        [InlineKeyboardButton(text="🔧 Панель",   callback_data="admin_panel")],
    ])