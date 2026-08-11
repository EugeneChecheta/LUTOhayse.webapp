#!/usr/bin/env python3
"""
Telegram‑бот для управления аккаунтами клиентов (таблица users).
Использует те же конфиги, что и основной бот.
Версия 1.0
"""
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ------------------------- Пути к файлам -------------------------
BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token_clients.txt"
CONFIG_DB_FILE = BASE_DIR / "config_db.txt"
ADMIN_FILE = BASE_DIR / "admin_ids.txt"

# ------------------------- Чтение конфигураций -------------------------
def read_token():
    if not TOKEN_FILE.exists():
        logging.error(f"Файл с токеном не найден: {TOKEN_FILE}")
        sys.exit(1)
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
            if not token:
                logging.error("Токен пустой")
                sys.exit(1)
            return token
    except Exception as e:
        logging.error(f"Ошибка чтения token.txt: {e}")
        sys.exit(1)

def read_db_config():
    if not CONFIG_DB_FILE.exists():
        logging.error(f"Файл конфигурации БД не найден: {CONFIG_DB_FILE}")
        sys.exit(1)
    config = {}
    try:
        with open(CONFIG_DB_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k] = v
        required = ["host", "port", "database", "user", "password"]
        for key in required:
            if key not in config:
                logging.error(f"В config_db.txt отсутствует параметр {key}")
                sys.exit(1)
        return config
    except Exception as e:
        logging.error(f"Ошибка чтения config_db.txt: {e}")
        sys.exit(1)

def read_admin_ids():
    if not ADMIN_FILE.exists():
        logging.warning(f"Файл admin_ids.txt не найден: {ADMIN_FILE}")
        return []
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            ids = []
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    ids.append(int(line))
            return ids
    except Exception as e:
        logging.error(f"Ошибка чтения admin_ids.txt: {e}")
        return []

# ------------------------- Инициализация конфигураций -------------------------
TOKEN = read_token()
DB_CONFIG = read_db_config()
ADMIN_IDS = read_admin_ids()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния разговоров
(
    MAIN_MENU,
    USER_LIST,
    USER_DETAIL,
    USER_EDIT_SELECT,
    USER_EDIT_FIELD,
    USER_EDIT_CONFIRM,
    USER_DELETE_CONFIRM,
    SEARCH_PHONE,
) = range(8)

# ------------------------- Декоратор проверки администратора -------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ У вас нет прав.")
                await update.callback_query.edit_message_text("⛔ Доступ запрещён.")
            else:
                await update.message.reply_text("⛔ У вас нет прав на использование этого бота.")
            return None
        return await func(update, context)
    return wrapper

# ------------------------- Работа с базой данных -------------------------
class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---------- Пользователи ----------
    async def get_users(self, offset: int = 0, limit: int = 10) -> tuple[List[Dict[str, Any]], int]:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users")
            rows = await conn.fetch(
                "SELECT id, phone, full_name, email, address, contact_time, created_at "
                "FROM users ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, phone, full_name, email, address, contact_time, created_at "
                "FROM users WHERE id = $1",
                user_id
            )
            return dict(row) if row else None

    async def get_user_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, phone, full_name, email, address, contact_time, created_at "
                "FROM users WHERE phone = $1",
                phone
            )
            return dict(row) if row else None

    async def update_user(self, user_id: int, field: str, value: str) -> bool:
        allowed_fields = {"full_name", "email", "address", "contact_time", "phone"}
        if field not in allowed_fields:
            return False
        async with self.pool.acquire() as conn:
            # Проверка уникальности телефона, если меняем phone
            if field == "phone":
                existing = await conn.fetchval(
                    "SELECT id FROM users WHERE phone = $1 AND id != $2",
                    value, user_id
                )
                if existing:
                    return False
            await conn.execute(
                f"UPDATE users SET {field} = $1 WHERE id = $2",
                value, user_id
            )
            return True

    async def delete_user(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
            return result != "DELETE 0"

db_pool = None
db = None

# ------------------------- Вспомогательные функции -------------------------
CANCEL_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="main_menu")]])

async def send_or_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, keyboard: InlineKeyboardMarkup,
                                new_message: bool = False):
    if update.callback_query and not new_message:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            await update.callback_query.answer()
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                   reply_markup=keyboard, parse_mode="Markdown")

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)

