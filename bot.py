import asyncio
import html
import json
import logging
import re
import time
from typing import Callable, Any, Awaitable

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    BufferedInputFile, BotCommand, BotCommandScopeDefault,
)

from config import settings
from database import Database, MAX_PROFILES_PER_USER
from amnezia_client import AmneziaClient
from admin_handlers import register_admin_handlers
from shared import (
    is_admin, is_allowed, fmt_bytes, fmt_handshake,
    safe_edit, delete_messages, push_side_msg, pop_side_msgs,
    find_peer_in_clients,
    menu_text,
    kb_main, kb_cancel, kb_confirm_create, kb_back,
    kb_profile_select, kb_my_profiles, kb_server_status,
    kb_user_del_confirm,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

MAX_INPUT_LENGTH = 4096
VPN_NAME_RE = re.compile(r"^[a-zA-Z\u0430-\u044f\u0410-\u042f\u0451\u04010-9]+$")


# ──────────────────────────── Middleware ────────────────────────────

class DIMiddleware(BaseMiddleware):
    def __init__(self, db, amnezia):
        self.db = db
        self.amnezia = amnezia

    async def __call__(self, handler, event, data):
        data["db"] = self.db
        data["amnezia"] = self.amnezia
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 0.7):
        self.limit = limit
        self.users: dict[int, float] = {}

    async def __call__(self, handler, event, data):
        uid = None
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id

        if uid:
            now = time.monotonic()
            if uid in self.users and (now - self.users[uid]) < self.limit:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Не так быстро, подождите…", show_alert=False)
                    except Exception:
                        pass
                return
            self.users[uid] = now

        return await handler(event, data)


class BannedUserMiddleware(BaseMiddleware):
    """Блокирует взаимодействия от забаненных пользователей."""

    async def __call__(self, handler, event, data):
        db: Database = data.get("db")
        uid = None
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id

        if uid and db:
            banned = await db.get_user_banned(uid)
            if banned:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("🚫 Ваш аккаунт заблокирован.", show_alert=True)
                    except Exception:
                        pass
                elif isinstance(event, Message):
                    try:
                        await event.delete()
                    except Exception:
                        pass
                return

        return await handler(event, data)


class CreateUserStates(StatesGroup):
    waiting_for_name = State()


# ──────────────────────────── Валидация ─────────────────────────────

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = text[:MAX_INPUT_LENGTH]
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()


def validate_vpn_name(name: str) -> tuple[bool, str]:
    if not name:
        return False, "Имя не может быть пустым."
    if len(name) > 16:
        return False, f"Слишком длинное ({len(name)} симв.). Максимум — 16."
    if not VPN_NAME_RE.match(name):
        return False, "Только буквы (латиница/кириллица) и цифры."
    return True, ""


# ──────────────────────────── Хендлеры ──────────────────────────────

async def _build_main_menu(uid: int, db: Database) -> tuple[str, Any]:
    user_data = await db.get_user(uid)
    admin = is_admin(uid)
    has_profiles = bool(user_data and user_data.get("profiles"))
    can_create = not user_data or await db.can_create_profile(uid)
    return menu_text(user_data), kb_main(has_profiles, can_create, admin)


