#!/usr/bin/env python3
"""
Telegram‑бот для управления заказами интернет‑магазина (PostgreSQL).
Возможности:
- Просмотр списка заказов (с пагинацией).
- Просмотр деталей заказа (товары, контактные данные).
- Изменение статуса заказа.
- Удаление заказа.
- Автоматическое оповещение администраторов о новых заказах.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

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

# ------------------------- Конфигурация -------------------------
def read_token():
    with open("token.txt", "r") as f:
        return f.read().strip()

def read_db_config():
    config = {}
    with open("config_db.txt", "r") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                config[k] = v
    return config

TOKEN = read_token()
DB_CONFIG = read_db_config()

def load_admin_ids():
    try:
        with open("admin_ids.txt", "r") as f:
            return {int(line.strip()) for line in f if line.strip().isdigit()}
    except FileNotFoundError:
        logging.warning("admin_ids.txt не найден – бот доступен всем (небезопасно!)")
        return None

ADMIN_IDS = load_admin_ids()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния разговоров
STATUS_CHANGE, DELETE_CONFIRM = range(2)

# Список возможных статусов (в порядке, удобном для отображения)
STATUSES = [
    "Ожидает подтверждения",
    "ожидает оплаты",
    "в сборке",
    "в пути",
    "доставлен"
]

# Путь к папке для временных данных (необязательно)
BASE_DIR = Path(__file__).parent

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

# ------------------------- Работа с базой данных -------------------------
class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_orders(self, offset: int = 0, limit: int = 10) -> Tuple[List[dict], int]:
        """Получить список заказов с пагинацией."""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM orders")
            rows = await conn.fetch(
                "SELECT id, user_name, phone, order_date, status FROM orders "
                "ORDER BY order_date DESC OFFSET $1 LIMIT $2",
                offset, limit
            )
            orders = []
            for r in rows:
                orders.append({
                    "id": r["id"],
                    "user_name": r["user_name"],
                    "phone": r["phone"],
                    "order_date": r["order_date"].isoformat() if r["order_date"] else None,
                    "status": r["status"]
                })
            return orders, total

    async def get_order_by_id(self, order_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, session_id, user_name, phone, email, address, comment, contact_time, "
                "order_date, status FROM orders WHERE id = $1", order_id
            )
            if not row:
                return None
            order = dict(row)
            order["order_date"] = order["order_date"].isoformat() if order["order_date"] else None
            return order

    async def get_order_items(self, order_id: int) -> List[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, product_code, product_name, material_code, material_name, cost, quantity "
                "FROM order_items WHERE order_id = $1 ORDER BY id", order_id
            )
            return [dict(r) for r in rows]

    async def update_order_status(self, order_id: int, new_status: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE orders SET status = $1 WHERE id = $2", new_status, order_id
            )
            return result != "UPDATE 0"

    async def delete_order(self, order_id: int) -> bool:
        async with self.pool.acquire() as conn:
            # каскадное удаление через ON DELETE CASCADE
            result = await conn.execute("DELETE FROM orders WHERE id = $1", order_id)
            return result != "DELETE 0"

    async def get_new_orders_since(self, since: datetime) -> List[dict]:
        """Получить заказы, созданные после указанного времени."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_name, phone, order_date, status FROM orders "
                "WHERE order_date > $1 ORDER BY order_date ASC", since
            )
            return [dict(r) for r in rows]


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

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("📋 Список заказов", callback_data="orders_list")],
        [InlineKeyboardButton("🔄 Проверить новые заказы", callback_data="check_new_orders")],
    ]
    await send_or_edit_message(update, context, "🔧 *Главное меню управления заказами*",
                               InlineKeyboardMarkup(keyboard), new_message)