@admin_only
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("👥 Список пользователей", callback_data="users_list")],
        [InlineKeyboardButton("🔍 Поиск по телефону", callback_data="users_search")],
    ]
    text = "👤 *Управление аккаунтами клиентов*\nВыберите действие:"
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message)

# ------------------------- Список пользователей с пагинацией -------------------------
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    await query.answer()
    limit = 8
    offset = page * limit
    users, total = await db.get_users(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        users, total = await db.get_users(offset, limit)

    text = f"*Список пользователей* (стр. {page+1}/{max(1, total_pages)})\n\n"
    if not users:
        text += "Нет пользователей."
    else:
        for u in users:
            text += f"• {u['full_name'] or 'Без имени'} – `{u['phone']}`\n"

    keyboard = []
    for u in users:
        keyboard.append([
            InlineKeyboardButton(f"👤 {u['full_name'] or u['phone']}", callback_data=f"user_detail_{u['id']}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"users_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"users_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    context.user_data["users_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def users_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await users_list(update, context, page)

# ------------------------- Детали пользователя -------------------------
async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split("_")[-1])
    user = await db.get_user_by_id(user_id)
    if not user:
        await query.edit_message_text("❌ Пользователь не найден.")
        return

    text = (
        f"*Информация о пользователе*\n\n"
        f"🆔 ID: {user['id']}\n"
        f"📞 Телефон: `{user['phone']}`\n"
        f"👤 Имя: {user['full_name'] or '—'}\n"
        f"📧 Email: {user['email'] or '—'}\n"
        f"🏠 Адрес: {user['address'] or '—'}\n"
        f"🕒 Удобное время: {user['contact_time'] or '—'}\n"
        f"📅 Дата регистрации: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else '—'}"
    )

    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"user_edit_{user_id}")],
        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"user_delete_confirm_{user_id}")],
        [InlineKeyboardButton("◀ К списку", callback_data="users_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ------------------------- Редактирование пользователя -------------------------
async def user_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split("_")[-1])
    context.user_data["edit_user_id"] = user_id
    keyboard = [
        [InlineKeyboardButton("👤 Имя (full_name)", callback_data="user_edit_field_full_name")],
        [InlineKeyboardButton("📧 Email", callback_data="user_edit_field_email")],
        [InlineKeyboardButton("🏠 Адрес", callback_data="user_edit_field_address")],
        [InlineKeyboardButton("🕒 Удобное время", callback_data="user_edit_field_contact_time")],
        [InlineKeyboardButton("📞 Телефон", callback_data="user_edit_field_phone")],
        [InlineKeyboardButton("◀ Отмена", callback_data=f"user_detail_{user_id}")],
    ]
    await query.edit_message_text("Выберите поле для редактирования:", reply_markup=InlineKeyboardMarkup(keyboard))

async def user_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("_")[-1]  # full_name, email, address, contact_time, phone
    context.user_data["edit_field"] = field
    field_names = {
        "full_name": "новое имя",
        "email": "новый email",
        "address": "новый адрес",
        "contact_time": "новое удобное время",
        "phone": "новый номер телефона"
    }
    await query.edit_message_text(
        f"Введите {field_names.get(field, field)}:",
        reply_markup=CANCEL_KEYBOARD
    )
    return USER_EDIT_FIELD

async def user_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    user_id = context.user_data["edit_user_id"]
    field = context.user_data["edit_field"]

    if field == "phone":
        # проверка уникальности
        existing = await db.get_user_by_phone(value)
        if existing and existing["id"] != user_id:
            await update.message.reply_text(
                "❌ Этот номер телефона уже занят. Введите другой:",
                reply_markup=CANCEL_KEYBOARD
            )
            return USER_EDIT_FIELD

    success = await db.update_user(user_id, field, value)
    if success:
        await update.message.reply_text("✅ Данные обновлены.")
    else:
        await update.message.reply_text("❌ Ошибка обновления. Возможно, поле недопустимо.")
    # Возвращаемся к деталям
    user = await db.get_user_by_id(user_id)
    if user:
        await user_detail_from_message(update, context, user_id)
    else:
        await update.message.reply_text("❌ Пользователь не найден.")
        await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END

async def user_detail_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    # Вспомогательная функция для отображения деталей после редактирования
    user = await db.get_user_by_id(user_id)
    if not user:
        await update.message.reply_text("❌ Пользователь не найден.")
        return
    text = (
        f"*Информация о пользователе*\n\n"
        f"🆔 ID: {user['id']}\n"
        f"📞 Телефон: `{user['phone']}`\n"
        f"👤 Имя: {user['full_name'] or '—'}\n"
        f"📧 Email: {user['email'] or '—'}\n"
        f"🏠 Адрес: {user['address'] or '—'}\n"
        f"🕒 Удобное время: {user['contact_time'] or '—'}\n"
        f"📅 Дата регистрации: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else '—'}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"user_edit_{user_id}")],
        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"user_delete_confirm_{user_id}")],
        [InlineKeyboardButton("◀ К списку", callback_data="users_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ------------------------- Удаление пользователя -------------------------
async def user_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split("_")[-1])
    context.user_data["delete_user_id"] = user_id
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="user_delete_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"user_detail_{user_id}")],
    ]
    await query.edit_message_text("⚠️ Удалить этого пользователя навсегда?", reply_markup=InlineKeyboardMarkup(keyboard))

