#!/usr/bin/env python3
"""
Telegram‑бот для управления топерами (слои, чехлы, размеры, цены).
Версия 1.0
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List, Dict, Optional

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
TOKEN_FILE = BASE_DIR / "token_toppers.txt"
CONFIG_DB_FILE = BASE_DIR / "config_db.txt"
ADMIN_FILE = BASE_DIR / "admin_ids.txt"

# ------------------------- Чтение конфигураций -------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def read_token():
    if not TOKEN_FILE.exists():
        logger.error(f"Файл с токеном не найден: {TOKEN_FILE}")
        sys.exit(1)
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
            if not token:
                logger.error("Токен пустой")
                sys.exit(1)
            return token
    except Exception as e:
        logger.error(f"Ошибка чтения token_toppers.txt: {e}")
        sys.exit(1)


def read_db_config():
    if not CONFIG_DB_FILE.exists():
        logger.error(f"Файл конфигурации БД не найден: {CONFIG_DB_FILE}")
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
                logger.error(f"В config_db.txt отсутствует параметр {key}")
                sys.exit(1)
        return config
    except Exception as e:
        logger.error(f"Ошибка чтения config_db.txt: {e}")
        sys.exit(1)


def read_admin_ids():
    if not ADMIN_FILE.exists():
        logger.warning(f"Файл admin_ids.txt не найден: {ADMIN_FILE}")
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
        logger.error(f"Ошибка чтения admin_ids.txt: {e}")
        return []


TOKEN = read_token()
DB_CONFIG = read_db_config()
ADMIN_IDS = read_admin_ids()

# ------------------------- Состояния разговоров -------------------------
(
    MAIN_MENU,
    LAYER_LIST,
    LAYER_ADD_NAME,
    LAYER_ADD_CODE,
    LAYER_ADD_DESC,
    LAYER_EDIT_SELECT,
    LAYER_EDIT_NAME,
    LAYER_EDIT_CODE,
    LAYER_EDIT_DESC,
    LAYER_DELETE_CONFIRM,
    COVER_LIST,
    COVER_ADD_NAME,
    COVER_ADD_CODE,
    COVER_ADD_DESC,
    COVER_EDIT_SELECT,
    COVER_EDIT_NAME,
    COVER_EDIT_CODE,
    COVER_EDIT_DESC,
    COVER_DELETE_CONFIRM,
    SIZE_LIST,
    SIZE_ADD_NAME,
    SIZE_ADD_SORT,
    SIZE_EDIT_NAME,
    SIZE_EDIT_SORT,
    SIZE_DELETE_CONFIRM,
    PRICE_MENU,
    PRICE_ENTITY_SELECT,
    PRICE_SIZE_SELECT,
    PRICE_INPUT,
    PRICE_DELETE_SELECT,
    PRICE_DELETE_CONFIRM,
) = range(31)

# ------------------------- Декоратор администратора -------------------------
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


# ------------------------- База данных -------------------------
class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ---------- Слои ----------
    async def get_layers(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM layers")
            rows = await conn.fetch(
                "SELECT id, code, name, description FROM layers ORDER BY code OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_layer_by_id(self, layer_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, description FROM layers WHERE id = $1", layer_id)
            return dict(row) if row else None

    async def get_layer_by_code(self, code: str):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, description FROM layers WHERE code = $1", code)
            return dict(row) if row else None

    async def create_layer(self, code: str, name: str, description: str = "") -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO layers (code, name, description) VALUES ($1, $2, $3) RETURNING id",
                code, name, description
            )

    async def update_layer(self, layer_id: int, code: str, name: str, description: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE layers SET code = $1, name = $2, description = $3 WHERE id = $4",
                code, name, description, layer_id
            )

    async def delete_layer(self, layer_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Удаляем связанные цены
                await conn.execute("DELETE FROM layer_prices WHERE layer_id = $1", layer_id)
                result = await conn.execute("DELETE FROM layers WHERE id = $1", layer_id)
                return result != "DELETE 0"

    async def is_layer_code_unique(self, code: str, exclude_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_id:
                row = await conn.fetchrow("SELECT id FROM layers WHERE code = $1 AND id != $2", code, exclude_id)
            else:
                row = await conn.fetchrow("SELECT id FROM layers WHERE code = $1", code)
            return row is None

    # ---------- Чехлы ----------
    async def get_covers(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM covers")
            rows = await conn.fetch(
                "SELECT id, code, name, description FROM covers ORDER BY code OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_cover_by_id(self, cover_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, description FROM covers WHERE id = $1", cover_id)
            return dict(row) if row else None

    async def get_cover_by_code(self, code: str):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, description FROM covers WHERE code = $1", code)
            return dict(row) if row else None

    async def create_cover(self, code: str, name: str, description: str = "") -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO covers (code, name, description) VALUES ($1, $2, $3) RETURNING id",
                code, name, description
            )

    async def update_cover(self, cover_id: int, code: str, name: str, description: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE covers SET code = $1, name = $2, description = $3 WHERE id = $4",
                code, name, description, cover_id
            )

    async def delete_cover(self, cover_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM cover_prices WHERE cover_id = $1", cover_id)
                result = await conn.execute("DELETE FROM covers WHERE id = $1", cover_id)
                return result != "DELETE 0"

    async def is_cover_code_unique(self, code: str, exclude_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_id:
                row = await conn.fetchrow("SELECT id FROM covers WHERE code = $1 AND id != $2", code, exclude_id)
            else:
                row = await conn.fetchrow("SELECT id FROM covers WHERE code = $1", code)
            return row is None

    # ---------- Размеры ----------
    async def get_sizes(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM sizes")
            rows = await conn.fetch(
                "SELECT id, name, sort_order FROM sizes ORDER BY sort_order, name OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_size_by_id(self, size_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name, sort_order FROM sizes WHERE id = $1", size_id)
            return dict(row) if row else None

    async def get_all_sizes(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, name, sort_order FROM sizes ORDER BY sort_order, name")
            return [dict(r) for r in rows]

    async def create_size(self, name: str, sort_order: int = 0) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO sizes (name, sort_order) VALUES ($1, $2) RETURNING id",
                name, sort_order
            )

    async def update_size(self, size_id: int, name: str, sort_order: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE sizes SET name = $1, sort_order = $2 WHERE id = $3",
                name, sort_order, size_id
            )

    async def delete_size(self, size_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM layer_prices WHERE size_id = $1", size_id)
                await conn.execute("DELETE FROM cover_prices WHERE size_id = $1", size_id)
                result = await conn.execute("DELETE FROM sizes WHERE id = $1", size_id)
                return result != "DELETE 0"

    async def is_size_name_unique(self, name: str, exclude_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_id:
                row = await conn.fetchrow("SELECT id FROM sizes WHERE name = $1 AND id != $2", name, exclude_id)
            else:
                row = await conn.fetchrow("SELECT id FROM sizes WHERE name = $1", name)
            return row is None

    # ---------- Цены слоёв ----------
    async def get_layer_prices(self, layer_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT lp.id, lp.layer_id, lp.size_id, lp.price, s.name as size_name "
                "FROM layer_prices lp "
                "JOIN sizes s ON lp.size_id = s.id "
                "WHERE lp.layer_id = $1 "
                "ORDER BY s.sort_order, s.name",
                layer_id
            )
            return [dict(r) for r in rows]

    async def set_layer_price(self, layer_id: int, size_id: int, price: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO layer_prices (layer_id, size_id, price) VALUES ($1, $2, $3) "
                "ON CONFLICT (layer_id, size_id) DO UPDATE SET price = $3",
                layer_id, size_id, price
            )

    async def delete_layer_price(self, price_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM layer_prices WHERE id = $1", price_id)

    # ---------- Цены чехлов ----------
    async def get_cover_prices(self, cover_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT cp.id, cp.cover_id, cp.size_id, cp.price, s.name as size_name "
                "FROM cover_prices cp "
                "JOIN sizes s ON cp.size_id = s.id "
                "WHERE cp.cover_id = $1 "
                "ORDER BY s.sort_order, s.name",
                cover_id
            )
            return [dict(r) for r in rows]

    async def set_cover_price(self, cover_id: int, size_id: int, price: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cover_prices (cover_id, size_id, price) VALUES ($1, $2, $3) "
                "ON CONFLICT (cover_id, size_id) DO UPDATE SET price = $3",
                cover_id, size_id, price
            )

    async def delete_cover_price(self, price_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM cover_prices WHERE id = $1", price_id)


db_pool = None
db = None


# ------------------------- Вспомогательные функции -------------------------
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    keyboard = [
        [InlineKeyboardButton("🧩 Слои", callback_data="menu_layers")],
        [InlineKeyboardButton("🧵 Чехлы", callback_data="menu_covers")],
        [InlineKeyboardButton("📐 Размеры", callback_data="menu_sizes")],
        [InlineKeyboardButton("💰 Цены (слои + чехлы)", callback_data="menu_prices")],
    ]
    await send_or_edit_message(update, context, "🔧 *Главное меню управления топерами*",
                               InlineKeyboardMarkup(keyboard), new_message)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    else:
        await update.message.reply_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


# ------------------------- Слои -------------------------
async def menu_layers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_layers(update, context, 0)


async def show_layers(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    layers, total = await db.get_layers(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        layers, total = await db.get_layers(offset, limit)

    text = "*Слои*\n\n"
    if not layers:
        text += "Нет слоёв."
    else:
        for l in layers:
            desc = l['description'][:30] + "…" if len(l['description']) > 30 else l['description']
            text += f"`{l['code']}` – {l['name']}  (описание: {desc})\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for l in layers:
        keyboard.append([
            InlineKeyboardButton(f"📄 {l['code']}", callback_data=f"layer_details_{l['id']}_{page}"),
            InlineKeyboardButton("✏️", callback_data=f"layer_edit_{l['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"layer_del_confirm_{l['id']}_{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"layers_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"layers_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить слой", callback_data="layer_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["layers_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)


async def layers_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_layers(update, context, page)


async def layer_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    layer_id = int(parts[2])
    page = int(parts[3])
    layer = await db.get_layer_by_id(layer_id)
    if not layer:
        await query.edit_message_text("❌ Слой не найден.")
        return
    text = (f"*Слой {layer['code']}*\n\n"
            f"📛 *Название:* {layer['name']}\n"
            f"🔢 *Код:* {layer['code']}\n"
            f"📝 *Описание:* {layer['description'] or '—'}")
    keyboard = [
        [InlineKeyboardButton("◀ К списку слоёв", callback_data=f"layers_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def layer_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите *название* слоя:", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return LAYER_ADD_NAME


async def layer_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_layer_name"] = update.message.text
    await update.message.reply_text("Введите *код* слоя (уникальный):", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return LAYER_ADD_CODE


async def layer_add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    if not await db.is_layer_code_unique(code):
        await update.message.reply_text("❌ Слой с таким кодом уже существует. Введите другой код:", reply_markup=CANCEL_KEYBOARD)
        return LAYER_ADD_CODE
    context.user_data["new_layer_code"] = code
    await update.message.reply_text("Введите *описание* слоя (можно оставить пустым, отправьте '-' для пропуска):",
                                    parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return LAYER_ADD_DESC


async def layer_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == "-":
        desc = ""
    code = context.user_data["new_layer_code"]
    name = context.user_data["new_layer_name"]
    await db.create_layer(code, name, desc)
    await update.message.reply_text(f"✅ Слой создан, код: `{code}`.")
    await show_layers(update, context, context.user_data.get("layers_page", 0))
    return ConversationHandler.END


async def layer_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    layer_id = int(parts[2])
    page = int(parts[3])
    context.user_data["edit_layer_id"] = layer_id
    context.user_data["edit_layer_page"] = page
    layer = await db.get_layer_by_id(layer_id)
    if not layer:
        await query.edit_message_text("❌ Слой не найден.")
        return ConversationHandler.END
    text = (f"*Редактирование слоя {layer['code']}*\n\n"
            f"📛 Название: {layer['name']}\n"
            f"🔢 Код: {layer['code']}\n"
            f"📝 Описание: {layer['description'] or '—'}")
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data="layer_edit_name"),
         InlineKeyboardButton("✏️ Код", callback_data="layer_edit_code")],
        [InlineKeyboardButton("✏️ Описание", callback_data="layer_edit_desc")],
        [InlineKeyboardButton("🗑️ Удалить слой", callback_data="layer_edit_delete")],
        [InlineKeyboardButton("◀ К списку слоёв", callback_data=f"layers_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return LAYER_EDIT_SELECT


async def layer_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое название слоя:", reply_markup=CANCEL_KEYBOARD)
    return LAYER_EDIT_NAME


async def layer_update_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    layer_id = context.user_data["edit_layer_id"]
    layer = await db.get_layer_by_id(layer_id)
    await db.update_layer(layer_id, layer['code'], new_name, layer['description'])
    await update.message.reply_text("✅ Название обновлено.")
    await show_layers(update, context, context.user_data["edit_layer_page"])
    return ConversationHandler.END


async def layer_edit_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый код слоя (уникальный):", reply_markup=CANCEL_KEYBOARD)
    return LAYER_EDIT_CODE


async def layer_update_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    layer_id = context.user_data["edit_layer_id"]
    if not await db.is_layer_code_unique(new_code, exclude_id=layer_id):
        await update.message.reply_text("❌ Код уже существует. Введите другой:", reply_markup=CANCEL_KEYBOARD)
        return LAYER_EDIT_CODE
    layer = await db.get_layer_by_id(layer_id)
    await db.update_layer(layer_id, new_code, layer['name'], layer['description'])
    await update.message.reply_text("✅ Код обновлён.")
    await show_layers(update, context, context.user_data["edit_layer_page"])
    return ConversationHandler.END


async def layer_edit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое описание (или '-' для очистки):", reply_markup=CANCEL_KEYBOARD)
    return LAYER_EDIT_DESC


async def layer_update_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_desc = update.message.text.strip()
    if new_desc == "-":
        new_desc = ""
    layer_id = context.user_data["edit_layer_id"]
    layer = await db.get_layer_by_id(layer_id)
    await db.update_layer(layer_id, layer['code'], layer['name'], new_desc)
    await update.message.reply_text("✅ Описание обновлено.")
    await show_layers(update, context, context.user_data["edit_layer_page"])
    return ConversationHandler.END


async def layer_edit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    layer_id = context.user_data["edit_layer_id"]
    layer = await db.get_layer_by_id(layer_id)
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="layer_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"layers_page_{context.user_data['edit_layer_page']}")]
    ]
    await query.edit_message_text(f"Удалить слой *{layer['name']}* (код {layer['code']}) навсегда? Все связанные цены будут удалены.",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return LAYER_DELETE_CONFIRM


async def layer_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    layer_id = context.user_data.get("edit_layer_id")
    page = context.user_data.get("edit_layer_page", 0)
    if not layer_id:
        await query.edit_message_text("❌ Ошибка: ID слоя не найден.")
        await show_layers(update, context, page)
        return ConversationHandler.END
    success = await db.delete_layer(layer_id)
    await query.edit_message_text("✅ Слой удалён." if success else "❌ Слой не найден.")
    await show_layers(update, context, page)
    return ConversationHandler.END


# ------------------------- Чехлы (аналогично слоям) -------------------------
async def menu_covers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_covers(update, context, 0)


async def show_covers(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    covers, total = await db.get_covers(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        covers, total = await db.get_covers(offset, limit)

    text = "*Чехлы*\n\n"
    if not covers:
        text += "Нет чехлов."
    else:
        for c in covers:
            desc = c['description'][:30] + "…" if len(c['description']) > 30 else c['description']
            text += f"`{c['code']}` – {c['name']}  (описание: {desc})\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for c in covers:
        keyboard.append([
            InlineKeyboardButton(f"📄 {c['code']}", callback_data=f"cover_details_{c['id']}_{page}"),
            InlineKeyboardButton("✏️", callback_data=f"cover_edit_{c['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"cover_del_confirm_{c['id']}_{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"covers_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"covers_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить чехол", callback_data="cover_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["covers_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)


async def covers_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_covers(update, context, page)


async def cover_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    cover_id = int(parts[2])
    page = int(parts[3])
    cover = await db.get_cover_by_id(cover_id)
    if not cover:
        await query.edit_message_text("❌ Чехол не найден.")
        return
    text = (f"*Чехол {cover['code']}*\n\n"
            f"📛 *Название:* {cover['name']}\n"
            f"🔢 *Код:* {cover['code']}\n"
            f"📝 *Описание:* {cover['description'] or '—'}")
    keyboard = [
        [InlineKeyboardButton("◀ К списку чехлов", callback_data=f"covers_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def cover_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите *название* чехла:", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return COVER_ADD_NAME


async def cover_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_cover_name"] = update.message.text
    await update.message.reply_text("Введите *код* чехла (уникальный):", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return COVER_ADD_CODE


async def cover_add_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    if not await db.is_cover_code_unique(code):
        await update.message.reply_text("❌ Чехол с таким кодом уже существует. Введите другой код:", reply_markup=CANCEL_KEYBOARD)
        return COVER_ADD_CODE
    context.user_data["new_cover_code"] = code
    await update.message.reply_text("Введите *описание* чехла (можно оставить пустым, отправьте '-' для пропуска):",
                                    parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return COVER_ADD_DESC


async def cover_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == "-":
        desc = ""
    code = context.user_data["new_cover_code"]
    name = context.user_data["new_cover_name"]
    await db.create_cover(code, name, desc)
    await update.message.reply_text(f"✅ Чехол создан, код: `{code}`.")
    await show_covers(update, context, context.user_data.get("covers_page", 0))
    return ConversationHandler.END


async def cover_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    cover_id = int(parts[2])
    page = int(parts[3])
    context.user_data["edit_cover_id"] = cover_id
    context.user_data["edit_cover_page"] = page
    cover = await db.get_cover_by_id(cover_id)
    if not cover:
        await query.edit_message_text("❌ Чехол не найден.")
        return ConversationHandler.END
    text = (f"*Редактирование чехла {cover['code']}*\n\n"
            f"📛 Название: {cover['name']}\n"
            f"🔢 Код: {cover['code']}\n"
            f"📝 Описание: {cover['description'] or '—'}")
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data="cover_edit_name"),
         InlineKeyboardButton("✏️ Код", callback_data="cover_edit_code")],
        [InlineKeyboardButton("✏️ Описание", callback_data="cover_edit_desc")],
        [InlineKeyboardButton("🗑️ Удалить чехол", callback_data="cover_edit_delete")],
        [InlineKeyboardButton("◀ К списку чехлов", callback_data=f"covers_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return COVER_EDIT_SELECT


async def cover_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое название чехла:", reply_markup=CANCEL_KEYBOARD)
    return COVER_EDIT_NAME


async def cover_update_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    cover_id = context.user_data["edit_cover_id"]
    cover = await db.get_cover_by_id(cover_id)
    await db.update_cover(cover_id, cover['code'], new_name, cover['description'])
    await update.message.reply_text("✅ Название обновлено.")
    await show_covers(update, context, context.user_data["edit_cover_page"])
    return ConversationHandler.END


async def cover_edit_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый код чехла (уникальный):", reply_markup=CANCEL_KEYBOARD)
    return COVER_EDIT_CODE


async def cover_update_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    cover_id = context.user_data["edit_cover_id"]
    if not await db.is_cover_code_unique(new_code, exclude_id=cover_id):
        await update.message.reply_text("❌ Код уже существует. Введите другой:", reply_markup=CANCEL_KEYBOARD)
        return COVER_EDIT_CODE
    cover = await db.get_cover_by_id(cover_id)
    await db.update_cover(cover_id, new_code, cover['name'], cover['description'])
    await update.message.reply_text("✅ Код обновлён.")
    await show_covers(update, context, context.user_data["edit_cover_page"])
    return ConversationHandler.END


async def cover_edit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое описание (или '-' для очистки):", reply_markup=CANCEL_KEYBOARD)
    return COVER_EDIT_DESC


async def cover_update_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_desc = update.message.text.strip()
    if new_desc == "-":
        new_desc = ""
    cover_id = context.user_data["edit_cover_id"]
    cover = await db.get_cover_by_id(cover_id)
    await db.update_cover(cover_id, cover['code'], cover['name'], new_desc)
    await update.message.reply_text("✅ Описание обновлено.")
    await show_covers(update, context, context.user_data["edit_cover_page"])
    return ConversationHandler.END


async def cover_edit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cover_id = context.user_data["edit_cover_id"]
    cover = await db.get_cover_by_id(cover_id)
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="cover_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"covers_page_{context.user_data['edit_cover_page']}")]
    ]
    await query.edit_message_text(f"Удалить чехол *{cover['name']}* (код {cover['code']}) навсегда? Все связанные цены будут удалены.",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return COVER_DELETE_CONFIRM


async def cover_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cover_id = context.user_data.get("edit_cover_id")
    page = context.user_data.get("edit_cover_page", 0)
    if not cover_id:
        await query.edit_message_text("❌ Ошибка: ID чехла не найден.")
        await show_covers(update, context, page)
        return ConversationHandler.END
    success = await db.delete_cover(cover_id)
    await query.edit_message_text("✅ Чехол удалён." if success else "❌ Чехол не найден.")
    await show_covers(update, context, page)
    return ConversationHandler.END


# ------------------------- Размеры -------------------------
async def menu_sizes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_sizes(update, context, 0)


async def show_sizes(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    sizes, total = await db.get_sizes(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        sizes, total = await db.get_sizes(offset, limit)

    text = "*Размеры*\n\n"
    if not sizes:
        text += "Нет размеров."
    else:
        for s in sizes:
            text += f"• {s['name']} (порядок: {s['sort_order']})\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for s in sizes:
        keyboard.append([
            InlineKeyboardButton(f"📏 {s['name']}", callback_data=f"size_details_{s['id']}_{page}"),
            InlineKeyboardButton("✏️", callback_data=f"size_edit_{s['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"size_del_confirm_{s['id']}_{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"sizes_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"sizes_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить размер", callback_data="size_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["sizes_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)


async def sizes_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_sizes(update, context, page)


async def size_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    size_id = int(parts[2])
    page = int(parts[3])
    size = await db.get_size_by_id(size_id)
    if not size:
        await query.edit_message_text("❌ Размер не найден.")
        return
    text = (f"*Размер {size['name']}*\n\n"
            f"📛 *Название:* {size['name']}\n"
            f"🔢 *Порядок сортировки:* {size['sort_order']}")
    keyboard = [
        [InlineKeyboardButton("◀ К списку размеров", callback_data=f"sizes_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def size_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите *название* размера (например, S, M, L):",
                                                  parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return SIZE_ADD_NAME


async def size_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not await db.is_size_name_unique(name):
        await update.message.reply_text("❌ Размер с таким названием уже существует. Введите другое:", reply_markup=CANCEL_KEYBOARD)
        return SIZE_ADD_NAME
    context.user_data["new_size_name"] = name
    await update.message.reply_text("Введите *порядок сортировки* (целое число, чем меньше, тем выше в списке):",
                                    parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return SIZE_ADD_SORT


async def size_add_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sort_order = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите целое число.", reply_markup=CANCEL_KEYBOARD)
        return SIZE_ADD_SORT
    name = context.user_data["new_size_name"]
    await db.create_size(name, sort_order)
    await update.message.reply_text(f"✅ Размер '{name}' добавлен.")
    await show_sizes(update, context, context.user_data.get("sizes_page", 0))
    return ConversationHandler.END


async def size_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    size_id = int(parts[2])
    page = int(parts[3])
    context.user_data["edit_size_id"] = size_id
    context.user_data["edit_size_page"] = page
    size = await db.get_size_by_id(size_id)
    if not size:
        await query.edit_message_text("❌ Размер не найден.")
        return ConversationHandler.END
    text = (f"*Редактирование размера {size['name']}*\n\n"
            f"📛 Название: {size['name']}\n"
            f"🔢 Порядок сортировки: {size['sort_order']}")
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data="size_edit_name"),
         InlineKeyboardButton("✏️ Порядок", callback_data="size_edit_sort")],
        [InlineKeyboardButton("🗑️ Удалить размер", callback_data="size_edit_delete")],
        [InlineKeyboardButton("◀ К списку размеров", callback_data=f"sizes_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SIZE_EDIT_NAME  # Неважно, потом переключим по callback


async def size_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое название размера:", reply_markup=CANCEL_KEYBOARD)
    return SIZE_EDIT_NAME


async def size_update_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    size_id = context.user_data["edit_size_id"]
    if not await db.is_size_name_unique(new_name, exclude_id=size_id):
        await update.message.reply_text("❌ Размер с таким названием уже существует. Введите другое:", reply_markup=CANCEL_KEYBOARD)
        return SIZE_EDIT_NAME
    size = await db.get_size_by_id(size_id)
    await db.update_size(size_id, new_name, size['sort_order'])
    await update.message.reply_text("✅ Название обновлено.")
    await show_sizes(update, context, context.user_data["edit_size_page"])
    return ConversationHandler.END


async def size_edit_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый порядок сортировки (целое число):", reply_markup=CANCEL_KEYBOARD)
    return SIZE_EDIT_SORT


async def size_update_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_sort = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите целое число.", reply_markup=CANCEL_KEYBOARD)
        return SIZE_EDIT_SORT
    size_id = context.user_data["edit_size_id"]
    size = await db.get_size_by_id(size_id)
    await db.update_size(size_id, size['name'], new_sort)
    await update.message.reply_text("✅ Порядок обновлён.")
    await show_sizes(update, context, context.user_data["edit_size_page"])
    return ConversationHandler.END


async def size_edit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    size_id = context.user_data["edit_size_id"]
    size = await db.get_size_by_id(size_id)
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="size_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"sizes_page_{context.user_data['edit_size_page']}")]
    ]
    await query.edit_message_text(f"Удалить размер *{size['name']}*? Все связанные цены будут удалены.",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SIZE_DELETE_CONFIRM


async def size_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    size_id = context.user_data.get("edit_size_id")
    page = context.user_data.get("edit_size_page", 0)
    if not size_id:
        await query.edit_message_text("❌ Ошибка: ID размера не найден.")
        await show_sizes(update, context, page)
        return ConversationHandler.END
    success = await db.delete_size(size_id)
    await query.edit_message_text("✅ Размер удалён." if success else "❌ Размер не найден.")
    await show_sizes(update, context, page)
    return ConversationHandler.END


# ------------------------- Управление ценами -------------------------
async def menu_prices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("💰 Цены слоёв", callback_data="prices_layer")],
        [InlineKeyboardButton("💰 Цены чехлов", callback_data="prices_cover")],
        [InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text("Выберите тип цен:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PRICE_MENU


async def prices_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    price_type = query.data.split("_")[-1]  # layer или cover
    context.user_data["price_type"] = price_type
    # Показываем список сущностей (слоёв или чехлов)
    if price_type == "layer":
        entities, _ = await db.get_layers(0, 1000)
        title = "слоёв"
    else:
        entities, _ = await db.get_covers(0, 1000)
        title = "чехлов"
    if not entities:
        await query.edit_message_text(f"❌ Нет {title}. Сначала создайте хотя бы одну запись.")
        return ConversationHandler.END
    keyboard = []
    for e in entities:
        keyboard.append([InlineKeyboardButton(e['name'], callback_data=f"price_entity_{e['id']}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    await query.edit_message_text(f"Выберите {title[:-1]} для управления ценами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PRICE_ENTITY_SELECT


async def price_entity_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    entity_id = int(query.data.split("_")[-1])
    context.user_data["price_entity_id"] = entity_id
    price_type = context.user_data["price_type"]

    # Получаем текущие цены
    if price_type == "layer":
        prices = await db.get_layer_prices(entity_id)
        entity = await db.get_layer_by_id(entity_id)
        entity_name = entity['name'] if entity else "Слой"
    else:
        prices = await db.get_cover_prices(entity_id)
        entity = await db.get_cover_by_id(entity_id)
        entity_name = entity['name'] if entity else "Чехол"

    text = f"*Цены для {entity_name}*\n\n"
    if prices:
        for p in prices:
            text += f"• {p['size_name']}: {p['price']} руб.\n"
    else:
        text += "Нет установленных цен."

    sizes = await db.get_all_sizes()
    if not sizes:
        await query.edit_message_text("❌ Нет размеров. Сначала создайте размеры.")
        return ConversationHandler.END

    keyboard = []
    for s in sizes:
        keyboard.append([InlineKeyboardButton(f"➕ {s['name']}", callback_data=f"price_add_{s['id']}")])
    # Добавим кнопки для удаления существующих цен
    if prices:
        for p in prices:
            keyboard.append([InlineKeyboardButton(f"🗑️ Удалить {p['size_name']} ({p['price']} руб.)",
                                                  callback_data=f"price_del_{p['id']}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к выбору типа", callback_data="prices_back")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PRICE_SIZE_SELECT


async def price_add_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    size_id = int(query.data.split("_")[-1])
    context.user_data["price_size_id"] = size_id
    await query.edit_message_text("Введите цену в рублях (число, можно с десятичной частью):",
                                  reply_markup=CANCEL_KEYBOARD)
    return PRICE_INPUT


async def price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace(',', '.'))
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите положительное число.", reply_markup=CANCEL_KEYBOARD)
        return PRICE_INPUT

    entity_id = context.user_data["price_entity_id"]
    size_id = context.user_data["price_size_id"]
    price_type = context.user_data["price_type"]

    if price_type == "layer":
        await db.set_layer_price(entity_id, size_id, price)
    else:
        await db.set_cover_price(entity_id, size_id, price)

    await update.message.reply_text("✅ Цена сохранена.")
    # Возвращаемся к управлению ценами для этой сущности
    # Заново вызовем price_entity_selected, но через новый callback или просто перезапустим состояние
    # Проще создать новый запрос
    # Создадим фиктивный update с callback данными
    fake_query = update.callback_query
    if fake_query:
        await fake_query.edit_message_text("Цена обновлена.")
    # Переходим обратно к списку цен
    await price_entity_selected(update, context)
    return ConversationHandler.END


async def price_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    price_id = int(query.data.split("_")[-1])
    price_type = context.user_data["price_type"]
    if price_type == "layer":
        await db.delete_layer_price(price_id)
    else:
        await db.delete_cover_price(price_id)
    await query.edit_message_text("✅ Цена удалена.")
    # Возвращаемся к списку
    await price_entity_selected(update, context)
    return ConversationHandler.END


async def prices_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await menu_prices_callback(update, context)
    return PRICE_MENU


# ------------------------- Обработчик команды /start и /reboot -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)


@admin_only
async def reboot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        process = await asyncio.create_subprocess_exec(
            "pm2", "restart", "1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            await update.message.reply_text(
                f"✅ Команда 'pm2 restart 1' выполнена успешно.\n{stdout.decode()}"
            )
        else:
            await update.message.reply_text(
                f"❌ Ошибка при выполнении 'pm2 restart 1':\n{stderr.decode()}"
            )
    except FileNotFoundError:
        await update.message.reply_text("❌ Команда 'pm2' не найдена.")
    except Exception as e:
        logger.exception("Ошибка при выполнении reboot")
        await update.message.reply_text(f"❌ Непредвиденная ошибка: {e}")


# ------------------------- Регистрация обработчиков -------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reboot", reboot_command))

    # Главное меню
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern="^main_menu$"))

    # Слои
    app.add_handler(CallbackQueryHandler(menu_layers_callback, pattern="^menu_layers$"))
    app.add_handler(CallbackQueryHandler(layers_page_callback, pattern="^layers_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(layer_details, pattern="^layer_details_\\d+_\\d+$"))

    layer_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(layer_add_entry, pattern="^layer_add$")],
        states={
            LAYER_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_add_name)],
            LAYER_ADD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_add_code)],
            LAYER_ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_add_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(layer_add_conv)

    layer_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(layer_edit_entry, pattern="^layer_edit_\\d+_\\d+$")],
        states={
            LAYER_EDIT_SELECT: [
                CallbackQueryHandler(layer_edit_name, pattern="^layer_edit_name$"),
                CallbackQueryHandler(layer_edit_code, pattern="^layer_edit_code$"),
                CallbackQueryHandler(layer_edit_desc, pattern="^layer_edit_desc$"),
                CallbackQueryHandler(layer_edit_delete, pattern="^layer_edit_delete$"),
                CallbackQueryHandler(show_layers, pattern="^layers_page_\\d+$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
            LAYER_EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_update_name)],
            LAYER_EDIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_update_code)],
            LAYER_EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, layer_update_desc)],
            LAYER_DELETE_CONFIRM: [CallbackQueryHandler(layer_delete_execute, pattern="^layer_del_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(layer_edit_conv)

    # Чехлы
    app.add_handler(CallbackQueryHandler(menu_covers_callback, pattern="^menu_covers$"))
    app.add_handler(CallbackQueryHandler(covers_page_callback, pattern="^covers_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(cover_details, pattern="^cover_details_\\d+_\\d+$"))

    cover_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cover_add_entry, pattern="^cover_add$")],
        states={
            COVER_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_add_name)],
            COVER_ADD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_add_code)],
            COVER_ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_add_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(cover_add_conv)

    cover_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cover_edit_entry, pattern="^cover_edit_\\d+_\\d+$")],
        states={
            COVER_EDIT_SELECT: [
                CallbackQueryHandler(cover_edit_name, pattern="^cover_edit_name$"),
                CallbackQueryHandler(cover_edit_code, pattern="^cover_edit_code$"),
                CallbackQueryHandler(cover_edit_desc, pattern="^cover_edit_desc$"),
                CallbackQueryHandler(cover_edit_delete, pattern="^cover_edit_delete$"),
                CallbackQueryHandler(show_covers, pattern="^covers_page_\\d+$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
            COVER_EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_update_name)],
            COVER_EDIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_update_code)],
            COVER_EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, cover_update_desc)],
            COVER_DELETE_CONFIRM: [CallbackQueryHandler(cover_delete_execute, pattern="^cover_del_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(cover_edit_conv)

    # Размеры
    app.add_handler(CallbackQueryHandler(menu_sizes_callback, pattern="^menu_sizes$"))
    app.add_handler(CallbackQueryHandler(sizes_page_callback, pattern="^sizes_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(size_details, pattern="^size_details_\\d+_\\d+$"))

    size_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(size_add_entry, pattern="^size_add$")],
        states={
            SIZE_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, size_add_name)],
            SIZE_ADD_SORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, size_add_sort)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(size_add_conv)

    size_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(size_edit_entry, pattern="^size_edit_\\d+_\\d+$")],
        states={
            SIZE_EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, size_update_name)],
            SIZE_EDIT_SORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, size_update_sort)],
            SIZE_DELETE_CONFIRM: [CallbackQueryHandler(size_delete_execute, pattern="^size_del_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(size_edit_conv)

    # Цены
    price_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_prices_callback, pattern="^menu_prices$")],
        states={
            PRICE_MENU: [
                CallbackQueryHandler(prices_type_selected, pattern="^prices_(layer|cover)$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
            PRICE_ENTITY_SELECT: [
                CallbackQueryHandler(price_entity_selected, pattern="^price_entity_\\d+$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
            PRICE_SIZE_SELECT: [
                CallbackQueryHandler(price_add_size, pattern="^price_add_\\d+$"),
                CallbackQueryHandler(price_delete, pattern="^price_del_\\d+$"),
                CallbackQueryHandler(prices_back, pattern="^prices_back$"),
                CallbackQueryHandler(show_main_menu, pattern="^main_menu$"),
            ],
            PRICE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(price_conv)


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

    # Создаём таблицы, если их нет
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS layers (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE IF NOT EXISTS covers (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE IF NOT EXISTS sizes (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS layer_prices (
                id SERIAL PRIMARY KEY,
                layer_id INTEGER REFERENCES layers(id) ON DELETE CASCADE,
                size_id INTEGER REFERENCES sizes(id) ON DELETE CASCADE,
                price NUMERIC(10,2) NOT NULL,
                UNIQUE(layer_id, size_id)
            );
            CREATE TABLE IF NOT EXISTS cover_prices (
                id SERIAL PRIMARY KEY,
                cover_id INTEGER REFERENCES covers(id) ON DELETE CASCADE,
                size_id INTEGER REFERENCES sizes(id) ON DELETE CASCADE,
                price NUMERIC(10,2) NOT NULL,
                UNIQUE(cover_id, size_id)
            );
        """)

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
    logging.info("Бот для управления топерами запущен.")
    await application.updater.start_polling()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())