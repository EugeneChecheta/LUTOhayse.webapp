#!/usr/bin/env python3
"""
Telegram‑бот для управления аккаунтами клиентов (таблица users).
Работает с той же БД, что и основной бот.
Использует: admin_ids.txt, config_db.txt, token_clients.txt

Добавлено:
- Отображение хэша пароля в карточке клиента (хранится в том же формате, что и на сайте).
- Возможность редактирования пароля с использованием generate_password_hash из werkzeug.security
  для полной совместимости с сайтом (Flask).
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict

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
from werkzeug.security import generate_password_hash  # для хэширования паролей

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
        logging.error(f"Ошибка чтения token_clients.txt: {e}")
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

# ------------------------- Инициализация -------------------------
TOKEN = read_token()
DB_CONFIG = read_db_config()
ADMIN_IDS = read_admin_ids()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------- Состояния разговоров -------------------------
(
    MAIN_MENU,
    VIEW_CLIENTS,
    CLIENT_DETAILS,
    CLIENT_EDIT_SELECT,
    CLIENT_EDIT_FIELD,
    CLIENT_EDIT_PHONE,
    CLIENT_EDIT_PASSWORD,      # новое состояние для ввода пароля
    CLIENT_DELETE_CONFIRM,
    CLIENT_ORDERS,
    ORDER_DETAILS,
    SEARCH_PHONE_INPUT,
) = range(11)

CANCEL_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="main_menu")]])

# ------------------------- Класс для работы с БД (клиенты) -------------------------
class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---------- Пользователи ----------
    async def get_users(self, offset: int = 0, limit: int = 10) -> (List[Dict], int):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users")
            rows = await conn.fetch(
                "SELECT id, phone, full_name, email, address, contact_time, created_at, password_hash "
                "FROM users ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def search_users_by_phone(self, phone: str) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, phone, full_name, email, address, contact_time, created_at, password_hash "
                "FROM users WHERE phone ILIKE $1 ORDER BY id",
                f"%{phone}%"
            )
            return [dict(r) for r in rows]

    async def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, phone, full_name, email, address, contact_time, created_at, password_hash "
                "FROM users WHERE id = $1",
                user_id
            )
            return dict(row) if row else None

    async def update_user(self, user_id: int, **kwargs) -> bool:
        if not kwargs:
            return False
        fields = []
        values = []
        idx = 1
        for key, val in kwargs.items():
            # разрешённые поля для обновления
            if key not in ('full_name', 'email', 'address', 'contact_time', 'phone', 'password_hash'):
                continue
            fields.append(f"{key} = ${idx}")
            values.append(val)
            idx += 1
        if not fields:
            return False
        values.append(user_id)
        query = f"UPDATE users SET {', '.join(fields)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *values)
            return result != "UPDATE 0"

    async def delete_user(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
            return result != "DELETE 0"

    async def is_phone_unique(self, phone: str, exclude_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_id:
                row = await conn.fetchrow(
                    "SELECT id FROM users WHERE phone = $1 AND id != $2",
                    phone, exclude_id
                )
            else:
                row = await conn.fetchrow("SELECT id FROM users WHERE phone = $1", phone)
            return row is None

    # ---------- Заказы ----------
    async def get_orders_by_user(self, user_id: int) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, order_date, status, session_id, user_name, phone, email, address, comment, contact_time "
                "FROM orders WHERE user_id = $1 ORDER BY order_date DESC",
                user_id
            )
            return [dict(r) for r in rows]

    async def get_order_items(self, order_id: int) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, product_code, product_name, material_code, material_name, cost, quantity "
                "FROM order_items WHERE order_id = $1",
                order_id
            )
            return [dict(r) for r in rows]

    async def get_order_total(self, order_id: int) -> int:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(cost * quantity), 0) FROM order_items WHERE order_id = $1",
                order_id
            )
            return total

db_pool = None
db = None

# ------------------------- Декоратор проверки администратора -------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if ADMIN_IDS is not None and user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ У вас нет прав.")
                await update.callback_query.edit_message_text("⛔ Доступ запрещён.")
            else:
                await update.message.reply_text("⛔ У вас нет прав на использование этого бота.")
            return None
        return await func(update, context)
    return wrapper

# ------------------------- Вспомогательные функции -------------------------
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

# ------------------------- Главное меню -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("📋 Список клиентов", callback_data="clients_list")],
        [InlineKeyboardButton("🔍 Поиск по телефону", callback_data="search_phone")],
    ]
    await send_or_edit_message(update, context, "👤 *Управление аккаунтами клиентов*\nВыберите действие:",
                               InlineKeyboardMarkup(keyboard), new_message)

@admin_only
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# ------------------------- Список клиентов (пагинация) -------------------------
@admin_only
async def clients_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_clients(update, context, page=0)

async def show_clients(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    users, total = await db.get_users(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        users, total = await db.get_users(offset, limit)

    text = "*Список клиентов*\n\n"
    if not users:
        text += "Нет зарегистрированных клиентов."
    else:
        for u in users:
            phone = u['phone'] or 'не указан'
            name = u['full_name'] or 'не указано'
            text += f"🆔 {u['id']} | {phone} | {name}\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for u in users:
        keyboard.append([InlineKeyboardButton(
            f"{u['phone']} – {u['full_name'] or 'Без имени'}",
            callback_data=f"client_detail_{u['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"clients_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"clients_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔍 Поиск по телефону", callback_data="search_phone")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    context.user_data['clients_page'] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

@admin_only
async def clients_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_clients(update, context, page)

# ------------------------- Детали клиента -------------------------
@admin_only
async def client_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_id = int(query.data.split("_")[-1])
    await show_client_detail(update, context, client_id)

async def show_client_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, client_id: int):
    user = await db.get_user_by_id(client_id)
    if not user:
        await send_or_edit_message(update, context, "❌ Клиент не найден.",
                                   InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="clients_list")]]),
                                   new_message=False)
        return

    text = (f"*Клиент #{user['id']}*\n\n"
            f"📞 *Телефон:* {user['phone'] or '—'}\n"
            f"👤 *Имя:* {user['full_name'] or '—'}\n"
            f"📧 *Email:* {user['email'] or '—'}\n"
            f"🏠 *Адрес:* {user['address'] or '—'}\n"
            f"🕒 *Время связи:* {user['contact_time'] or '—'}\n"
            f"🔑 *Пароль (хэш):* `{user['password_hash'] or '—'}`\n"
            f"📅 *Дата регистрации:* {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else '—'}")

    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"client_edit_{client_id}")],
        [InlineKeyboardButton("🗑️ Удалить", callback_data=f"client_delete_confirm_{client_id}")],
        [InlineKeyboardButton("📦 Заказы", callback_data=f"client_orders_{client_id}")],
        [InlineKeyboardButton("◀ Назад к списку", callback_data="clients_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    context.user_data['current_client_id'] = client_id
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

# ------------------------- Поиск по телефону -------------------------
@admin_only
async def search_phone_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🔍 Введите номер телефона (или его часть) для поиска клиента:",
                                  reply_markup=CANCEL_KEYBOARD)
    return SEARCH_PHONE_INPUT

@admin_only
async def search_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("❌ Введите номер телефона.", reply_markup=CANCEL_KEYBOARD)
        return SEARCH_PHONE_INPUT

    users = await db.search_users_by_phone(phone)
    if not users:
        await update.message.reply_text("❌ Клиенты не найдены.", reply_markup=CANCEL_KEYBOARD)
        return ConversationHandler.END

    # Покажем список найденных
    text = "*Результаты поиска:*\n\n"
    for u in users:
        text += f"🆔 {u['id']} | {u['phone']} | {u['full_name'] or '—'}\n"

    keyboard = []
    for u in users:
        keyboard.append([InlineKeyboardButton(
            f"{u['phone']} – {u['full_name'] or 'Без имени'}",
            callback_data=f"client_detail_{u['id']}"
        )])
    keyboard.append([InlineKeyboardButton("🔍 Новый поиск", callback_data="search_phone")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END

@admin_only
async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END

# ------------------------- Редактирование клиента -------------------------
@admin_only
async def client_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_id = int(query.data.split("_")[-1])
    context.user_data['edit_client_id'] = client_id
    user = await db.get_user_by_id(client_id)
    if not user:
        await query.edit_message_text("❌ Клиент не найден.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("📞 Телефон", callback_data="edit_phone")],
        [InlineKeyboardButton("👤 Имя", callback_data="edit_full_name")],
        [InlineKeyboardButton("📧 Email", callback_data="edit_email")],
        [InlineKeyboardButton("🏠 Адрес", callback_data="edit_address")],
        [InlineKeyboardButton("🕒 Время связи", callback_data="edit_contact_time")],
        [InlineKeyboardButton("🔑 Пароль", callback_data="edit_password")],
        [InlineKeyboardButton("◀ Отмена", callback_data=f"client_detail_{client_id}")],
    ]
    await query.edit_message_text("Выберите поле для редактирования:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CLIENT_EDIT_SELECT

@admin_only
async def client_edit_field_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field = query.data.split("_")[1]  # например edit_phone -> phone
    context.user_data['edit_field'] = field
    field_names = {
        'phone': 'номер телефона',
        'full_name': 'имя',
        'email': 'email',
        'address': 'адрес',
        'contact_time': 'время связи',
        'password': 'пароль'
    }
    if field == 'phone':
        await query.edit_message_text(f"Введите новый номер телефона (должен быть уникальным):",
                                      reply_markup=CANCEL_KEYBOARD)
        return CLIENT_EDIT_PHONE
    elif field == 'password':
        await query.edit_message_text("Введите новый пароль (будет сохранён в хэшированном виде, совместимом с сайтом):",
                                      reply_markup=CANCEL_KEYBOARD)
        return CLIENT_EDIT_PASSWORD
    else:
        await query.edit_message_text(f"Введите новое значение для поля *{field_names.get(field, field)}*:",
                                      parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
        return CLIENT_EDIT_FIELD

@admin_only
async def client_edit_field_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    field = context.user_data.get('edit_field')
    client_id = context.user_data.get('edit_client_id')
    if not field or not client_id:
        await update.message.reply_text("❌ Ошибка: не выбрано поле для редактирования.")
        return ConversationHandler.END

    # Обновляем
    success = await db.update_user(client_id, **{field: value})
    if success:
        await update.message.reply_text("✅ Данные обновлены.")
    else:
        await update.message.reply_text("❌ Ошибка при обновлении данных (возможно, поле недопустимо).")
    await show_client_detail(update, context, client_id)
    return ConversationHandler.END

@admin_only
async def client_edit_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    client_id = context.user_data.get('edit_client_id')
    if not client_id:
        await update.message.reply_text("❌ Ошибка: клиент не определён.")
        return ConversationHandler.END

    # Проверка уникальности
    if not await db.is_phone_unique(phone, exclude_id=client_id):
        await update.message.reply_text("❌ Такой номер телефона уже занят другим клиентом. Введите другой:",
                                        reply_markup=CANCEL_KEYBOARD)
        return CLIENT_EDIT_PHONE

    success = await db.update_user(client_id, phone=phone)
    if success:
        await update.message.reply_text("✅ Номер телефона обновлён.")
    else:
        await update.message.reply_text("❌ Ошибка при обновлении номера.")
    await show_client_detail(update, context, client_id)
    return ConversationHandler.END

@admin_only
async def client_edit_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_password = update.message.text.strip()
    client_id = context.user_data.get('edit_client_id')
    if not client_id:
        await update.message.reply_text("❌ Ошибка: клиент не определён.")
        return ConversationHandler.END

    if not new_password:
        await update.message.reply_text("❌ Пароль не может быть пустым. Введите новый пароль:",
                                        reply_markup=CANCEL_KEYBOARD)
        return CLIENT_EDIT_PASSWORD

    # Хэшируем с помощью generate_password_hash для совместимости с сайтом
    hashed = generate_password_hash(new_password)
    success = await db.update_user(client_id, password_hash=hashed)
    if success:
        await update.message.reply_text("✅ Пароль обновлён.")
    else:
        await update.message.reply_text("❌ Ошибка при обновлении пароля.")
    await show_client_detail(update, context, client_id)
    return ConversationHandler.END

# ------------------------- Удаление клиента -------------------------
@admin_only
async def client_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_id = int(query.data.split("_")[-1])
    context.user_data['delete_client_id'] = client_id
    user = await db.get_user_by_id(client_id)
    if not user:
        await query.edit_message_text("❌ Клиент не найден.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="delete_yes")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data=f"client_detail_{client_id}")],
    ]
    await query.edit_message_text(f"⚠️ Удалить клиента *{user['phone']}* ({user['full_name'] or 'Без имени'})? Все заказы клиента останутся, но связь с пользователем будет удалена.",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CLIENT_DELETE_CONFIRM

@admin_only
async def client_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_id = context.user_data.get('delete_client_id')
    if not client_id:
        await query.edit_message_text("❌ Ошибка: клиент не определён.")
        return ConversationHandler.END

    success = await db.delete_user(client_id)
    if success:
        await query.edit_message_text("✅ Клиент удалён.")
    else:
        await query.edit_message_text("❌ Ошибка при удалении клиента.")
    await show_clients(update, context, 0)
    return ConversationHandler.END

# ------------------------- Заказы клиента -------------------------
@admin_only
async def client_orders_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    client_id = int(query.data.split("_")[-1])
    await show_client_orders(update, context, client_id)

async def show_client_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, client_id: int, page: int = 0):
    orders = await db.get_orders_by_user(client_id)
    if not orders:
        text = "У этого клиента нет заказов."
        keyboard = [[InlineKeyboardButton("◀ Назад к клиенту", callback_data=f"client_detail_{client_id}")]]
        await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)
        return

    # Пагинация по заказам
    PER_PAGE = 5
    total = len(orders)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
    start = page * PER_PAGE
    end = min(start + PER_PAGE, total)
    page_orders = orders[start:end]

    text = f"*Заказы клиента #{client_id}*\n\n"
    for o in page_orders:
        total_sum = await db.get_order_total(o['id'])
        text += (f"📦 Заказ #{o['id']}\n"
                 f"📅 {o['order_date'].strftime('%d.%m.%Y %H:%M') if o['order_date'] else '—'}\n"
                 f"Статус: {o['status']}\n"
                 f"Сумма: {total_sum} ₽\n\n")

    keyboard = []
    for o in page_orders:
        keyboard.append([InlineKeyboardButton(f"📄 Заказ #{o['id']}", callback_data=f"order_detail_{o['id']}_{client_id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"orders_page_{page-1}_{client_id}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"orders_page_{page+1}_{client_id}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀ Назад к клиенту", callback_data=f"client_detail_{client_id}")])

    context.user_data['orders_client_id'] = client_id
    context.user_data['orders_page'] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

@admin_only
async def orders_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    page = int(parts[3])
    client_id = int(parts[4])
    await show_client_orders(update, context, client_id, page)

# ------------------------- Детали заказа -------------------------
@admin_only
async def order_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    order_id = int(parts[3])
    client_id = int(parts[4])
    order = None
    orders = await db.get_orders_by_user(client_id)
    for o in orders:
        if o['id'] == order_id:
            order = o
            break
    if not order:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    items = await db.get_order_items(order_id)
    total = await db.get_order_total(order_id)

    text = (f"*Заказ #{order_id}*\n\n"
            f"📅 Дата: {order['order_date'].strftime('%d.%m.%Y %H:%M') if order['order_date'] else '—'}\n"
            f"Статус: {order['status']}\n"
            f"👤 Имя: {order['user_name']}\n"
            f"📞 Телефон: {order['phone']}\n"
            f"📧 Email: {order['email'] or '—'}\n"
            f"🏠 Адрес: {order['address'] or '—'}\n"
            f"📝 Комментарий: {order['comment'] or '—'}\n"
            f"🕒 Время связи: {order['contact_time'] or '—'}\n"
            f"💰 Итого: {total} ₽\n\n"
            f"*Товары:*\n")
    if not items:
        text += "Нет позиций."
    else:
        for it in items:
            text += f"• {it['product_name']} ({it['product_code']}) — {it['material_name']} ({it['material_code']}) — {it['quantity']} шт. × {it['cost']} ₽ = {it['cost'] * it['quantity']} ₽\n"

    keyboard = [
        [InlineKeyboardButton("◀ Назад к заказам", callback_data=f"client_orders_{client_id}")],
        [InlineKeyboardButton("◀ Назад к клиенту", callback_data=f"client_detail_{client_id}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# ------------------------- Регистрация обработчиков -------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))

    # Главное меню
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(clients_list_start, pattern="^clients_list$"))
    app.add_handler(CallbackQueryHandler(search_phone_start, pattern="^search_phone$"))

    # Список клиентов
    app.add_handler(CallbackQueryHandler(clients_page_callback, pattern="^clients_page_\\d+$"))

    # Детали клиента
    app.add_handler(CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"))

    # Поиск по телефону (Conversation)
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_phone_start, pattern="^search_phone$")],
        states={
            SEARCH_PHONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_phone_input),
                CallbackQueryHandler(search_cancel, pattern="^main_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel),
            CallbackQueryHandler(search_cancel, pattern="^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(search_conv)

    # Редактирование клиента (Conversation)
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(client_edit_start, pattern="^client_edit_\\d+$")],
        states={
            CLIENT_EDIT_SELECT: [
                CallbackQueryHandler(client_edit_field_select, pattern="^edit_"),
                CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
            ],
            CLIENT_EDIT_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, client_edit_field_input),
                CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
            ],
            CLIENT_EDIT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, client_edit_phone_input),
                CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
            ],
            CLIENT_EDIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, client_edit_password_input),
                CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel),
            CallbackQueryHandler(search_cancel, pattern="^main_menu$"),
            CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_conv)

    # Удаление клиента (Conversation)
    delete_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(client_delete_confirm, pattern="^client_delete_confirm_\\d+$")],
        states={
            CLIENT_DELETE_CONFIRM: [
                CallbackQueryHandler(client_delete_execute, pattern="^delete_yes$"),
                CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel),
            CallbackQueryHandler(search_cancel, pattern="^main_menu$"),
            CallbackQueryHandler(client_detail_callback, pattern="^client_detail_\\d+$"),
        ],
        per_message=False,
    )
    app.add_handler(delete_conv)

    # Заказы клиента
    app.add_handler(CallbackQueryHandler(client_orders_start, pattern="^client_orders_\\d+$"))
    app.add_handler(CallbackQueryHandler(orders_page_callback, pattern="^orders_page_\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(order_detail_callback, pattern="^order_detail_\\d+_\\d+$"))

# ------------------------- Запуск -------------------------
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
    logging.info("Бот для управления клиентами запущен.")
    await application.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())