async def user_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = context.user_data.get("delete_user_id")
    if not user_id:
        await query.edit_message_text("❌ Ошибка: ID пользователя не найден.")
        return
    success = await db.delete_user(user_id)
    if success:
        await query.edit_message_text("✅ Пользователь удалён.")
    else:
        await query.edit_message_text("❌ Пользователь не найден.")
    await show_main_menu(update, context, new_message=True)

# ------------------------- Поиск по телефону -------------------------
async def user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📞 Введите номер телефона (в том же формате, как в базе):", reply_markup=CANCEL_KEYBOARD)
    return SEARCH_PHONE

async def user_search_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user = await db.get_user_by_phone(phone)
    if not user:
        await update.message.reply_text("❌ Пользователь с таким номером не найден.")
        await show_main_menu(update, context, new_message=True)
        return ConversationHandler.END

    # Отображаем детали
    text = (
        f"*Найден пользователь*\n\n"
        f"🆔 ID: {user['id']}\n"
        f"📞 Телефон: `{user['phone']}`\n"
        f"👤 Имя: {user['full_name'] or '—'}\n"
        f"📧 Email: {user['email'] or '—'}\n"
        f"🏠 Адрес: {user['address'] or '—'}\n"
        f"🕒 Удобное время: {user['contact_time'] or '—'}\n"
        f"📅 Дата регистрации: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else '—'}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"user_edit_{user['id']}")],
        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"user_delete_confirm_{user['id']}")],
        [InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END

# ------------------------- Обработчики меню -------------------------
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END

# ------------------------- Регистрация обработчиков -------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(users_list, pattern="^users_list$"))
    app.add_handler(CallbackQueryHandler(users_page_callback, pattern="^users_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(user_detail, pattern="^user_detail_\\d+$"))

    # Редактирование
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(user_edit_select, pattern="^user_edit_\\d+$")],
        states={
            USER_EDIT_SELECT: [
                CallbackQueryHandler(user_edit_field, pattern="^user_edit_field_"),
                CallbackQueryHandler(cancel, pattern="^user_detail_\\d+$"),
            ],
            USER_EDIT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_edit_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_conv)

    # Удаление
    delete_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(user_delete_confirm, pattern="^user_delete_confirm_\\d+$")],
        states={
            USER_DELETE_CONFIRM: [
                CallbackQueryHandler(user_delete_execute, pattern="^user_delete_yes$"),
                CallbackQueryHandler(cancel, pattern="^user_detail_\\d+$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_conv)

    # Поиск
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(user_search_start, pattern="^users_search$")],
        states={
            SEARCH_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_search_phone)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(search_conv)

# ------------------------- Запуск бота -------------------------
async def main():
    global db_pool, db
    db_pool = await asyncpg.create_pool(
        host=DB_CONFIG.get("host", "localhost"),
        port=DB_CONFIG.get("port", "5432"),
        database=DB_CONFIG["database"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        min_size=1,
        max_size=5,
        timeout=60.0,
        command_timeout=60.0,
    )
    db = Database(db_pool)

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )
    application = Application.builder().token(TOKEN).request(request).build()
    register_handlers(application)

    await application.initialize()
    await application.start()
    logging.info("Бот управления пользователями запущен.")
    await application.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())