async def cmd_start(message: Message, state: FSMContext, db: Database):
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return

    try:
        await message.delete()
    except Exception:
        pass

    if not is_allowed(uid):
        return

    data = await state.get_data()

    if old_menu := data.get("menu_msg_id"):
        try:
            await message.bot.delete_message(message.chat.id, old_menu)
        except Exception:
            pass

    side = data.get("side_msgs", [])
    if side:
        await delete_messages(message.bot, message.chat.id, side)

    await state.clear()

    text, kb = await _build_main_menu(uid, db)
    sent = await message.answer(
        f"👋 Привет, <b>{html.escape(message.from_user.first_name or '')}</b>!\n\n{text}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await state.update_data(menu_msg_id=sent.message_id)


async def cmd_menu(message: Message, state: FSMContext, db: Database):
    await cmd_start(message, state, db)


async def catch_all_messages(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


async def cb_back_main(callback: CallbackQuery, state: FSMContext, db: Database):
    side = await pop_side_msgs(state)
    await delete_messages(callback.bot, callback.message.chat.id, side)
    await state.clear()
    text, kb = await _build_main_menu(callback.from_user.id, db)
    await safe_edit(callback.message, text, reply_markup=kb)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.answer()


async def cb_cancel(callback: CallbackQuery, state: FSMContext, db: Database):
    side = await pop_side_msgs(state)
    await delete_messages(callback.bot, callback.message.chat.id, side)
    await state.clear()
    text, kb = await _build_main_menu(callback.from_user.id, db)
    await safe_edit(callback.message, menu_text(await db.get_user(callback.from_user.id), "Действие отменено"), reply_markup=kb)
    await state.update_data(menu_msg_id=callback.message.message_id)
    await callback.answer()


async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ──────────────────────────── Создание профиля ──────────────────────

async def cb_create_vpn(callback: CallbackQuery, state: FSMContext, db: Database):
    uid = callback.from_user.id
    if not is_allowed(uid):
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    if not await db.can_create_profile(uid):
        await callback.answer(
            f"⚠️ Достигнут лимит профилей ({MAX_PROFILES_PER_USER}).",
            show_alert=True,
        )
        return

    side = await pop_side_msgs(state)
    await delete_messages(callback.bot, callback.message.chat.id, side)
    await state.set_state(CreateUserStates.waiting_for_name)
    await state.update_data(menu_msg_id=callback.message.message_id)

    count = await db.count_profiles(uid)
    await safe_edit(
        callback.message,
        f"✏️ <b>Создание профиля AmneziaVPN</b>\n\n"
        f"У вас {count}/{MAX_PROFILES_PER_USER} профилей.\n\n"
        "Введите имя нового профиля:\n\n"
        "• до <b>16 символов</b>\n"
        "• только <b>буквы</b> и <b>цифры</b>",
        reply_markup=kb_cancel(),
    )
    await callback.answer()


async def process_vpn_name(message: Message, state: FSMContext, db: Database):
    uid = message.from_user.id if message.from_user else None
    if uid is None or not is_allowed(uid):
        await state.clear()
        return

    name = sanitize_text(message.text or "")
    chat_id = message.chat.id
    bot = message.bot

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    menu_msg_id = data.get("menu_msg_id")

    async def edit_menu(text: str, kb=None):
        kb = kb or kb_cancel()
        if menu_msg_id:
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=menu_msg_id,
                    reply_markup=kb, parse_mode="HTML",
                )
                return
            except Exception:
                pass
        sent = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        await state.update_data(menu_msg_id=sent.message_id)

    valid, error = validate_vpn_name(name)
    if not valid:
        await edit_menu(f"⚠️ {error}\n\nВведите другое имя:")
        return

    if await db.is_vpn_name_taken(name):
        await edit_menu(f"⚠️ Имя <b>{html.escape(name)}</b> уже занято. Введите другое:")
        return

    await state.update_data(vpn_name=name)
    await edit_menu(
        f"🔍 <b>Подтверждение</b>\n\nСоздать профиль <b>{html.escape(name)}</b>?",
        kb=kb_confirm_create(name),
    )


async def cb_confirm_create(callback: CallbackQuery, state: FSMContext,
                             db: Database, amnezia: AmneziaClient):
    uid = callback.from_user.id

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    name = sanitize_text(parts[1])

    valid, error = validate_vpn_name(name)
    if not valid:
        await callback.answer(f"❌ {error}", show_alert=True)
        return

    if not await db.can_create_profile(uid):
        await state.clear()
        await callback.answer(
            f"⚠️ Достигнут лимит профилей ({MAX_PROFILES_PER_USER}).",
            show_alert=True,
        )
        return

    await safe_edit(callback.message, "⏳ Создаю профиль, подождите…")
    await callback.answer()

    try:
        result = await amnezia.create_user(name)
        if result is None:
            await safe_edit(
                callback.message,
                "❌ <b>Ошибка</b>\n\nНе удалось создать профиль. Попробуйте позже.",
                reply_markup=kb_back(),
            )
            return

        peer_id = result.get("client", {}).get("id")
        await db.add_profile(uid, name, peer_id, json.dumps(result, ensure_ascii=False))
        await state.clear()
        await state.update_data(menu_msg_id=callback.message.message_id)

        user_data = await db.get_user(uid)
        admin = is_admin(uid)
        has_profiles = bool(user_data and user_data.get("profiles"))
        can_create = await db.can_create_profile(uid)

        await safe_edit(
            callback.message,
            f"✅ <b>Профиль создан!</b>\n\nИмя: <b>{html.escape(name)}</b>\n\n"
            f"Откройте <b>«Мои профили»</b> для просмотра конфигурации.",
            reply_markup=kb_main(has_profiles=has_profiles, can_create=can_create, admin=admin),
        )

        if not is_admin(uid):
            uname = callback.from_user.username or ""
            safe_u = html.escape(uname)
            tg_ref = f"@{safe_u}" if safe_u else html.escape(callback.from_user.first_name or "")
            msg = (
                f"🆕 <b>Новый профиль</b>\n\n"
                f"👤 {tg_ref} (ID: <code>{uid}</code>)\n"
                f"🔑 Имя: <b>{html.escape(name)}</b>"
            )
            for aid in settings.ADMIN_IDS:
                try:
                    await callback.bot.send_message(aid, msg, parse_mode="HTML")
                except Exception:
                    pass

    except Exception as e:
        logger.error("Ошибка создания профиля: %s", e)
        await state.clear()
        await safe_edit(
            callback.message,
            "❌ Непредвиденная ошибка. Попробуйте позже.",
            reply_markup=kb_back(),
        )


# ──────────────────────────── Получить конфиг ───────────────────────

async def cb_get_config(callback: CallbackQuery, state: FSMContext, db: Database):
    """Показывает список профилей для выбора конфига."""
    uid = callback.from_user.id
    profiles = await db.get_profiles(uid)
    if not profiles:
        await callback.answer("❌ У вас нет профилей.", show_alert=True)
        return

    if len(profiles) == 1:
        # Если профиль один — сразу выдаём конфиг
        await _send_config_for_profile(callback, state, db, profiles[0])
        return

    side = await pop_side_msgs(state)
    await delete_messages(callback.bot, callback.message.chat.id, side)
    await safe_edit(
        callback.message,
        "📥 <b>Выберите профиль для получения конфига:</b>",
        reply_markup=kb_profile_select(profiles, "get_config"),
    )
    await callback.answer()


async def cb_get_config_profile(callback: CallbackQuery, state: FSMContext,
                                 db: Database, amnezia: AmneziaClient):
    """Конфиг конкретного профиля (выбор из списка)."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    profile_id = int(parts[1]) if parts[1].isdigit() else -1
    profile = await db.get_profile_by_id(profile_id)
    if not profile or profile["telegram_id"] != callback.from_user.id:
        await callback.answer("❌ Профиль не найден.", show_alert=True)
        return

    await _send_config_for_profile(callback, state, db, profile, amnezia)


async def _send_config_for_profile(callback: CallbackQuery, state: FSMContext,
                                    db: Database, profile: dict,
                                    amnezia: AmneziaClient | None = None):
    if profile.get("disabled"):
        await callback.answer("🚫 Этот профиль отключён администратором.", show_alert=True)
        return

    side = await pop_side_msgs(state)
    await delete_messages(callback.bot, callback.message.chat.id, side)
    await safe_edit(callback.message, "⏳ Формирую конфиг…")
    await callback.answer()

    vpn_name = profile["vpn_name"]
    config_str = None

    raw = profile.get("raw_response")
    if raw:
        try:
            config_str = json.loads(raw).get("client", {}).get("config")
        except Exception:
            pass

    if not config_str and amnezia:
        fresh = await amnezia.get_client_config(profile.get("peer_id") or vpn_name)
        if fresh:
            config_str = fresh

    if not config_str:
        await safe_edit(
            callback.message,
            "❌ <b>Конфиг недоступен</b>\n\nОбратитесь к администратору.",
            reply_markup=kb_back(),
        )
        return

    fname = f"{vpn_name}.vpn"

    st = await callback.message.answer(
        f"📋 <b>Строка для импорта</b> — <code>{html.escape(vpn_name)}</code>\n"
        f"<i>Нажмите, чтобы скопировать:</i>\n\n"
        f"<blockquote expandable><code>{html.escape(config_str)}</code></blockquote>\n\n"
        f"AmneziaVPN → «+» → <i>Вставить из буфера обмена</i>",
        parse_mode="HTML",
    )
    await push_side_msg(state, st.message_id)

    sf = await callback.message.answer_document(
        BufferedInputFile(config_str.encode(), filename=fname),
        caption=(
            f"📁 <b>{html.escape(vpn_name)}.vpn</b>\n\n"
            f"AmneziaVPN → «+» → <i>Импорт из файла</i>"
        ),
        parse_mode="HTML",
    )
    await push_side_msg(state, sf.message_id)

    try:
        await callback.message.delete()
    except Exception:
        pass

    sm = await callback.message.answer(
        "📥 <b>Конфиг отправлен выше ⬆️</b>\n\n"
        "Используйте <b>строку</b> (буфер обмена) или <b>файл</b> для импорта.",
        reply_markup=kb_back(),
        parse_mode="HTML",
    )
    await state.update_data(menu_msg_id=sm.message_id)


# ──────────────────────────── Мои профили ───────────────────────────

async def cb_my_profiles(callback: CallbackQuery, db: Database):
    """Список профилей пользователя."""
    uid = callback.from_user.id
    profiles = await db.get_profiles(uid)
    if not profiles:
        await callback.answer("❌ У вас нет профилей.", show_alert=True)
        return

    await safe_edit(
        callback.message,
        f"👤 <b>Мои профили</b> ({len(profiles)}/{MAX_PROFILES_PER_USER})\n\n"
        "Выберите профиль для просмотра статистики:",
        reply_markup=kb_my_profiles(profiles),
    )
    await callback.answer()


async def cb_my_info_profile(callback: CallbackQuery, db: Database, amnezia: AmneziaClient):
    """Информация о конкретном профиле пользователя."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    profile_id = int(parts[1]) if parts[1].isdigit() else -1
    profile = await db.get_profile_by_id(profile_id)
    if not profile or profile["telegram_id"] != callback.from_user.id:
        await callback.answer("❌ Профиль не найден.", show_alert=True)
        return

    uid = callback.from_user.id
    admin = is_admin(uid)
    await safe_edit(callback.message, "⏳ Загружаю данные…")
    await callback.answer()

    vpn_name = profile["vpn_name"]
    peer_id = profile.get("peer_id") or "—"
    created_at = profile.get("created_at", "—")
    last_ip = profile.get("last_ip") or "—"
    disabled = profile.get("disabled", False)
    user_banned = await db.get_user_banned(uid)

    clients = await amnezia.get_all_clients()
    peer = find_peer_in_clients(clients, vpn_name)

    if peer:
        ep = peer.get("endpoint") or ""
        if ep:
            ip_only = ep.split(":")[0]
            if ip_only and ip_only != last_ip:
                await db.set_last_ip(profile["id"], ip_only)
                last_ip = ip_only

    if user_banned:
        acct_status = "🚫 <b>Аккаунт заблокирован</b>"
    elif disabled:
        acct_status = "⏸ <b>Профиль отключён администратором</b>"
    else:
        acct_status = "✅ Активен"

    if peer:
        online = peer.get("online", False)
        ps = peer.get("status", "active")
        hs = fmt_handshake(peer.get("lastHandshake", 0))
        tr = peer.get("traffic", {})
        rx = fmt_bytes(float(tr.get("received", 0) or 0))
        tx = fmt_bytes(float(tr.get("sent", 0) or 0))
        proto = peer.get("protocol") or "—"

        cs = (
            "🚫 Отключён" if ps == "disabled"
            else ("🟢 Подключён" if online else "🔴 Не подключён")
        )
        conn_block = (
            f"\n\n<b>📡 Подключение</b>\n"
            f"Статус: {cs}\n"
            f"Протокол: <code>{html.escape(proto)}</code>\n"
            f"Последнее подключение: {hs}\n"
            f"⬇️ Получено: <b>{rx}</b>\n"
            f"⬆️ Отправлено: <b>{tx}</b>"
        )
    else:
        conn_block = "\n\n<i>📡 Данные подключения недоступны</i>"

    ip_line = f"🌐 Последний IP: <code>{html.escape(last_ip)}</code>\n" if admin else ""

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Конфиг этого профиля",
                              callback_data=f"get_config_profile:{profile_id}")],
        [InlineKeyboardButton(text="◀️ К списку профилей", callback_data="my_profiles")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")],
    ])

    await safe_edit(
        callback.message,
        f"👤 <b>Профиль: {html.escape(vpn_name)}</b>\n\n"
        f"Статус: {acct_status}\n"
        f"Создан: {created_at}\n"
        f"{ip_line}"
        f"Key ID: <code>{html.escape(peer_id)}</code>"
        f"{conn_block}",
        reply_markup=kb,
    )


