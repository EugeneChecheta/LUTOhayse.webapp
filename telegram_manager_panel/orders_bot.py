#!/usr/bin/env python3
"""
Telegram‑бот для управления заказами интернет‑магазина (PostgreSQL).
Возможности:
- Просмотр списка заказов продуктов и матрацев (два раздела, с пагинацией).
- Просмотр деталей заказа (товары, слои матраца, контактные данные).
- Изменение статуса заказа.
- Удаление заказа.
- Просмотр других заказов того же клиента (по телефону).
- Автоматическое оповещение администраторов о новых заказах (продукты и матрацы).
"""

import asyncio
import html
import logging
from datetime import datetime, timedelta
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
BASE_DIR = Path(__file__).resolve().parent


def read_token() -> str:
    """Читает токен бота из файла token.txt в директории скрипта."""
    token_path = BASE_DIR / "token.txt"
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.error(f"Файл {token_path} не найден. Создайте его с токеном бота.")
        raise


def read_db_config() -> Dict[str, str]:
    """Читает параметры подключения к БД из config_db.txt."""
    config_path = BASE_DIR / "config_db.txt"
    config: Dict[str, str] = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k] = v
        required = ["database", "user", "password"]
        for key in required:
            if key not in config:
                raise ValueError(f"В {config_path} отсутствует обязательное поле {key}")
        config.setdefault("host", "localhost")
        config.setdefault("port", "5432")
        return config
    except FileNotFoundError:
        logging.error(f"Файл {config_path} не найден. Создайте его с параметрами БД.")
        raise


def load_admin_ids() -> Optional[set]:
    """Загружает множество ID администраторов из admin_ids.txt."""
    admins_path = BASE_DIR / "admin_ids.txt"
    try:
        with open(admins_path, "r", encoding="utf-8") as f:
            return {int(line.strip()) for line in f if line.strip().isdigit()}
    except FileNotFoundError:
        logging.warning(f"Файл {admins_path} не найден – бот доступен всем (небезопасно!)")
        return None


TOKEN = read_token()
DB_CONFIG = read_db_config()
ADMIN_IDS = load_admin_ids()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния разговоров
STATUS_CHANGE, DELETE_CONFIRM = range(2)

# Список возможных статусов
STATUSES = [
    "Ожидает подтверждения",
    "ожидает оплаты",
    "в сборке",
    "в пути",
    "доставлен",
]

PAGE_SIZE = 8


# ------------------------- Утилиты -------------------------
def esc(value) -> str:
    """HTML-экранирование пользовательских данных."""
    if value is None:
        return ""
    return html.escape(str(value))


# ------------------------- Декоратор проверки администратора -------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if ADMIN_IDS is not None and user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("⛔ У вас нет прав.")
                try:
                    await update.callback_query.edit_message_text("⛔ Доступ запрещён.")
                except Exception:
                    pass
            else:
                await update.message.reply_text("⛔ У вас нет прав на использование этого бота.")
            return None
        return await func(update, context)
    return wrapper