async def show_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Отобразить список заказов с пагинацией."""
    limit = 8
    offset = page * limit
    orders, total = await db.get_orders(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        orders, total = await db.get_orders(offset, limit)

    text = "*Список заказов*\n\n"
    if not orders:
        text += "Нет заказов."
    else:
        for o in orders:
            text += f"`#{o['id']}` – {o['user_name']}, {o['phone']}\n"
            text += f"   📅 {o['order_date']}\n"
            text += f"   🏷️ Статус: {o['status']}\n\n"
    text += f"Страница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for o in orders:
        keyboard.append([
            InlineKeyboardButton(f"📄 Заказ #{o['id']}", callback_data=f"order_details_{o['id']}_{page}"),
            InlineKeyboardButton("✏️ Статус", callback_data=f"order_status_{o['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"order_delete_{o['id']}_{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"orders_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"orders_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔄 Проверить новые заказы", callback_data="check_new_orders")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    context.user_data["orders_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def orders_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_orders_list(update, context, page)

async def order_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, order_id, page = query.data.split("_")
    order_id = int(order_id)
    page = int(page)

    order = await db.get_order_by_id(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    items = await db.get_order_items(order_id)
    items_text = ""
    total_sum = 0
    for it in items:
        item_total = it['cost'] * it['quantity']
        total_sum += item_total
        items_text += f"• {it['product_name']} ({it['material_name']}) – {it['quantity']} шт. × {it['cost']}₽ = {item_total}₽\n"

    text = (
        f"*Заказ #{order_id}*\n\n"
        f"👤 *Клиент:* {order['user_name']}\n"
        f"📞 *Телефон:* {order['phone']}\n"
        f"✉️ *Email:* {order['email'] or '—'}\n"
        f"🏠 *Адрес:* {order['address'] or '—'}\n"
        f"💬 *Комментарий:* {order['comment'] or '—'}\n"
        f"⏰ *Удобное время:* {order['contact_time'] or '—'}\n"
        f"📅 *Дата заказа:* {order['order_date']}\n"
        f"🏷️ *Статус:* {order['status']}\n\n"
        f"*Товары:*\n{items_text}\n"
        f"*Итого:* {total_sum}₽"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить статус", callback_data=f"order_status_{order_id}_{page}")],
        [InlineKeyboardButton("🗑️ Удалить заказ", callback_data=f"order_delete_{order_id}_{page}")],
        [InlineKeyboardButton("◀ К списку заказов", callback_data=f"orders_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def change_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, order_id, page = query.data.split("_")
    order_id = int(order_id)
    page = int(page)
    context.user_data["status_order_id"] = order_id
    context.user_data["status_page"] = page

    keyboard = []
    for status in STATUSES:
        keyboard.append([InlineKeyboardButton(status, callback_data=f"status_set_{status}")])
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"orders_page_{page}")])
    await query.edit_message_text("Выберите новый статус заказа:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STATUS_CHANGE

async def change_status_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_status = query.data.split("_", 2)[-1]  # формат "status_set_новый статус"
    order_id = context.user_data["status_order_id"]
    page = context.user_data["status_page"]
    success = await db.update_order_status(order_id, new_status)
    if success:
        await query.edit_message_text(f"✅ Статус заказа #{order_id} изменён на «{new_status}».")
    else:
        await query.edit_message_text(f"❌ Не удалось изменить статус заказа #{order_id}.")
    await show_orders_list(update, context, page)
    return ConversationHandler.END

async def delete_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, order_id, page = query.data.split("_")
    order_id = int(order_id)
    page = int(page)
    context.user_data["del_order_id"] = order_id
    context.user_data["del_page"] = page
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="order_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"orders_page_{page}")],
    ]
    await query.edit_message_text(f"⚠️ Удалить заказ #{order_id} навсегда?",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_CONFIRM

async def delete_order_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data["del_order_id"]
    page = context.user_data["del_page"]
    success = await db.delete_order(order_id)
    if success:
        await query.edit_message_text(f"✅ Заказ #{order_id} удалён.")
    else:
        await query.edit_message_text(f"❌ Не удалось удалить заказ #{order_id}.")
    await show_orders_list(update, context, page)
    return ConversationHandler.END

async def check_new_orders_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Проверить новые заказы'."""
    query = update.callback_query
    await query.answer()
    last_check = context.bot_data.get("last_order_check", datetime.min)
    new_orders = await db.get_new_orders_since(last_check)
    if not new_orders:
        await query.edit_message_text("Новых заказов не обнаружено.")
        await show_main_menu(update, context, new_message=True)
        return
    # Обновляем время последней проверки
    context.bot_data["last_order_check"] = datetime.now()
    for order in new_orders:
        await notify_new_order(context.bot, order)
    await query.edit_message_text(f"✅ Отправлено уведомлений о {len(new_orders)} новых заказах.")
    await show_main_menu(update, context, new_message=True)

# ------------------------- Фоновая задача оповещения -------------------------
async def notify_new_order(bot, order: dict):
    """Отправить уведомление о новом заказе всем администраторам."""
    if ADMIN_IDS is None:
        # Если нет списка админов, уведомляем только владельца? Но лучше не отправлять никому.
        logger.warning("Нет списка администраторов, уведомление не отправлено.")
        return
    text = (
        f"🆕 *Новый заказ!*\n"
        f"Номер: #{order['id']}\n"
        f"Клиент: {order['user_name']}\n"
        f"Телефон: {order['phone']}\n"
        f"Дата: {order['order_date'].isoformat() if order['order_date'] else '—'}\n"
        f"Статус: {order['status']}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Посмотреть заказ", callback_data=f"order_details_{order['id']}_0")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

async def periodic_new_orders_check(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача, запускаемая каждые 10 секунд."""
    last_check = context.bot_data.get("last_order_check", datetime.min)
    new_orders = await db.get_new_orders_since(last_check)
    if new_orders:
        context.bot_data["last_order_check"] = datetime.now()
        for order in new_orders:
            await notify_new_order(context.bot, order)

# ------------------------- Обработчики меню и отмена -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)

@admin_only
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

@admin_only
async def orders_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_orders_list(update, context, 0)

@admin_only
async def check_new_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_new_orders_manual(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.edit_message_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    else:
        await update.message.reply_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END

# ------------------------- Регистрация обработчиков -------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(orders_list_callback, pattern="^orders_list$"))
    app.add_handler(CallbackQueryHandler(check_new_orders_callback, pattern="^check_new_orders$"))
    app.add_handler(CallbackQueryHandler(orders_page_callback, pattern="^orders_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(order_details, pattern="^order_details_\\d+_\\d+$"))

    # Изменение статуса
    status_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(change_status_start, pattern="^order_status_\\d+_\\d+$")],
        states={
            STATUS_CHANGE: [CallbackQueryHandler(change_status_set, pattern="^status_set_")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(status_conv)

    # Удаление заказа
    delete_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_order_start, pattern="^order_delete_\\d+_\\d+$")],
        states={
            DELETE_CONFIRM: [CallbackQueryHandler(delete_order_execute, pattern="^order_del_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_conv)

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

    # Запускаем фоновую задачу для проверки новых заказов (раз в 10 секунд)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(periodic_new_orders_check, interval=10, first=5)

    await application.initialize()
    await application.start()
    logging.info("Бот для управления заказами запущен.")
    await application.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())