# ──────────────────────────── Удаление профиля пользователем ───────

async def cb_user_del_profile(callback: CallbackQuery, db: Database):
    """Запрос подтверждения удаления профиля самим пользователем."""
    uid = callback.from_user.id
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    profile_id = int(parts[1])
    profile = await db.get_profile_by_id(profile_id)
    if not profile or profile["telegram_id"] != uid:
        await callback.answer("❌ Профиль не найден.", show_alert=True)
        return

    vpn_name = profile["vpn_name"]
    await safe_edit(
        callback.message,
        f"🗑 <b>Удалить профиль?</b>\n\n"
        f"Профиль <b>{html.escape(vpn_name)}</b> будет удалён безвозвратно.\n\n"
        f"⚠️ Это действие нельзя отменить.",
        reply_markup=kb_user_del_confirm(profile_id, vpn_name),
    )
    await callback.answer()


async def cb_user_del_profile_do(callback: CallbackQuery, db: Database,
                                  amnezia: AmneziaClient):
    """Подтверждённое удаление профиля самим пользователем."""
    uid = callback.from_user.id
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    profile_id = int(parts[1])
    profile = await db.get_profile_by_id(profile_id)
    if not profile or profile["telegram_id"] != uid:
        await callback.answer("❌ Профиль не найден.", show_alert=True)
        return

    vpn_name = profile["vpn_name"]
    await safe_edit(callback.message, f"⏳ Удаляю профиль <b>{html.escape(vpn_name)}</b>…")
    await callback.answer()

    peer_id = profile.get("peer_id")
    if peer_id:
        await amnezia.delete_user(peer_id)

    await db.delete_profile(profile_id)

    # Уведомляем администраторов
    uname = callback.from_user.username or ""
    safe_u = html.escape(uname)
    tg_ref = f"@{safe_u}" if safe_u else html.escape(callback.from_user.first_name or "")
    msg = (
        f"🗑 <b>Профиль удалён</b>\n\n"
        f"👤 {tg_ref} (ID: <code>{uid}</code>)\n"
        f"🔑 Имя: <b>{html.escape(vpn_name)}</b>"
    )
    for aid in settings.ADMIN_IDS:
        try:
            await callback.bot.send_message(aid, msg, parse_mode="HTML")
        except Exception:
            pass

    user_data = await db.get_user(uid)
    admin = is_admin(uid)
    has_profiles = bool(user_data and user_data.get("profiles"))
    can_create = await db.can_create_profile(uid)

    await safe_edit(
        callback.message,
        menu_text(user_data, f"✅ Профиль «{vpn_name}» удалён"),
        reply_markup=kb_main(has_profiles=has_profiles, can_create=can_create, admin=admin),
    )