# ------------------------- Работа с базой данных -------------------------
class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---------- Продукты ----------
    async def get_product_orders(self, offset: int = 0, limit: int = PAGE_SIZE) -> Tuple[List[dict], int]:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM orders")
            rows = await conn.fetch(
                "SELECT id, user_name, phone, order_date, status FROM orders "
                "ORDER BY order_date DESC OFFSET $1 LIMIT $2",
                offset, limit,
            )
            orders = []
            for r in rows:
                orders.append({
                    "id": r["id"],
                    "user_name": r["user_name"],
                    "phone": r["phone"],
                    "order_date": r["order_date"].isoformat(sep=' ') if r["order_date"] else None,
                    "status": r["status"],
                })
            return orders, total

    async def get_product_order_by_id(self, order_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, session_id, user_id, user_name, phone, email, address, comment, "
                "contact_time, order_date, status FROM orders WHERE id = $1",
                order_id,
            )
            if not row:
                return None
            order = dict(row)
            order["order_date"] = order["order_date"].isoformat(sep=' ') if order["order_date"] else None
            return order

    async def get_product_order_items(self, order_id: int) -> List[dict]:
        async with self.pool.acquire() as conn:
            # Проверяем наличие колонки extra_data
            has_extra = await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name='order_items' AND column_name='extra_data'"
            )
            if has_extra:
                rows = await conn.fetch(
                    "SELECT id, product_code, product_name, material_code, material_name, "
                    "cost, quantity, extra_data FROM order_items "
                    "WHERE order_id = $1 ORDER BY id",
                    order_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, product_code, product_name, material_code, material_name, "
                    "cost, quantity, NULL::text AS extra_data FROM order_items "
                    "WHERE order_id = $1 ORDER BY id",
                    order_id,
                )
            return [dict(r) for r in rows]

    # ---------- Матрацы ----------
    async def get_mattress_orders(self, offset: int = 0, limit: int = PAGE_SIZE) -> Tuple[List[dict], int]:
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM mattress_orders")
            rows = await conn.fetch(
                """SELECT mo.id, mo.user_name, mo.phone, mo.order_date, mo.status,
                          ms.name AS size_name
                   FROM mattress_orders mo
                   LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
                   ORDER BY mo.order_date DESC OFFSET $1 LIMIT $2""",
                offset, limit,
            )
            orders = []
            for r in rows:
                orders.append({
                    "id": r["id"],
                    "user_name": r["user_name"],
                    "phone": r["phone"],
                    "order_date": r["order_date"].isoformat(sep=' ') if r["order_date"] else None,
                    "status": r["status"],
                    "size_name": r["size_name"] or "—",
                })
            return orders, total

    async def get_mattress_order_by_id(self, order_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT mo.id, mo.session_id, mo.user_id, mo.user_name, mo.phone, mo.email,
                          mo.address, mo.comment, mo.contact_time, mo.order_date, mo.status,
                          mo.size_id, mo.initial_height, mo.cover_id, mo.cover_price,
                          ms.name AS size_name,
                          mc.name AS cover_name, mc.code AS cover_code
                   FROM mattress_orders mo
                   LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
                   LEFT JOIN mattress_cover mc ON mo.cover_id = mc.id
                   WHERE mo.id = $1""",
                order_id,
            )
            if not row:
                return None
            order = dict(row)
            order["order_date"] = order["order_date"].isoformat(sep=' ') if order["order_date"] else None
            return order

    async def get_mattress_order_items(self, order_id: int) -> List[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, layer_id, layer_code, layer_name, quantity, price_per_unit, total_price "
                "FROM mattress_order_items WHERE order_id = $1 ORDER BY id",
                order_id,
            )
            return [dict(r) for r in rows]

    # ---------- Обновление статуса ----------
    async def update_product_order_status(self, order_id: int, new_status: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE orders SET status = $1 WHERE id = $2", new_status, order_id,
            )
            return result != "UPDATE 0"

    async def update_mattress_order_status(self, order_id: int, new_status: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE mattress_orders SET status = $1 WHERE id = $2", new_status, order_id,
            )
            return result != "UPDATE 0"

    # ---------- Удаление ----------
    async def delete_product_order(self, order_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM orders WHERE id = $1", order_id)
            return result != "DELETE 0"

    async def delete_mattress_order(self, order_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM mattress_orders WHERE id = $1", order_id)
            return result != "DELETE 0"

    # ---------- Новые заказы ----------
    async def get_new_product_orders_since(self, since: datetime) -> List[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, user_name, phone, order_date, status FROM orders "
                "WHERE order_date > $1 ORDER BY order_date ASC",
                since,
            )
            result = []
            for r in rows:
                d = dict(r)
                result.append(d)
            return result

    async def get_new_mattress_orders_since(self, since: datetime) -> List[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT mo.id, mo.user_name, mo.phone, mo.order_date, mo.status,
                          ms.name AS size_name
                   FROM mattress_orders mo
                   LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
                   WHERE mo.order_date > $1 ORDER BY mo.order_date ASC""",
                since,
            )
            return [dict(r) for r in rows]

    # ---------- Заказы клиента по телефону ----------
    async def get_other_orders_by_phone(self, phone: str) -> Tuple[List[dict], List[dict]]:
        async with self.pool.acquire() as conn:
            p_rows = await conn.fetch(
                "SELECT id, user_name, phone, order_date, status FROM orders "
                "WHERE phone = $1 ORDER BY order_date DESC",
                phone,
            )
            m_rows = await conn.fetch(
                """SELECT mo.id, mo.user_name, mo.phone, mo.order_date, mo.status,
                          ms.name AS size_name
                   FROM mattress_orders mo
                   LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
                   WHERE mo.phone = $1 ORDER BY mo.order_date DESC""",
                phone,
            )
            products = [dict(r) for r in p_rows]
            mattresses = [dict(r) for r in m_rows]
            return products, mattresses