# ──────────────────────────── Статус сервера ────────────────────────

async def cb_server_status(callback: CallbackQuery, amnezia: AmneziaClient):
    await safe_edit(callback.message, "⏳ Запрашиваю данные сервера…")
    await callback.answer()

    info = await amnezia.get_server_info()

    if not info:
        await safe_edit(
            callback.message,
            "❌ <b>Сервер недоступен</b>",
            reply_markup=kb_server_status(),
        )
        return

    region = info.get("region") or info.get("serverRegion") or "—"
    pr = info.get("protocols") or info.get("protocolsEnabled") or []
    if isinstance(pr, str):
        pr = [pr]
    proto = ", ".join(pr) if pr else "—"
    cnt = info.get("peersCount") or info.get("totalPeers") or info.get("clientsCount") or "—"
    mx = info.get("maxPeers") or info.get("serverMaxPeers") or "—"

    await safe_edit(
        callback.message,
        f"🖥 <b>Статус сервера</b>\n\n"
        f"🌍 Регион: <code>{html.escape(str(region))}</code>\n"
        f"🔌 Протоколы: <code>{html.escape(str(proto))}</code>\n"
        f"👥 Клиентов: <b>{cnt}</b> / {mx}",
        reply_markup=kb_server_status(),
    )


# ──────────────────────────── Команды бота ──────────────────────────

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="menu",  description="🏠 Открыть меню"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())