db_pool = None
db: Optional[Database] = None


# ------------------------- Вспомогательные функции -------------------------
async def send_or_edit_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
    new_message: bool = False,
):
    if update.callback_query and not new_message:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="HTML",
            )
            await update.callback_query.answer()
            return
        except Exception as e:
            logger.debug(f"edit_message_text не удался: {e}")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("📦 Заказы продуктов", callback_data="orders_list")],
        [InlineKeyboardButton("🛏️ Заказы матрасов", callback_data="mattress_list")],
        [InlineKeyboardButton("🔄 Проверить новые заказы", callback_data="check_new_orders")],
    ]
    text = "🔧 <b>Главное меню управления заказами</b>"
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message)


# ------------------------- Список продуктов -------------------------
async def show_product_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    orders, total = await db.get_product_orders(page * PAGE_SIZE, PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        orders, total = await db.get_product_orders(page * PAGE_SIZE, PAGE_SIZE)

    text = "📦 <b>Заказы продуктов</b>\n\n"
    if not orders:
        text += "Нет заказов."
    else:
        for o in orders:
            text += f"<code>#{o['id']}</code> – {esc(o['user_name'])}, {esc(o['phone'])}\n"
            text += f"   📅 {esc(o['order_date'])}\n"
            text += f"   🏷️ Статус: {esc(o['status'])}\n\n"
    text += f"Страница {page + 1} из {max(1, total_pages)}"

    keyboard = []
    for o in orders:
        keyboard.append([
            InlineKeyboardButton(f"📄 #{o['id']}", callback_data=f"order_details_{o['id']}_{page}"),
            InlineKeyboardButton("✏️ Статус", callback_data=f"order_status_{o['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"order_delete_{o['id']}_{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"orders_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"orders_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    context.user_data["orders_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard))


# ------------------------- Список матрацев -------------------------
async def show_mattress_orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    orders, total = await db.get_mattress_orders(page * PAGE_SIZE, PAGE_SIZE)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        orders, total = await db.get_mattress_orders(page * PAGE_SIZE, PAGE_SIZE)

    text = "🛏️ <b>Заказы матрасов</b>\n\n"
    if not orders:
        text += "Нет заказов."
    else:
        for o in orders:
            text += f"<code>#{o['id']}</code> – {esc(o['user_name'])}, {esc(o['phone'])}\n"
            text += f"   📐 Размер: {esc(o['size_name'])}\n"
            text += f"   📅 {esc(o['order_date'])}\n"
            text += f"   🏷️ Статус: {esc(o['status'])}\n\n"
    text += f"Страница {page + 1} из {max(1, total_pages)}"

    keyboard = []
    for o in orders:
        keyboard.append([
            InlineKeyboardButton(f"📄 #{o['id']}", callback_data=f"mattress_details_{o['id']}_{page}"),
            InlineKeyboardButton("✏️ Статус", callback_data=f"mattress_status_{o['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"mattress_delete_{o['id']}_{page}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"mattress_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"mattress_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    context.user_data["mattress_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard))


# ------------------------- Детали заказа продукта -------------------------
async def product_order_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, order_id_str, page_str = query.data.split("_")
    order_id = int(order_id_str)
    page = int(page_str)

    order = await db.get_product_order_by_id(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден.")
        return

    items = await db.get_product_order_items(order_id)

    # Сохраняем телефон для функции «Другие заказы»
    context.user_data["other_orders_phone"] = order["phone"]
    context.user_data["other_orders_exclude_type"] = "product"
    context.user_data["other_orders_exclude_id"] = order_id
    context.user_data["other_orders_back_type"] = "product"
    context.user_data["other_orders_back_page"] = page

    items_text = ""
    total_sum = 0
    for it in items:
        item_total = it["cost"] * (it["quantity"] or 1)
        total_sum += item_total
        extra_raw = it.get("extra_data")
        is_topper = False
        extra: Dict = {}
        if extra_raw:
            try:
                import json
                extra = json.loads(extra_raw)
                is_topper = True
            except Exception:
                extra = {}

        if is_topper:
            size_name = extra.get("size_name", "—")
            layer_names = extra.get("layer_names", []) or []
            cover_name = extra.get("cover_name", "—")
            layers_str = ", ".join(layer_names) if layer_names else "—"
            items_text += (
                f"🛏️ <b>Топпер</b> ({esc(size_name)})\n"
                f"   Слои: {esc(layers_str)}\n"
                f"   Чехол: {esc(cover_name)}\n"
                f"   {it['quantity']} шт × {it['cost']}₽ = {item_total}₽\n\n"
            )
        else:
            items_text += (
                f"• {esc(it['product_name'])} "
                f"({esc(it['material_name'])}) – "
                f"{it['quantity']} шт × {it['cost']}₽ = {item_total}₽\n"
            )

    text = (
        f"📦 <b>Заказ продукта #{order_id}</b>\n\n"
        f"👤 <b>Клиент:</b> {esc(order['user_name'])}\n"
        f"📞 <b>Телефон:</b> {esc(order['phone'])}\n"
        f"✉️ <b>Email:</b> {esc(order['email']) or '—'}\n"
        f"🏠 <b>Адрес:</b> {esc(order['address']) or '—'}\n"
        f"💬 <b>Комментарий:</b> {esc(order['comment']) or '—'}\n"
        f"⏰ <b>Удобное время:</b> {esc(order['contact_time']) or '—'}\n"
        f"📅 <b>Дата заказа:</b> {esc(order['order_date'])}\n"
        f"🏷️ <b>Статус:</b> {esc(order['status'])}\n\n"
        f"<b>Товары:</b>\n{items_text}\n"
        f"💰 <b>Итого:</b> {total_sum}₽"
    )

    keyboard = [
        [InlineKeyboardButton("✏️ Изменить статус", callback_data=f"order_status_{order_id}_{page}")],
        [InlineKeyboardButton("🗑️ Удалить заказ", callback_data=f"order_delete_{order_id}_{page}")],
        [InlineKeyboardButton("👥 Другие заказы клиента", callback_data="other_orders")],
        [InlineKeyboardButton("◀ К списку продуктов", callback_data=f"orders_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ------------------------- Детали заказа матраца -------------------------
async def mattress_order_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, order_id_str, page_str = query.data.split("_")
    order_id = int(order_id_str)
    page = int(page_str)

    order = await db.get_mattress_order_by_id(order_id)
    if not order:
        await query.edit_message_text("❌ Заказ матраца не найден.")
        return

    items = await db.get_mattress_order_items(order_id)

    context.user_data["other_orders_phone"] = order["phone"]
    context.user_data["other_orders_exclude_type"] = "mattress"
    context.user_data["other_orders_exclude_id"] = order_id
    context.user_data["other_orders_back_type"] = "mattress"
    context.user_data["other_orders_back_page"] = page

    layers_text = ""
    layers_total = 0
    layers_height = 0
    for it in items:
        item_total = it["total_price"] or (it["price_per_unit"] * it["quantity"])
        layers_total += item_total
        layers_height += 5 * (it["quantity"] or 1)
        layers_text += (
            f"• {esc(it['layer_name'])} "
            f"({esc(it['layer_code'])}) – "
            f"{it['quantity']} шт × {it['price_per_unit']}₽ = {item_total}₽\n"
        )

    cover_price = order.get("cover_price", 0) or 0
    initial_height = order.get("initial_height", 0) or 0
    total_height = initial_height + layers_height
    grand_total = layers_total + cover_price

    text = (
        f"🛏️ <b>Заказ матраца #{order_id}</b>\n\n"
        f"👤 <b>Клиент:</b> {esc(order['user_name'])}\n"
        f"📞 <b>Телефон:</b> {esc(order['phone'])}\n"
        f"✉️ <b>Email:</b> {esc(order['email']) or '—'}\n"
        f"🏠 <b>Адрес:</b> {esc(order['address']) or '—'}\n"
        f"💬 <b>Комментарий:</b> {esc(order['comment']) or '—'}\n"
        f"⏰ <b>Удобное время:</b> {esc(order['contact_time']) or '—'}\n"
        f"📅 <b>Дата заказа:</b> {esc(order['order_date'])}\n"
        f"🏷️ <b>Статус:</b> {esc(order['status'])}\n\n"
        f"📐 <b>Размер:</b> {esc(order.get('size_name')) or '—'}\n"
        f"📏 <b>Изначальная высота:</b> {initial_height} см\n"
        f"🛏️ <b>Чехол:</b> {esc(order.get('cover_name')) or '—'} "
        f"({cover_price}₽)\n\n"
        f"<b>Слои:</b>\n{layers_text}\n"
        f"📊 <b>Высота слоёв:</b> {layers_height} см\n"
        f"📏 <b>Итоговая высота:</b> {total_height} см\n"
        f"💰 <b>Итого:</b> {grand_total}₽"
    )

    keyboard = [
        [InlineKeyboardButton("✏️ Изменить статус", callback_data=f"mattress_status_{order_id}_{page}")],
        [InlineKeyboardButton("🗑️ Удалить заказ", callback_data=f"mattress_delete_{order_id}_{page}")],
        [InlineKeyboardButton("👥 Другие заказы клиента", callback_data="other_orders")],
        [InlineKeyboardButton("◀ К списку матрасов", callback_data=f"mattress_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ------------------------- Другие заказы клиента -------------------------
async def other_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    phone = context.user_data.get("other_orders_phone")
    if not phone:
        await query.edit_message_text("❌ Не удалось определить клиента.")
        return

    exclude_type = context.user_data.get("other_orders_exclude_type")
    exclude_id = context.user_data.get("other_orders_exclude_id")
    back_type = context.user_data.get("other_orders_back_type", "product")
    back_page = context.user_data.get("other_orders_back_page", 0)

    products, mattresses = await db.get_other_orders_by_phone(phone)

    # Исключаем текущий заказ
    products = [o for o in products
                if not (exclude_type == "product" and o["id"] == exclude_id)]
    mattresses = [o for o in mattresses
                  if not (exclude_type == "mattress" and o["id"] == exclude_id)]

    text = f"👥 <b>Другие заказы клиента</b>\n📞 {esc(phone)}\n\n"
    if not products and not mattresses:
        text += "Других заказов не найдено."
    else:
        if products:
            text += "📦 <b>Продукты:</b>\n"
            for o in products:
                text += f"  • <code>#{o['id']}</code> – {esc(o['order_date'])} – {esc(o['status'])}\n"
            text += "\n"
        if mattresses:
            text += "🛏️ <b>Матрацы:</b>\n"
            for o in mattresses:
                text += f"  • <code>#{o['id']}</code> – {esc(o.get('size_name') or '—')} – {esc(o['order_date'])} – {esc(o['status'])}\n"

    keyboard = []
    for o in products[:8]:
        keyboard.append([InlineKeyboardButton(
            f"📦 Открыть продукт #{o['id']}",
            callback_data=f"order_details_{o['id']}_0",
        )])
    for o in mattresses[:8]:
        keyboard.append([InlineKeyboardButton(
            f"🛏️ Открыть матрац #{o['id']}",
            callback_data=f"mattress_details_{o['id']}_0",
        )])

    if back_type == "mattress":
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data=f"mattress_page_{back_page}")])
    else:
        keyboard.append([InlineKeyboardButton("◀ Назад", callback_data=f"orders_page_{back_page}")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ------------------------- Пагинация списков -------------------------
async def orders_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_product_orders_list(update, context, page)


async def mattress_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_mattress_orders_list(update, context, page)


# ------------------------- Изменение статуса -------------------------
async def change_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    # parts: ["order"|"mattress", "status", "<id>", "<page>"]
    order_type = "mattress" if parts[0] == "mattress" else "product"
    order_id = int(parts[2])
    page = int(parts[3])

    context.user_data["status_order_type"] = order_type
    context.user_data["status_order_id"] = order_id
    context.user_data["status_page"] = page

    keyboard = []
    for status in STATUSES:
        keyboard.append([InlineKeyboardButton(
            status, callback_data=f"status_set_{status}",
        )])
    if order_type == "mattress":
        back_cb = f"mattress_page_{page}"
    else:
        back_cb = f"orders_page_{page}"
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=back_cb)])
    await query.edit_message_text(
        f"Выберите новый статус для заказа #{order_id}:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return STATUS_CHANGE


async def change_status_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_status = query.data.split("_", 2)[-1]
    order_type = context.user_data.get("status_order_type", "product")
    order_id = context.user_data.get("status_order_id")
    page = context.user_data.get("status_page", 0)

    if order_type == "mattress":
        success = await db.update_mattress_order_status(order_id, new_status)
    else:
        success = await db.update_product_order_status(order_id, new_status)

    if success:
        await query.edit_message_text(
            f"✅ Статус заказа #{order_id} изменён на «{new_status}».",
        )
    else:
        await query.edit_message_text(
            f"❌ Не удалось изменить статус заказа #{order_id}.",
        )

    if order_type == "mattress":
        await show_mattress_orders_list(update, context, page)
    else:
        await show_product_orders_list(update, context, page)
    return ConversationHandler.END


# ------------------------- Удаление -------------------------
async def delete_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    # parts: ["order"|"mattress", "delete", "<id>", "<page>"]
    order_type = "mattress" if parts[0] == "mattress" else "product"
    order_id = int(parts[2])
    page = int(parts[3])

    context.user_data["del_order_type"] = order_type
    context.user_data["del_order_id"] = order_id
    context.user_data["del_page"] = page

    if order_type == "mattress":
        back_cb = f"mattress_page_{page}"
    else:
        back_cb = f"orders_page_{page}"

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="order_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=back_cb)],
    ]
    label = "матраца" if order_type == "mattress" else "продукта"
    await query.edit_message_text(
        f"⚠️ Удалить заказ {label} #{order_id} навсегда?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return DELETE_CONFIRM


async def delete_order_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_type = context.user_data.get("del_order_type", "product")
    order_id = context.user_data.get("del_order_id")
    page = context.user_data.get("del_page", 0)

    if order_type == "mattress":
        success = await db.delete_mattress_order(order_id)
    else:
        success = await db.delete_product_order(order_id)

    if success:
        await query.edit_message_text(f"✅ Заказ #{order_id} удалён.")
    else:
        await query.edit_message_text(f"❌ Не удалось удалить заказ #{order_id}.")

    if order_type == "mattress":
        await show_mattress_orders_list(update, context, page)
    else:
        await show_product_orders_list(update, context, page)
    return ConversationHandler.END


# ------------------------- Проверка новых заказов вручную -------------------------
async def check_new_orders_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    last_check = context.bot_data.get("last_order_check")
    if last_check is None:
        last_check = datetime.now() - timedelta(hours=1)

    new_products = await db.get_new_product_orders_since(last_check)
    new_mattresses = await db.get_new_mattress_orders_since(last_check)

    if not new_products and not new_mattresses:
        await query.edit_message_text("Новых заказов не обнаружено.")
        await show_main_menu(update, context, new_message=True)
        return

    context.bot_data["last_order_check"] = datetime.now()
    for order in new_products:
        await notify_new_product_order(context.bot, order)
    for order in new_mattresses:
        await notify_new_mattress_order(context.bot, order)

    total = len(new_products) + len(new_mattresses)
    await query.edit_message_text(f"✅ Отправлено уведомлений о {total} новых заказах.")
    await show_main_menu(update, context, new_message=True)


# ------------------------- Уведомления -------------------------
async def notify_new_product_order(bot, order: dict):
    if ADMIN_IDS is None:
        logger.warning("Нет списка администраторов, уведомление не отправлено.")
        return
    order_date = order.get("order_date")
    date_str = order_date.isoformat(sep=' ') if hasattr(order_date, "isoformat") else str(order_date or "—")
    text = (
        f"🆕 <b>Новый заказ продукта!</b>\n"
        f"Номер: <code>#{order['id']}</code>\n"
        f"Клиент: {esc(order['user_name'])}\n"
        f"Телефон: {esc(order['phone'])}\n"
        f"Дата: {esc(date_str)}\n"
        f"Статус: {esc(order['status'])}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Посмотреть заказ", callback_data=f"order_details_{order['id']}_0"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")


async def notify_new_mattress_order(bot, order: dict):
    if ADMIN_IDS is None:
        logger.warning("Нет списка администраторов, уведомление не отправлено.")
        return
    order_date = order.get("order_date")
    date_str = order_date.isoformat(sep=' ') if hasattr(order_date, "isoformat") else str(order_date or "—")
    text = (
        f"🛏️ <b>Новый заказ матраца!</b>\n"
        f"Номер: <code>#{order['id']}</code>\n"
        f"Клиент: {esc(order['user_name'])}\n"
        f"Телефон: {esc(order['phone'])}\n"
        f"Размер: {esc(order.get('size_name') or '—')}\n"
        f"Дата: {esc(date_str)}\n"
        f"Статус: {esc(order['status'])}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Посмотреть заказ", callback_data=f"mattress_details_{order['id']}_0"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")


async def periodic_new_orders_check(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: проверка новых заказов (продукты + матрацы)."""
    try:
        last_check = context.bot_data.get("last_order_check")
        if last_check is None:
            last_check = datetime.now()
            context.bot_data["last_order_check"] = last_check
            return

        new_products = await db.get_new_product_orders_since(last_check)
        new_mattresses = await db.get_new_mattress_orders_since(last_check)

        if new_products or new_mattresses:
            context.bot_data["last_order_check"] = datetime.now()
            for order in new_products:
                await notify_new_product_order(context.bot, order)
            for order in new_mattresses:
                await notify_new_mattress_order(context.bot, order)
    except Exception as e:
        logger.error(f"Ошибка в periodic_new_orders_check: {e}")


# ------------------------- Обработчики меню и отмена -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)


@admin_only
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)


@admin_only
async def orders_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_product_orders_list(update, context, 0)


@admin_only
async def mattress_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mattress_orders_list(update, context, 0)


@admin_only
async def check_new_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await check_new_orders_manual(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text("Операция отменена.")
        except Exception:
            pass
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
    app.add_handler(CallbackQueryHandler(mattress_list_callback, pattern="^mattress_list$"))
    app.add_handler(CallbackQueryHandler(check_new_orders_callback, pattern="^check_new_orders$"))
    app.add_handler(CallbackQueryHandler(orders_page_callback, pattern=r"^orders_page_\d+$"))
    app.add_handler(CallbackQueryHandler(mattress_page_callback, pattern=r"^mattress_page_\d+$"))
    app.add_handler(CallbackQueryHandler(product_order_details, pattern=r"^order_details_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(mattress_order_details, pattern=r"^mattress_details_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(other_orders, pattern="^other_orders$"))

    # Изменение статуса (продукты + матрацы)
    status_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(change_status_start, pattern=r"^order_status_\d+_\d+$"),
            CallbackQueryHandler(change_status_start, pattern=r"^mattress_status_\d+_\d+$"),
        ],
        states={
            STATUS_CHANGE: [CallbackQueryHandler(change_status_set, pattern=r"^status_set_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(status_conv)

    # Удаление (продукты + матрацы)
    delete_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(delete_order_start, pattern=r"^order_delete_\d+_\d+$"),
            CallbackQueryHandler(delete_order_start, pattern=r"^mattress_delete_\d+_\d+$"),
        ],
        states={
            DELETE_CONFIRM: [CallbackQueryHandler(delete_order_execute, pattern="^order_del_yes$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(delete_conv)


# ------------------------- Запуск бота -------------------------
async def background_poller(application: Application):
    """Резервный цикл опроса БД, если JobQueue недоступен."""
    while True:
        try:
            await asyncio.sleep(10)
            last_check = application.bot_data.get("last_order_check")
            if last_check is None:
                application.bot_data["last_order_check"] = datetime.now()
                continue

            new_products = await db.get_new_product_orders_since(last_check)
            new_mattresses = await db.get_new_mattress_orders_since(last_check)

            if new_products or new_mattresses:
                application.bot_data["last_order_check"] = datetime.now()
                for order in new_products:
                    await notify_new_product_order(application.bot, order)
                for order in new_mattresses:
                    await notify_new_mattress_order(application.bot, order)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка фонового опроса: {e}")


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
        pool_timeout=30.0,
    )
    application = Application.builder().token(TOKEN).request(request).build()
    register_handlers(application)

    # Инициализируем время последней проверки — только новые заказы после старта
    application.bot_data["last_order_check"] = datetime.now()

    # Пробуем использовать JobQueue, если он доступен
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(periodic_new_orders_check, interval=10, first=5)
        logger.info("Фоновая задача JobQueue запущена (интервал 10 сек).")
        bg_task = None
    else:
        logger.warning("JobQueue недоступен, запускаю собственный фоновый цикл.")
        bg_task = asyncio.create_task(background_poller(application))

    await application.initialize()
    await application.start()
    logger.info("Бот для управления заказами запущен.")
    await application.updater.start_polling()
    try:
        await asyncio.Event().wait()
    finally:
        if bg_task is not None:
            bg_task.cancel()
            try:
                await bg_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())