# ──────────────────────────── main ──────────────────────────────────

async def main():
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())
    db = Database(settings.DB_PATH, settings.DB_ENCRYPTION_KEY)
    await db.init()

    amnezia = AmneziaClient(
        settings.AMNEZIA_API_URL,
        settings.AMNEZIA_API_KEY,
        settings.AMNEZIA_PROTOCOL,
    )

    di = DIMiddleware(db, amnezia)
    throttle = ThrottlingMiddleware(0.7)
    banned_mw = BannedUserMiddleware()

    dp.message.middleware(di)
    dp.callback_query.middleware(di)
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    dp.message.middleware(banned_mw)
    dp.callback_query.middleware(banned_mw)

    # Команды
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_menu,  Command("menu"))

    # FSM
    dp.message.register(process_vpn_name, CreateUserStates.waiting_for_name, F.text)

    # Catch-all
    dp.message.register(catch_all_messages)

    # Callbacks
    dp.callback_query.register(cb_back_main,          F.data == "back_main")
    dp.callback_query.register(cb_cancel,              F.data == "cancel")
    dp.callback_query.register(cb_noop,                F.data == "noop")
    dp.callback_query.register(cb_create_vpn,          F.data == "create_vpn")
    dp.callback_query.register(cb_confirm_create,      F.data.startswith("confirm_create:"))
    dp.callback_query.register(cb_get_config_profile,  F.data.startswith("get_config_profile:"))
    dp.callback_query.register(cb_my_profiles,         F.data == "my_profiles")
    dp.callback_query.register(cb_my_info_profile,     F.data.startswith("my_info_profile:"))
    dp.callback_query.register(cb_user_del_profile,    F.data.startswith("user_del_profile:") & ~F.data.startswith("user_del_profile_do:"))
    dp.callback_query.register(cb_user_del_profile_do, F.data.startswith("user_del_profile_do:"))
    dp.callback_query.register(cb_server_status,       F.data == "server_status")

    register_admin_handlers(dp)

    async def on_startup():
        await set_bot_commands(bot)
        logger.info(
            "Бот запущен. Режим: %s  Администраторы: %s",
            settings.BOT_MODE, settings.ADMIN_IDS,
        )

    async def on_shutdown():
        await db.close()
        await amnezia.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())