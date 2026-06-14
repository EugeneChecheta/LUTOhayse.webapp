#!/usr/bin/env python3
"""
Telegram‑бот для управления интернет‑магазином (PostgreSQL).
Добавлена поддержка материалов: CRUD, фотографии (одно фото на материал).
Исправлена стабильность: обработка выхода из разговоров, предотвращение зависаний.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import io

import asyncpg
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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

# Состояния разговоров (расширены для материалов)
(
    ADD_PROD_NAME,
    ADD_PROD_CODE,
    ADD_PROD_TYPE_ID,
    EDIT_PROD_NAME,
    EDIT_PROD_CODE,
    EDIT_PROD_TYPE_ID,
    EDIT_PROD_FLAGS,
    EDIT_PROD_COST_MENU,
    EDIT_PROD_COST_ADD_TYPE,
    EDIT_PROD_COST_VALUE,
    EDIT_PROD_COST_DELETE,
    DELETE_PROD_CONFIRM,
    ADD_FLAG_NAME,
    ADD_PROD_TYPE_NAME,
    ADD_MAT_TYPE_NAME,
    DELETE_FLAG_CONFIRM,
    DELETE_PROD_TYPE_CONFIRM,
    DELETE_MAT_TYPE_CONFIRM,
    SEARCH_KEYWORD,
    PHOTO_MANAGE,
    PHOTO_ADD_TYPE_SELECT,
    PHOTO_ADD_WAIT,
    PHOTO_ADD_MAIN_COLLECT,
    PHOTO_DELETE_SELECT,
    SEARCH_TYPE_SELECT,
    SEARCH_FLAGS_SELECT,
    ADD_MAIN_FEATURE_NAME,
    EDIT_MAIN_FEATURE_NAME,
    DELETE_MAIN_FEATURE_CONFIRM,
    PROD_MAIN_FEATURES,
    PROD_MAIN_FEATURE_EDIT,
    PROD_EXTRA_FEATURES,
    PROD_EXTRA_FEATURE_ADD_NAME,
    PROD_EXTRA_FEATURE_ADD_VALUE,
    PROD_EXTRA_FEATURE_EDIT_NAME,
    PROD_EXTRA_FEATURE_EDIT_VALUE,
    # Новые состояния для управления материалами
    ADD_MAT_TYPE_SELECT,
    ADD_MAT_CODE,
    ADD_MAT_NAME,
    ADD_MAT_PHOTO,
    EDIT_MAT_SELECT,
    EDIT_MAT_NAME,
    EDIT_MAT_CODE,
    EDIT_MAT_TYPE,
    EDIT_MAT_PHOTO,
    DELETE_MAT_CONFIRM,
) = range(46)

# Путь к папке media
MEDIA_BASE = Path(__file__).parent.parent / "media"
PRODUCTS_MEDIA = MEDIA_BASE / "products"
MATERIALS_MEDIA = MEDIA_BASE / "materials"
PRODUCTS_MEDIA.mkdir(parents=True, exist_ok=True)
MATERIALS_MEDIA.mkdir(parents=True, exist_ok=True)

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

    # ---------- Типы товаров ----------
    async def get_product_types(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM products_type")
            rows = await conn.fetch(
                "SELECT id, name FROM products_type ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_product_type_by_id(self, type_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name FROM products_type WHERE id = $1", type_id)
            return dict(row) if row else None

    async def create_product_type(self, name: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO products_type (name) VALUES ($1) RETURNING id", name
            )

    async def update_product_type(self, type_id: int, new_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE products_type SET name = $1 WHERE id = $2", new_name, type_id)

    async def delete_product_type_cascade(self, type_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                prod_ids = await conn.fetch("SELECT id, code FROM products WHERE products_type_id = $1", type_id)
                for rec in prod_ids:
                    await conn.execute("DELETE FROM flags_for_products WHERE products_id = $1", rec["id"])
                    await conn.execute("DELETE FROM materials_for_products WHERE products_id = $1", rec["id"])
                    await conn.execute("DELETE FROM product_main_features WHERE products_id = $1", rec["id"])
                    await conn.execute("DELETE FROM product_extra_features WHERE products_id = $1", rec["id"])
                    await delete_media_folder(rec["code"])
                await conn.execute("DELETE FROM products WHERE products_type_id = $1", type_id)
                result = await conn.execute("DELETE FROM products_type WHERE id = $1", type_id)
                return result != "DELETE 0"

    # ---------- Флаги ----------
    async def get_flags(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM flags")
            rows = await conn.fetch(
                "SELECT id, name FROM flags ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def create_flag(self, name: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("INSERT INTO flags (name) VALUES ($1) RETURNING id", name)

    async def update_flag(self, flag_id: int, new_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE flags SET name = $1 WHERE id = $2", new_name, flag_id)

    async def delete_flag(self, flag_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM flags WHERE id = $1", flag_id)

    # ---------- Типы материалов ----------
    async def get_material_types(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM materials_type")
            rows = await conn.fetch(
                "SELECT id, name FROM materials_type ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_material_type_by_id(self, mt_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name FROM materials_type WHERE id = $1", mt_id)
            return dict(row) if row else None

    async def create_material_type(self, name: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("INSERT INTO materials_type (name) VALUES ($1) RETURNING id", name)

    async def update_material_type(self, mt_id: int, new_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE materials_type SET name = $1 WHERE id = $2", new_name, mt_id)

    async def delete_material_type_cascade(self, mt_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM materials_for_products WHERE materials_type_id = $1", mt_id)
                result = await conn.execute("DELETE FROM materials_type WHERE id = $1", mt_id)
                return result != "DELETE 0"

    # ---------- Основные характеристики (справочник) ----------
    async def get_main_feature_types(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM main_features_types")
            rows = await conn.fetch(
                "SELECT id, name FROM main_features_types ORDER BY id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def create_main_feature_type(self, name: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO main_features_types (name) VALUES ($1) RETURNING id", name
            )

    async def update_main_feature_type(self, feat_id: int, new_name: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE main_features_types SET name = $1 WHERE id = $2", new_name, feat_id)

    async def delete_main_feature_type(self, feat_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM main_features_types WHERE id = $1", feat_id)

    # ---------- Основные характеристики товара ----------
    async def get_product_main_features(self, prod_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT mft.id as feature_id, mft.name as feature_name, COALESCE(pmf.value, '') as value "
                "FROM main_features_types mft "
                "LEFT JOIN product_main_features pmf ON mft.id = pmf.feature_id AND pmf.products_id = $1 "
                "ORDER BY mft.id", prod_id
            )
            return [dict(r) for r in rows]

    async def set_product_main_feature(self, prod_id: int, feature_id: int, value: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO product_main_features (products_id, feature_id, value) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (products_id, feature_id) DO UPDATE SET value = $3",
                prod_id, feature_id, value
            )

    # ---------- Дополнительные характеристики товара ----------
    async def get_product_extra_features(self, prod_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, value FROM product_extra_features WHERE products_id = $1 ORDER BY id",
                prod_id
            )
            return [dict(r) for r in rows]

    async def add_product_extra_feature(self, prod_id: int, name: str, value: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO product_extra_features (products_id, name, value) VALUES ($1, $2, $3) RETURNING id",
                prod_id, name, value
            )

    async def update_product_extra_feature(self, feat_id: int, name: str = None, value: str = None):
        async with self.pool.acquire() as conn:
            if name is not None and value is not None:
                await conn.execute("UPDATE product_extra_features SET name = $1, value = $2 WHERE id = $3", name, value, feat_id)
            elif name is not None:
                await conn.execute("UPDATE product_extra_features SET name = $1 WHERE id = $2", name, feat_id)
            elif value is not None:
                await conn.execute("UPDATE product_extra_features SET value = $1 WHERE id = $2", value, feat_id)

    async def delete_product_extra_feature(self, feat_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM product_extra_features WHERE id = $1", feat_id)

    # ---------- Товары (с фильтрацией) ----------
    async def get_products(self, search: str = None, type_id: int = None, flag_ids: List[int] = None,
                           offset: int = 0, limit: int = 10):
        async with self.pool.acquire() as conn:
            if flag_ids:
                query = """
                    SELECT p.id, p.code, p.name, p.products_type_id
                    FROM products p
                    JOIN flags_for_products fp ON p.id = fp.products_id
                    WHERE 1=1
                """
                params = []
                idx = 1
                if search:
                    query += f" AND (p.name ILIKE ${idx} OR p.code ILIKE ${idx})"
                    params.append(f"%{search}%")
                    idx += 1
                if type_id is not None:
                    query += f" AND p.products_type_id = ${idx}"
                    params.append(type_id)
                    idx += 1
                query += f" AND fp.flags_id = ANY(${idx}::int[])"
                params.append(flag_ids)
                idx += 1
                query += """
                    GROUP BY p.id, p.code, p.name, p.products_type_id
                    HAVING COUNT(DISTINCT fp.flags_id) = $3
                    ORDER BY p.id OFFSET $""" + str(idx) + f" LIMIT ${idx+1}"
                params.append(offset)
                params.append(limit)
                # total
                total = await conn.fetchval("""
                    SELECT COUNT(DISTINCT p.id) FROM products p
                    JOIN flags_for_products fp ON p.id = fp.products_id
                    WHERE 1=1 """ +
                    (f"AND (p.name ILIKE '%{search}%' OR p.code ILIKE '%{search}%')" if search else "") +
                    (f"AND p.products_type_id = {type_id}" if type_id else "") +
                    f" AND fp.flags_id = ANY(ARRAY{flag_ids}) HAVING COUNT(DISTINCT fp.flags_id) = {len(flag_ids)}"
                ) if flag_ids else 0
            else:
                query = "SELECT id, code, name, products_type_id FROM products WHERE 1=1"
                params = []
                idx = 1
                if search:
                    query += f" AND (name ILIKE ${idx} OR code ILIKE ${idx})"
                    params.append(f"%{search}%")
                    idx += 1
                if type_id is not None:
                    query += f" AND products_type_id = ${idx}"
                    params.append(type_id)
                    idx += 1
                query += f" ORDER BY id OFFSET ${idx} LIMIT ${idx+1}"
                params.append(offset)
                params.append(limit)
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM products WHERE 1=1" +
                    (f" AND (name ILIKE '%{search}%' OR code ILIKE '%{search}%')" if search else "") +
                    (f" AND products_type_id = {type_id}" if type_id else "")
                )
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows], total

    async def get_product_by_id(self, prod_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, products_type_id FROM products WHERE id = $1", prod_id)
            return dict(row) if row else None

    async def get_product_by_code(self, code: str):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, code, name, products_type_id FROM products WHERE code = $1", code)
            return dict(row) if row else None

    async def create_product(self, code: str, name: str, type_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO products (code, name, products_type_id) VALUES ($1, $2, $3) RETURNING id",
                code, name, type_id
            )

    async def update_product(self, prod_id: int, code: str, name: str, type_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE products SET code = $1, name = $2, products_type_id = $3 WHERE id = $4",
                code, name, type_id, prod_id
            )

    async def delete_product_cascade(self, prod_id: int):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM flags_for_products WHERE products_id = $1", prod_id)
                await conn.execute("DELETE FROM materials_for_products WHERE products_id = $1", prod_id)
                await conn.execute("DELETE FROM product_main_features WHERE products_id = $1", prod_id)
                await conn.execute("DELETE FROM product_extra_features WHERE products_id = $1", prod_id)
                await conn.execute("DELETE FROM products WHERE id = $1", prod_id)

    async def get_product_flags(self, prod_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT f.id, f.name FROM flags f JOIN flags_for_products fp ON f.id = fp.flags_id "
                "WHERE fp.products_id = $1 ORDER BY f.id", prod_id
            )
            return [dict(r) for r in rows]

    async def set_product_flags(self, prod_id: int, flag_ids: List[int]):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM flags_for_products WHERE products_id = $1", prod_id)
                if flag_ids:
                    await conn.executemany(
                        "INSERT INTO flags_for_products (products_id, flags_id) VALUES ($1, $2)",
                        [(prod_id, fid) for fid in flag_ids]
                    )

    async def get_costs_for_product(self, prod_id: int):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT mfp.id, mfp.materials_type_id, mt.name as material_name, mfp.cost "
                "FROM materials_for_products mfp JOIN materials_type mt ON mfp.materials_type_id = mt.id "
                "WHERE mfp.products_id = $1 ORDER BY mt.id", prod_id
            )
            return [dict(r) for r in rows]

    async def set_cost(self, prod_id: int, material_type_id: int, cost: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO materials_for_products (products_id, materials_type_id, cost) VALUES ($1, $2, $3) "
                "ON CONFLICT (products_id, materials_type_id) DO UPDATE SET cost = $3",
                prod_id, material_type_id, cost
            )

    async def delete_cost(self, cost_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM materials_for_products WHERE id = $1", cost_id)

    async def is_code_unique(self, code: str, exclude_product_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_product_id:
                row = await conn.fetchrow("SELECT id FROM products WHERE code = $1 AND id != $2", code, exclude_product_id)
            else:
                row = await conn.fetchrow("SELECT id FROM products WHERE code = $1", code)
            return row is None

    # ---------- Методы для материалов ----------
    async def get_materials(self, offset=0, limit=10):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM materials")
            rows = await conn.fetch(
                "SELECT m.id, m.code, m.name, m.materials_type_id, mt.name as type_name "
                "FROM materials m "
                "JOIN materials_type mt ON m.materials_type_id = mt.id "
                "ORDER BY m.id OFFSET $1 LIMIT $2",
                offset, limit
            )
            return [dict(r) for r in rows], total

    async def get_material_by_id(self, material_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT m.id, m.code, m.name, m.materials_type_id, mt.name as type_name "
                "FROM materials m "
                "JOIN materials_type mt ON m.materials_type_id = mt.id "
                "WHERE m.id = $1", material_id
            )
            return dict(row) if row else None

    async def get_material_by_code(self, code: str):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, code, name, materials_type_id FROM materials WHERE code = $1", code
            )
            return dict(row) if row else None

    async def create_material(self, code: str, name: str, material_type_id: int) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO materials (code, name, materials_type_id) VALUES ($1, $2, $3) RETURNING id",
                code, name, material_type_id
            )

    async def update_material(self, material_id: int, code: str, name: str, material_type_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE materials SET code = $1, name = $2, materials_type_id = $3 WHERE id = $4",
                code, name, material_type_id, material_id
            )

    async def delete_material(self, material_id: int):
        async with self.pool.acquire() as conn:
            # Проверяем, используется ли материал в materials_for_products
            used = await conn.fetchval("SELECT 1 FROM materials_for_products WHERE materials_type_id = "
                                       "(SELECT materials_type_id FROM materials WHERE id = $1) LIMIT 1", material_id)
            if used:
                raise ValueError("Материал используется в товарах, сначала удалите связи.")
            await conn.execute("DELETE FROM materials WHERE id = $1", material_id)

    async def is_material_code_unique(self, code: str, exclude_id: Optional[int] = None) -> bool:
        async with self.pool.acquire() as conn:
            if exclude_id:
                row = await conn.fetchrow("SELECT id FROM materials WHERE code = $1 AND id != $2", code, exclude_id)
            else:
                row = await conn.fetchrow("SELECT id FROM materials WHERE code = $1", code)
            return row is None

db_pool = None
db = None

# ------------------------- Работа с фото товаров -------------------------
async def download_photo_with_retry(photo, max_retries=3, delay=2.0):
    for attempt in range(max_retries):
        try:
            return await photo.get_file()
        except Exception as e:
            logger.warning(f"Попытка {attempt+1}/{max_retries} скачать фото не удалась: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(delay)
    return None

def get_product_media_dir(product_code: str) -> Path:
    return PRODUCTS_MEDIA / product_code

def ensure_media_dir(product_code: str) -> Path:
    path = get_product_media_dir(product_code)
    path.mkdir(parents=True, exist_ok=True)
    return path

async def save_photo_from_telegram(file, product_code: str, photo_type: str, index: int = None) -> bool:
    try:
        photo_bytes = await file.download_as_bytearray()
        img = Image.open(io.BytesIO(photo_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = rgb_img
        ensure_media_dir(product_code)
        if photo_type == 'preview':
            filename = 'preview.webp'
            quality = 50
        elif photo_type == 'size':
            filename = 'size.webp'
            quality = 85
        else:
            if index is None:
                existing = list(get_product_media_dir(product_code).glob('[0-9]*.webp'))
                numbers = [int(p.stem) for p in existing if p.stem.isdigit()]
                index = max(numbers) + 1 if numbers else 1
            filename = f'{index}.webp'
            quality = 85
        filepath = get_product_media_dir(product_code) / filename
        img.save(filepath, 'WEBP', quality=quality)
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения фото: {e}")
        return False

async def delete_photo(product_code: str, photo_type: str, index: int = None) -> bool:
    if photo_type == 'preview':
        filename = 'preview.webp'
    elif photo_type == 'size':
        filename = 'size.webp'
    else:
        if index is None:
            return False
        filename = f'{index}.webp'
    filepath = get_product_media_dir(product_code) / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    if photo_type == 'main':
        base_dir = get_product_media_dir(product_code)
        main_files = sorted([p for p in base_dir.glob('[0-9]*.webp') if p.stem.isdigit()], key=lambda x: int(x.stem))
        new_index = 1
        for p in main_files:
            current_num = int(p.stem)
            if current_num != new_index:
                new_path = base_dir / f'{new_index}.webp'
                p.rename(new_path)
            new_index += 1
    return True

def get_product_photos(product_code: str):
    base = get_product_media_dir(product_code)
    if not base.exists():
        return {'preview': None, 'size': None, 'main': []}
    return {
        'preview': str(base / 'preview.webp') if (base / 'preview.webp').exists() else None,
        'size': str(base / 'size.webp') if (base / 'size.webp').exists() else None,
        'main': sorted([str(p) for p in base.glob('[0-9]*.webp') if p.stem.isdigit()], key=lambda x: int(Path(x).stem))
    }

async def move_media_folder(old_code: str, new_code: str) -> bool:
    old_path = get_product_media_dir(old_code)
    new_path = get_product_media_dir(new_code)
    if old_path.exists():
        if new_path.exists():
            for item in old_path.iterdir():
                shutil.move(str(item), str(new_path / item.name))
            old_path.rmdir()
        else:
            shutil.move(str(old_path), str(new_path))
        return True
    return False

async def delete_media_folder(product_code: str):
    path = get_product_media_dir(product_code)
    if path.exists():
        shutil.rmtree(path)

async def send_product_photos(chat_id: int, product_code: str, context: ContextTypes.DEFAULT_TYPE):
    photos = get_product_photos(product_code)
    media_group = []
    if photos['preview']:
        media_group.append(InputMediaPhoto(media=open(photos['preview'], 'rb'), caption="🖼️ Preview"))
    for main_path in photos['main']:
        media_group.append(InputMediaPhoto(media=open(main_path, 'rb')))
    if photos['size']:
        media_group.append(InputMediaPhoto(media=open(photos['size'], 'rb'), caption="📐 Схема (size)"))
    if media_group:
        for i in range(0, len(media_group), 10):
            await context.bot.send_media_group(chat_id=chat_id, media=media_group[i:i+10])
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ У этого товара нет фото.")

# ------------------------- Работа с фото материалов -------------------------
def get_material_photo_path(material_code: str) -> Path:
    return MATERIALS_MEDIA / f"{material_code}.webp"

async def save_material_photo(file, material_code: str) -> bool:
    try:
        photo_bytes = await file.download_as_bytearray()
        img = Image.open(io.BytesIO(photo_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = rgb_img
        filepath = get_material_photo_path(material_code)
        img.save(filepath, 'WEBP', quality=85)
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения фото материала: {e}")
        return False

async def delete_material_photo(material_code: str):
    path = get_material_photo_path(material_code)
    if path.exists():
        path.unlink()

async def move_material_photo(old_code: str, new_code: str):
    old_path = get_material_photo_path(old_code)
    new_path = get_material_photo_path(new_code)
    if old_path.exists():
        old_path.rename(new_path)

async def send_material_photo(chat_id: int, material_code: str, context: ContextTypes.DEFAULT_TYPE):
    path = get_material_photo_path(material_code)
    if path.exists():
        with open(path, 'rb') as f:
            await context.bot.send_photo(chat_id=chat_id, photo=f)
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ У материала нет фото.")

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
        [InlineKeyboardButton("📦 Товары", callback_data="menu_products")],
        [InlineKeyboardButton("🏷️ Флаги / Теги", callback_data="menu_flags")],
        [InlineKeyboardButton("📂 Типы товаров", callback_data="menu_prod_types")],
        [InlineKeyboardButton("🧱 Типы материалов", callback_data="menu_mat_types")],
        [InlineKeyboardButton("🧰 Материалы", callback_data="menu_materials")],
        [InlineKeyboardButton("📊 Основные характеристики", callback_data="menu_main_features")],
        [InlineKeyboardButton("🔍 Поиск товаров", callback_data="menu_search")],
    ]
    await send_or_edit_message(update, context, "🔧 *Главное меню*",
                               InlineKeyboardMarkup(keyboard), new_message)

# Функции для безопасного выхода из разговоров
async def cancel_to_product_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает текущий разговор и показывает редактирование товара."""
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def exit_to_materials_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выход из разговора в список материалов."""
    await materials_page_callback(update, context)
    return ConversationHandler.END

async def exit_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выход из разговора в главное меню."""
    await main_menu_callback(update, context)
    return ConversationHandler.END

# ------------------------- Управление флагами -------------------------
async def show_flags(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    flags, total = await db.get_flags(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        flags, total = await db.get_flags(offset, limit)
    text = "*Флаги / Теги*\n\n"
    if not flags:
        text += "Нет флагов."
    else:
        for f in flags:
            text += f"`{f['id']}` – {f['name']}\n"
    text += f"\nСтраница {page+1} из {total_pages}"
    keyboard = []
    for f in flags:
        keyboard.append([InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"flag_edit_{f['id']}_{page}"),
                         InlineKeyboardButton("🗑️", callback_data=f"flag_del_confirm_{f['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"flags_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"flags_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить флаг", callback_data="flag_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["flags_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def flags_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_flags(update, context, page)

async def add_flag_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите название флага:", reply_markup=CANCEL_KEYBOARD)
    return ADD_FLAG_NAME

async def add_flag_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    await db.create_flag(name)
    await update.message.reply_text("✅ Флаг добавлен.")
    await show_flags(update, context, context.user_data.get("flags_page", 0))
    return ConversationHandler.END

async def edit_flag_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, flag_id, page = query.data.split("_")
    context.user_data["edit_flag_id"] = int(flag_id)
    context.user_data["edit_flag_page"] = int(page)
    await query.edit_message_text("Введите новое название флага:", reply_markup=CANCEL_KEYBOARD)
    return ADD_FLAG_NAME

async def edit_flag_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    flag_id = context.user_data["edit_flag_id"]
    await db.update_flag(flag_id, new_name)
    await update.message.reply_text("✅ Название флага обновлено.")
    await show_flags(update, context, context.user_data.get("edit_flag_page", 0))
    return ConversationHandler.END

async def delete_flag_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    flag_id = int(parts[3])
    page = int(parts[4])
    context.user_data["del_flag_id"] = flag_id
    context.user_data["del_flag_page"] = page
    flags, _ = await db.get_flags(0, 1000)
    flag_name = next((f["name"] for f in flags if f["id"] == flag_id), "Неизвестно")
    keyboard = [[InlineKeyboardButton("✅ Да, удалить", callback_data="flag_del_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data=f"flags_page_{page}")]]
    await query.edit_message_text(f"Удалить флаг '{flag_name}'? Он будет снят со всех товаров.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_FLAG_CONFIRM

async def delete_flag_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    flag_id = context.user_data["del_flag_id"]
    page = context.user_data["del_flag_page"]
    await db.delete_flag(flag_id)
    await query.edit_message_text("✅ Флаг удалён.")
    await show_flags(update, context, page)
    return ConversationHandler.END

# ------------------------- Управление типами товаров -------------------------
async def show_prod_types(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    types, total = await db.get_product_types(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        types, total = await db.get_product_types(offset, limit)
    text = "*Типы товаров*\n\n"
    if not types:
        text += "Нет типов."
    else:
        for t in types:
            text += f"`{t['id']}` – {t['name']}\n"
    text += f"\nСтраница {page+1} из {total_pages}"
    keyboard = []
    for t in types:
        keyboard.append([InlineKeyboardButton(f"✏️ {t['name']}", callback_data=f"prodtype_edit_{t['id']}_{page}"),
                         InlineKeyboardButton("🗑️", callback_data=f"prodtype_del_confirm_{t['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"prodtypes_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"prodtypes_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить тип", callback_data="prodtype_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["prodtypes_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def prodtypes_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_prod_types(update, context, page)

async def add_prodtype_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите название типа товара:", reply_markup=CANCEL_KEYBOARD)
    return ADD_PROD_TYPE_NAME

async def add_prodtype_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    await db.create_product_type(name)
    await update.message.reply_text("✅ Тип товара добавлен.")
    await show_prod_types(update, context, context.user_data.get("prodtypes_page", 0))
    return ConversationHandler.END

async def edit_prodtype_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, type_id, page = query.data.split("_")
    context.user_data["edit_prodtype_id"] = int(type_id)
    context.user_data["edit_prodtype_page"] = int(page)
    await query.edit_message_text("Введите новое название типа товара:", reply_markup=CANCEL_KEYBOARD)
    return ADD_PROD_TYPE_NAME

async def edit_prodtype_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    type_id = context.user_data["edit_prodtype_id"]
    await db.update_product_type(type_id, new_name)
    await update.message.reply_text("✅ Название типа товара обновлено.")
    await show_prod_types(update, context, context.user_data.get("edit_prodtype_page", 0))
    return ConversationHandler.END

async def delete_prodtype_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    type_id = int(parts[3])
    page = int(parts[4])
    context.user_data["del_prodtype_id"] = type_id
    context.user_data["del_prodtype_page"] = page
    types, _ = await db.get_product_types(0, 1000)
    type_name = next((t["name"] for t in types if t["id"] == type_id), "Неизвестно")
    keyboard = [[InlineKeyboardButton("✅ Да, удалить ВСЁ", callback_data="prodtype_del_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data=f"prodtypes_page_{page}")]]
    await query.edit_message_text(f"⚠️ Удалить тип '{type_name}'? Все товары этого типа будут удалены безвозвратно!",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_PROD_TYPE_CONFIRM

async def delete_prodtype_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    type_id = context.user_data["del_prodtype_id"]
    page = context.user_data["del_prodtype_page"]
    success = await db.delete_product_type_cascade(type_id)
    await query.edit_message_text("✅ Тип и все его товары удалены." if success else "❌ Ошибка при удалении типа.")
    await show_prod_types(update, context, page)
    return ConversationHandler.END

# ------------------------- Управление типами материалов -------------------------
async def show_mat_types(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    types, total = await db.get_material_types(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        types, total = await db.get_material_types(offset, limit)
    text = "*Типы материалов*\n\n"
    if not types:
        text += "Нет типов материалов."
    else:
        for t in types:
            text += f"`{t['id']}` – {t['name']}\n"
    text += f"\nСтраница {page+1} из {total_pages}"
    keyboard = []
    for t in types:
        keyboard.append([InlineKeyboardButton(f"✏️ {t['name']}", callback_data=f"mattype_edit_{t['id']}_{page}"),
                         InlineKeyboardButton("🗑️", callback_data=f"mattype_del_confirm_{t['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"mattypes_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"mattypes_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить тип материала", callback_data="mattype_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["mattypes_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def mattypes_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_mat_types(update, context, page)

async def add_mattype_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите название типа материала:", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAT_TYPE_NAME

async def add_mattype_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    await db.create_material_type(name)
    await update.message.reply_text("✅ Тип материала добавлен.")
    await show_mat_types(update, context, context.user_data.get("mattypes_page", 0))
    return ConversationHandler.END

async def edit_mattype_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, mt_id, page = query.data.split("_")
    context.user_data["edit_mattype_id"] = int(mt_id)
    context.user_data["edit_mattype_page"] = int(page)
    await query.edit_message_text("Введите новое название типа материала:", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAT_TYPE_NAME

async def edit_mattype_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    mt_id = context.user_data["edit_mattype_id"]
    await db.update_material_type(mt_id, new_name)
    await update.message.reply_text("✅ Название типа материала обновлено.")
    await show_mat_types(update, context, context.user_data.get("edit_mattype_page", 0))
    return ConversationHandler.END

async def delete_mattype_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    mt_id = int(parts[3])
    page = int(parts[4])
    context.user_data["del_mattype_id"] = mt_id
    context.user_data["del_mattype_page"] = page
    types, _ = await db.get_material_types(0, 1000)
    mt_name = next((t["name"] for t in types if t["id"] == mt_id), "Неизвестно")
    keyboard = [[InlineKeyboardButton("✅ Да, удалить", callback_data="mattype_del_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data=f"mattypes_page_{page}")]]
    await query.edit_message_text(f"Удалить тип материала '{mt_name}'? Все связанные стоимости будут удалены.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_MAT_TYPE_CONFIRM

async def delete_mattype_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mt_id = context.user_data["del_mattype_id"]
    page = context.user_data["del_mattype_page"]
    success = await db.delete_material_type_cascade(mt_id)
    await query.edit_message_text("✅ Тип материала и связанные стоимости удалены." if success else "❌ Ошибка при удалении.")
    await show_mat_types(update, context, page)
    return ConversationHandler.END

# ------------------------- Управление основными характеристиками (справочник) -------------------------
async def show_main_features(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    features, total = await db.get_main_feature_types(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        features, total = await db.get_main_feature_types(offset, limit)
    text = "*Основные характеристики (справочник)*\n\n"
    if not features:
        text += "Нет характеристик."
    else:
        for f in features:
            text += f"`{f['id']}` – {f['name']}\n"
    text += f"\nСтраница {page+1} из {total_pages}"
    keyboard = []
    for f in features:
        keyboard.append([InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"mainfeat_edit_{f['id']}_{page}"),
                         InlineKeyboardButton("🗑️", callback_data=f"mainfeat_del_confirm_{f['id']}_{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"mainfeat_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"mainfeat_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить характеристику", callback_data="mainfeat_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["mainfeat_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def mainfeat_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_main_features(update, context, page)

async def add_mainfeat_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите название основной характеристики:", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAIN_FEATURE_NAME

async def add_mainfeat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    await db.create_main_feature_type(name)
    await update.message.reply_text("✅ Характеристика добавлена.")
    await show_main_features(update, context, context.user_data.get("mainfeat_page", 0))
    return ConversationHandler.END

async def edit_mainfeat_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, feat_id, page = query.data.split("_")
    context.user_data["edit_mainfeat_id"] = int(feat_id)
    context.user_data["edit_mainfeat_page"] = int(page)
    await query.edit_message_text("Введите новое название характеристики:", reply_markup=CANCEL_KEYBOARD)
    return EDIT_MAIN_FEATURE_NAME

async def edit_mainfeat_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    feat_id = context.user_data["edit_mainfeat_id"]
    await db.update_main_feature_type(feat_id, new_name)
    await update.message.reply_text("✅ Название характеристики обновлено.")
    await show_main_features(update, context, context.user_data.get("edit_mainfeat_page", 0))
    return ConversationHandler.END

async def delete_mainfeat_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    feat_id = int(parts[3])
    page = int(parts[4])
    context.user_data["del_mainfeat_id"] = feat_id
    context.user_data["del_mainfeat_page"] = page
    features, _ = await db.get_main_feature_types(0, 1000)
    feat_name = next((f["name"] for f in features if f["id"] == feat_id), "Неизвестно")
    keyboard = [[InlineKeyboardButton("✅ Да, удалить", callback_data="mainfeat_del_yes")],
                [InlineKeyboardButton("❌ Нет", callback_data=f"mainfeat_page_{page}")]]
    await query.edit_message_text(f"Удалить характеристику '{feat_name}'? Значения для всех товаров будут удалены.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_MAIN_FEATURE_CONFIRM

async def delete_mainfeat_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    feat_id = context.user_data["del_mainfeat_id"]
    page = context.user_data["del_mainfeat_page"]
    await db.delete_main_feature_type(feat_id)
    await query.edit_message_text("✅ Характеристика удалена.")
    await show_main_features(update, context, page)
    return ConversationHandler.END

# ------------------------- Управление материалами -------------------------
async def show_materials(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    materials, total = await db.get_materials(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        materials, total = await db.get_materials(offset, limit)
    text = "*Материалы*\n\n"
    if not materials:
        text += "Нет материалов."
    else:
        for m in materials:
            text += f"`{m['id']}` – {m['name']} (код: {m['code']}, тип: {m['type_name']})\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"
    keyboard = []
    for m in materials:
        keyboard.append([
            InlineKeyboardButton(f"📄 {m['name']}", callback_data=f"mat_details_{m['id']}_{page}"),
            InlineKeyboardButton("✏️", callback_data=f"mat_edit_{m['id']}_{page}"),
            InlineKeyboardButton("🗑️", callback_data=f"mat_del_confirm_{m['id']}_{page}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"materials_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"materials_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить материал", callback_data="mat_add")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    context.user_data["materials_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def materials_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await show_materials(update, context, page)

async def material_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, mat_id, page = query.data.split("_")
    mat_id = int(mat_id)
    page = int(page)
    material = await db.get_material_by_id(mat_id)
    if not material:
        await query.edit_message_text("❌ Материал не найден.")
        return
    text = (f"*Материал ID {material['id']}*\n\n"
            f"📛 *Название:* {material['name']}\n"
            f"🔢 *Код:* {material['code']}\n"
            f"📂 *Тип:* {material['type_name']} (id {material['materials_type_id']})")
    keyboard = [
        [InlineKeyboardButton("🖼️ Посмотреть фото", callback_data=f"mat_view_photo_{mat_id}_{page}")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"mat_edit_{mat_id}_{page}")],
        [InlineKeyboardButton("◀ К списку материалов", callback_data=f"materials_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def material_view_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, _, mat_id, page = query.data.split("_")
    mat_id = int(mat_id)
    page = int(page)
    material = await db.get_material_by_id(mat_id)
    if material:
        await send_material_photo(update.effective_chat.id, material['code'], context)
    else:
        await query.edit_message_text("❌ Материал не найден.")
    await material_details(update, context)

async def add_material_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    types, _ = await db.get_material_types(0, 1000)
    if not types:
        await update.callback_query.edit_message_text("❌ Нет типов материалов. Сначала создайте тип материала.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"mat_add_type_{t['id']}")] for t in types]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    await update.callback_query.edit_message_text("Выберите тип материала:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_MAT_TYPE_SELECT

async def add_material_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    type_id = int(query.data.split("_")[-1])
    context.user_data["new_mat_type_id"] = type_id
    await query.edit_message_text("Введите *уникальный код* материала:", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAT_CODE

async def add_material_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    if not await db.is_material_code_unique(code):
        await update.message.reply_text("❌ Материал с таким кодом уже существует. Введите другой код:", reply_markup=CANCEL_KEYBOARD)
        return ADD_MAT_CODE
    context.user_data["new_mat_code"] = code
    await update.message.reply_text("Введите *название* материала:", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAT_NAME

async def add_material_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    context.user_data["new_mat_name"] = name
    await update.message.reply_text("Теперь отправьте *фото* материала (одно изображение).", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_MAT_PHOTO

async def add_material_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Пожалуйста, отправьте фото.", reply_markup=CANCEL_KEYBOARD)
        return ADD_MAT_PHOTO
    try:
        photo_file = await download_photo_with_retry(update.message.photo[-1])
        code = context.user_data["new_mat_code"]
        success = await save_material_photo(photo_file, code)
        if not success:
            await update.message.reply_text("❌ Ошибка при сохранении фото. Попробуйте ещё раз.")
            return ADD_MAT_PHOTO
        mat_id = await db.create_material(code, context.user_data["new_mat_name"], context.user_data["new_mat_type_id"])
        await update.message.reply_text(f"✅ Материал создан, ID = {mat_id}.")
        await show_materials(update, context, 0)
    except Exception as e:
        logger.error(f"Ошибка добавления материала: {e}")
        await update.message.reply_text("❌ Ошибка при создании материала.")
    return ConversationHandler.END

async def material_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, mat_id, page = query.data.split("_")
    mat_id = int(mat_id)
    page = int(page)
    context.user_data["edit_mat_id"] = mat_id
    context.user_data["edit_mat_page"] = page
    material = await db.get_material_by_id(mat_id)
    if not material:
        await query.edit_message_text("❌ Материал не найден.")
        return ConversationHandler.END
    text = (f"*Редактирование материала ID {mat_id}*\n\n"
            f"📛 *Название:* {material['name']}\n"
            f"🔢 *Код:* {material['code']}\n"
            f"📂 *Тип:* {material['type_name']} (id {material['materials_type_id']})")
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data="mat_edit_name"),
         InlineKeyboardButton("✏️ Код", callback_data="mat_edit_code")],
        [InlineKeyboardButton("✏️ Тип материала", callback_data="mat_edit_type"),
         InlineKeyboardButton("🖼️ Фото", callback_data="mat_edit_photo")],
        [InlineKeyboardButton("🗑️ Удалить материал", callback_data="mat_edit_delete")],
        [InlineKeyboardButton("◀ К списку материалов", callback_data=f"materials_page_{page}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return EDIT_MAT_SELECT

async def material_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое название материала:", reply_markup=CANCEL_KEYBOARD)
    return EDIT_MAT_NAME

async def material_update_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    mat_id = context.user_data["edit_mat_id"]
    material = await db.get_material_by_id(mat_id)
    await db.update_material(mat_id, material['code'], new_name, material['materials_type_id'])
    await update.message.reply_text("✅ Название обновлено.")
    await show_materials(update, context, context.user_data["edit_mat_page"])
    return ConversationHandler.END

async def material_edit_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый код материала (уникальный):", reply_markup=CANCEL_KEYBOARD)
    return EDIT_MAT_CODE

async def material_update_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    mat_id = context.user_data["edit_mat_id"]
    material = await db.get_material_by_id(mat_id)
    if not await db.is_material_code_unique(new_code, exclude_id=mat_id):
        await update.message.reply_text("❌ Код уже существует. Введите другой:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_MAT_CODE
    old_code = material['code']
    await db.update_material(mat_id, new_code, material['name'], material['materials_type_id'])
    await move_material_photo(old_code, new_code)
    await update.message.reply_text("✅ Код обновлён, фото переименовано.")
    await show_materials(update, context, context.user_data["edit_mat_page"])
    return ConversationHandler.END

async def material_edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    types, _ = await db.get_material_types(0, 1000)
    if not types:
        await query.edit_message_text("❌ Нет типов материалов. Сначала создайте тип.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"mat_upd_type_{t['id']}")] for t in types]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    await query.edit_message_text("Выберите новый тип материала:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_MAT_TYPE

async def material_update_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_type_id = int(query.data.split("_")[-1])
    mat_id = context.user_data["edit_mat_id"]
    material = await db.get_material_by_id(mat_id)
    await db.update_material(mat_id, material['code'], material['name'], new_type_id)
    await query.edit_message_text("✅ Тип материала обновлён.")
    await show_materials(update, context, context.user_data["edit_mat_page"])
    return ConversationHandler.END

async def material_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Отправьте новое фото для материала (одно изображение).", reply_markup=CANCEL_KEYBOARD)
    return EDIT_MAT_PHOTO

async def material_update_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Отправьте фото.", reply_markup=CANCEL_KEYBOARD)
        return EDIT_MAT_PHOTO
    try:
        photo_file = await download_photo_with_retry(update.message.photo[-1])
        mat_id = context.user_data["edit_mat_id"]
        material = await db.get_material_by_id(mat_id)
        await delete_material_photo(material['code'])
        success = await save_material_photo(photo_file, material['code'])
        if success:
            await update.message.reply_text("✅ Фото обновлено.")
        else:
            await update.message.reply_text("❌ Ошибка сохранения фото.")
    except Exception as e:
        logger.error(f"Ошибка обновления фото материала: {e}")
        await update.message.reply_text("❌ Не удалось обновить фото.")
    await show_materials(update, context, context.user_data["edit_mat_page"])
    return ConversationHandler.END

async def material_edit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mat_id = context.user_data["edit_mat_id"]
    material = await db.get_material_by_id(mat_id)
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="mat_del_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"materials_page_{context.user_data['edit_mat_page']}")]
    ]
    await query.edit_message_text(f"Удалить материал *{material['name']}* (ID {mat_id}) навсегда?",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return DELETE_MAT_CONFIRM

async def material_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mat_id = context.user_data["edit_mat_id"]
    material = await db.get_material_by_id(mat_id)
    if material:
        try:
            await db.delete_material(mat_id)
            await delete_material_photo(material['code'])
            await query.edit_message_text("✅ Материал удалён.")
        except ValueError as e:
            await query.edit_message_text(f"❌ {e}")
    else:
        await query.edit_message_text("❌ Материал не найден.")
    await show_materials(update, context, context.user_data["edit_mat_page"])
    return ConversationHandler.END

# ------------------------- Основные характеристики товара -------------------------
async def product_main_features_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["mainfeat_prod_id"] = prod_id
    features = await db.get_product_main_features(prod_id)
    text = "*Основные характеристики товара*\n\n"
    keyboard = []
    for f in features:
        value_display = f['value'][:40] + "…" if len(f['value']) > 40 else f['value']
        text += f"• **{f['feature_name']}**: {value_display}\n"
        keyboard.append([InlineKeyboardButton(f"✏️ {f['feature_name']}", callback_data=f"prod_mainfeat_edit_{f['feature_id']}_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_MAIN_FEATURES

async def product_main_feature_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    feature_id = int(parts[3])
    prod_id = int(parts[4])
    context.user_data["edit_mainfeat_feature_id"] = feature_id
    context.user_data["edit_mainfeat_prod_id"] = prod_id
    await query.edit_message_text("Введите новое значение для этой характеристики:", reply_markup=CANCEL_KEYBOARD)
    return PROD_MAIN_FEATURE_EDIT

async def product_main_feature_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text
    prod_id = context.user_data["edit_mainfeat_prod_id"]
    feature_id = context.user_data["edit_mainfeat_feature_id"]
    await db.set_product_main_feature(prod_id, feature_id, value)
    await update.message.reply_text("✅ Значение сохранено.")
    # Показываем обновлённый список
    features = await db.get_product_main_features(prod_id)
    text = "*Основные характеристики товара*\n\n"
    keyboard = []
    for f in features:
        value_display = f['value'][:40] + "…" if len(f['value']) > 40 else f['value']
        text += f"• **{f['feature_name']}**: {value_display}\n"
        keyboard.append([InlineKeyboardButton(f"✏️ {f['feature_name']}", callback_data=f"prod_mainfeat_edit_{f['feature_id']}_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_MAIN_FEATURES

# ------------------------- Дополнительные характеристики товара -------------------------
async def product_extra_features_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["extra_prod_id"] = prod_id
    features = await db.get_product_extra_features(prod_id)
    text = "*Дополнительные характеристики*\n\n"
    keyboard = []
    for f in features:
        text += f"• **{f['name']}**: {f['value']}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"prod_extra_edit_{f['id']}_{prod_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_extra_del_{f['id']}_{prod_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data=f"prod_extra_add_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_EXTRA_FEATURES

async def product_extra_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["extra_prod_id"] = prod_id
    await query.edit_message_text("Введите название дополнительной характеристики:", reply_markup=CANCEL_KEYBOARD)
    return PROD_EXTRA_FEATURE_ADD_NAME

async def product_extra_add_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text
    context.user_data["extra_new_name"] = name
    await update.message.reply_text("Теперь введите значение:", reply_markup=CANCEL_KEYBOARD)
    return PROD_EXTRA_FEATURE_ADD_VALUE

async def product_extra_save_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text
    name = context.user_data["extra_new_name"]
    prod_id = context.user_data["extra_prod_id"]
    await db.add_product_extra_feature(prod_id, name, value)
    await update.message.reply_text("✅ Характеристика добавлена.")
    features = await db.get_product_extra_features(prod_id)
    text = "*Дополнительные характеристики*\n\n"
    keyboard = []
    for f in features:
        text += f"• **{f['name']}**: {f['value']}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"prod_extra_edit_{f['id']}_{prod_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_extra_del_{f['id']}_{prod_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data=f"prod_extra_add_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_EXTRA_FEATURES

async def product_extra_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    feat_id = int(parts[3])
    prod_id = int(parts[4])
    context.user_data["edit_extra_id"] = feat_id
    context.user_data["edit_extra_prod_id"] = prod_id
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data="extra_edit_name")],
        [InlineKeyboardButton("✏️ Значение", callback_data="extra_edit_value")],
        [InlineKeyboardButton("◀ Отмена", callback_data=f"prod_extra_cancel_{prod_id}")],
    ]
    await query.edit_message_text("Что хотите отредактировать?", reply_markup=InlineKeyboardMarkup(keyboard))
    return PROD_EXTRA_FEATURES

async def product_extra_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое название:", reply_markup=CANCEL_KEYBOARD)
    return PROD_EXTRA_FEATURE_EDIT_NAME

async def product_extra_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новое значение:", reply_markup=CANCEL_KEYBOARD)
    return PROD_EXTRA_FEATURE_EDIT_VALUE

async def product_extra_update_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    feat_id = context.user_data["edit_extra_id"]
    await db.update_product_extra_feature(feat_id, name=new_name)
    await update.message.reply_text("✅ Название обновлено.")
    prod_id = context.user_data["edit_extra_prod_id"]
    features = await db.get_product_extra_features(prod_id)
    text = "*Дополнительные характеристики*\n\n"
    keyboard = []
    for f in features:
        text += f"• **{f['name']}**: {f['value']}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"prod_extra_edit_{f['id']}_{prod_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_extra_del_{f['id']}_{prod_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data=f"prod_extra_add_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_EXTRA_FEATURES

async def product_extra_update_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_value = update.message.text
    feat_id = context.user_data["edit_extra_id"]
    await db.update_product_extra_feature(feat_id, value=new_value)
    await update.message.reply_text("✅ Значение обновлено.")
    prod_id = context.user_data["edit_extra_prod_id"]
    features = await db.get_product_extra_features(prod_id)
    text = "*Дополнительные характеристики*\n\n"
    keyboard = []
    for f in features:
        text += f"• **{f['name']}**: {f['value']}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"prod_extra_edit_{f['id']}_{prod_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_extra_del_{f['id']}_{prod_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data=f"prod_extra_add_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_EXTRA_FEATURES

async def product_extra_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    feat_id = int(parts[3])
    prod_id = int(parts[4])
    await db.delete_product_extra_feature(feat_id)
    features = await db.get_product_extra_features(prod_id)
    text = "*Дополнительные характеристики*\n\n"
    keyboard = []
    for f in features:
        text += f"• **{f['name']}**: {f['value']}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ {f['name']}", callback_data=f"prod_extra_edit_{f['id']}_{prod_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_extra_del_{f['id']}_{prod_id}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Добавить", callback_data=f"prod_extra_add_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Назад к товару", callback_data=f"prod_edit_cancel_{prod_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return PROD_EXTRA_FEATURES

async def product_extra_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

# ------------------------- Управление фото товаров -------------------------
async def show_photo_management(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int):
    product = await db.get_product_by_id(prod_id)
    if not product:
        await update.callback_query.edit_message_text("❌ Товар не найден.")
        return
    context.user_data["photo_prod_id"] = prod_id
    context.user_data["photo_prod_code"] = product["code"]
    keyboard = [
        [InlineKeyboardButton("➕ Добавить фото", callback_data="photo_add")],
        [InlineKeyboardButton("🗑️ Удалить фото", callback_data="photo_delete")],
        [InlineKeyboardButton("🖼️ Посмотреть фото", callback_data="photo_view")],
        [InlineKeyboardButton("◀ Назад к редактированию", callback_data=f"prod_edit_cancel_{prod_id}")],
    ]
    await send_or_edit_message(update, context, "📸 *Управление фото товара*\nВыберите действие:",
                               InlineKeyboardMarkup(keyboard), new_message=False)

async def manage_photos_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("prod_manage_photos_"):
        prod_id = int(data.split("_")[-1])
    else:
        prod_id = context.user_data.get("photo_prod_id")
        if prod_id is None:
            await query.edit_message_text("❌ Ошибка: не удалось определить товар.")
            return ConversationHandler.END
    await show_photo_management(update, context, prod_id)
    return PHOTO_MANAGE

async def photo_add_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🖼️ Preview", callback_data="photo_type_preview")],
        [InlineKeyboardButton("📐 Size (схема)", callback_data="photo_type_size")],
        [InlineKeyboardButton("🔢 Основное фото", callback_data="photo_type_main")],
    ]
    await query.edit_message_text("Выберите тип фото:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PHOTO_ADD_TYPE_SELECT

async def photo_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    photo_type = query.data.split("_")[-1]
    context.user_data["temp_photo_type"] = photo_type
    if photo_type == "main":
        context.user_data["main_photos_buffer"] = []
        # Сохраняем ID сообщения и chat_id для последующего удаления
        context.user_data["photo_main_chat_id"] = update.effective_chat.id
        context.user_data["photo_main_message_id"] = query.message.message_id
        await query.edit_message_text(
            "📸 *Добавление основных фото*\n\nОтправьте *одно или несколько фото* (можно альбомом).\n"
            "Когда закончите, *нажмите кнопку «Отправить фото»* для сохранения.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Отправить фото", callback_data="send_main_photos")],
                [InlineKeyboardButton("❌ Отменить", callback_data="cancel_main_photos")]
            ])
        )
        context.user_data["photo_main_message_id"] = query.message.message_id
        return PHOTO_ADD_MAIN_COLLECT
    else:
        await query.edit_message_text("Отправьте *одно фото* (будет сохранено как Preview или Size).",
                                      parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
        return PHOTO_ADD_WAIT

async def photo_add_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ Пожалуйста, отправьте фото.", reply_markup=CANCEL_KEYBOARD)
        return PHOTO_ADD_WAIT
    try:
        photo_file = await download_photo_with_retry(update.message.photo[-1])
        prod_code = context.user_data["photo_prod_code"]
        photo_type = context.user_data["temp_photo_type"]
        success = await save_photo_from_telegram(photo_file, prod_code, photo_type)
        await update.message.reply_text("✅ Фото сохранено." if success else "❌ Ошибка при сохранении фото.")
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        await update.message.reply_text("❌ Не удалось скачать фото. Попробуйте ещё раз.")
        return PHOTO_ADD_WAIT
    await show_product_edit(update, context, context.user_data["photo_prod_id"])
    return ConversationHandler.END

async def photo_add_main_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Обработка как фото, так и callback'ов
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data
        if data == "send_main_photos":
            # Удаляем исходное сообщение с кнопками
            chat_id = context.user_data.get("photo_main_chat_id")
            msg_id = context.user_data.get("photo_main_message_id")
            if chat_id and msg_id:
                try:
                    await context.bot.delete_message(chat_id, msg_id)
                except Exception as e:
                    logger.warning(f"Не удалось удалить сообщение: {e}")
            buffer = context.user_data.get("main_photos_buffer", [])
            if not buffer:
                await context.bot.send_message(chat_id=chat_id, text="❌ Нет фото для сохранения. Отправьте хотя бы одно фото.")
                await show_product_edit(update, context, context.user_data["photo_prod_id"])
                return ConversationHandler.END
            prod_code = context.user_data["photo_prod_code"]
            saved = 0
            for file_obj, _ in buffer:
                if await save_photo_from_telegram(file_obj, prod_code, "main"):
                    saved += 1
            context.user_data.pop("main_photos_buffer", None)
            context.user_data.pop("photo_main_chat_id", None)
            context.user_data.pop("photo_main_message_id", None)
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Сохранено основных фото: {saved}.")
            # Возврат в меню редактирования товара
            await show_product_edit(update, context, context.user_data["photo_prod_id"])
            return ConversationHandler.END
        elif data == "cancel_main_photos":
            chat_id = context.user_data.get("photo_main_chat_id")
            msg_id = context.user_data.get("photo_main_message_id")
            if chat_id and msg_id:
                try:
                    await context.bot.delete_message(chat_id, msg_id)
                except Exception as e:
                    logger.warning(f"Не удалось удалить сообщение: {e}")
            context.user_data.pop("main_photos_buffer", None)
            context.user_data.pop("photo_main_chat_id", None)
            context.user_data.pop("photo_main_message_id", None)
            await context.bot.send_message(chat_id=chat_id, text="❌ Добавление фото отменено.")
            await show_photo_management(update, context, context.user_data["photo_prod_id"])
            return PHOTO_MANAGE
        else:
            await query.edit_message_text("Неизвестная команда.")
            return PHOTO_ADD_MAIN_COLLECT
    # Обработка отправленных фото
    if not update.message.photo:
        # Если пришло не фото – напоминаем
        chat_id = update.effective_chat.id
        msg_id = context.user_data.get("photo_main_message_id")
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text="📸 *Добавление основных фото*\n\nОтправьте фото или нажмите кнопку «Отправить фото».",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Отправить фото", callback_data="send_main_photos")],
                        [InlineKeyboardButton("❌ Отменить", callback_data="cancel_main_photos")]
                    ])
                )
            except Exception:
                pass
        else:
            await update.message.reply_text("❌ Отправьте фото или используйте кнопки.",
                                            reply_markup=InlineKeyboardMarkup([
                                                [InlineKeyboardButton("✅ Отправить фото", callback_data="send_main_photos")],
                                                [InlineKeyboardButton("❌ Отменить", callback_data="cancel_main_photos")]
                                            ]))
        return PHOTO_ADD_MAIN_COLLECT
    try:
        photo_file = await download_photo_with_retry(update.message.photo[-1])
        context.user_data.setdefault("main_photos_buffer", []).append((photo_file, update.message))
        current_count = len(context.user_data["main_photos_buffer"])
        # Обновляем сообщение с кнопками
        chat_id = update.effective_chat.id
        msg_id = context.user_data.get("photo_main_message_id")
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id,
                    text=f"📸 *Добавление основных фото*\n\n📷 Фото добавлено. Всего в буфере: {current_count}.\n\n"
                         "Отправьте ещё фото или нажмите кнопку «Отправить фото» для сохранения.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Отправить фото", callback_data="send_main_photos")],
                        [InlineKeyboardButton("❌ Отменить", callback_data="cancel_main_photos")]
                    ])
                )
            except Exception as e:
                logger.warning(f"Не удалось обновить сообщение: {e}")
        else:
            await update.message.reply_text(f"📷 Фото добавлено. Всего в буфере: {current_count}.\n"
                                            "Нажмите кнопку «Отправить фото» для сохранения.",
                                            reply_markup=InlineKeyboardMarkup([
                                                [InlineKeyboardButton("✅ Отправить фото", callback_data="send_main_photos")],
                                                [InlineKeyboardButton("❌ Отменить", callback_data="cancel_main_photos")]
                                            ]))
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        await update.message.reply_text("❌ Не удалось скачать фото. Попробуйте ещё раз.")
    return PHOTO_ADD_MAIN_COLLECT

async def photo_delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_code = context.user_data["photo_prod_code"]
    prod_id = context.user_data["photo_prod_id"]
    photos = get_product_photos(prod_code)
    keyboard = []
    if photos['preview']:
        keyboard.append([InlineKeyboardButton("🗑️ Preview", callback_data="photo_del_preview")])
    if photos['size']:
        keyboard.append([InlineKeyboardButton("🗑️ Size (схема)", callback_data="photo_del_size")])
    for main_path in photos['main']:
        num = Path(main_path).stem
        keyboard.append([InlineKeyboardButton(f"🗑️ Основное фото #{num}", callback_data=f"photo_del_main_{num}")])
    if not any([photos['preview'], photos['size'], photos['main']]):
        await query.edit_message_text("Нет фото для удаления.")
        await show_photo_management(update, context, prod_id)
        return PHOTO_MANAGE
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data="photo_cancel")])
    await query.edit_message_text("Выберите фото для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
    return PHOTO_DELETE_SELECT

async def photo_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    prod_code = context.user_data["photo_prod_code"]
    if data == "photo_del_preview":
        await delete_photo(prod_code, "preview")
        await query.edit_message_text("✅ Preview удалён.")
    elif data == "photo_del_size":
        await delete_photo(prod_code, "size")
        await query.edit_message_text("✅ Size удалён.")
    elif data.startswith("photo_del_main_"):
        num = int(data.split("_")[-1])
        await delete_photo(prod_code, "main", num)
        await query.edit_message_text(f"✅ Основное фото #{num} удалено, остальные перенумерованы.")
    else:
        await query.edit_message_text("❌ Неизвестная команда.")
    await show_product_edit(update, context, context.user_data["photo_prod_id"])
    return ConversationHandler.END

async def photo_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_code = context.user_data.get("photo_prod_code")
    prod_id = context.user_data.get("photo_prod_id")
    if prod_code and prod_id:
        await send_product_photos(update.effective_chat.id, prod_code, context)
        await show_photo_management(update, context, prod_id)
    else:
        await query.edit_message_text("❌ Ошибка: данные товара не найдены.")
    return PHOTO_MANAGE

async def photo_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_product_edit(update, context, context.user_data["photo_prod_id"])
    return ConversationHandler.END

async def details_photos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    product = await db.get_product_by_id(prod_id)
    if product:
        await send_product_photos(update.effective_chat.id, product["code"], context)
    else:
        await query.edit_message_text("❌ Товар не найден.")
    await show_product_details(update, context, prod_id)

# ------------------------- Добавление товара, удаление, редактирование полей -------------------------
async def add_product_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Введите *название* товара:", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_PROD_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_prod_name"] = update.message.text
    await update.message.reply_text("Введите *код* товара (уникальный):", parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_PROD_CODE

async def add_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    if not await db.is_code_unique(code):
        await update.message.reply_text("❌ Товар с таким кодом уже существует. Введите другой код:", reply_markup=CANCEL_KEYBOARD)
        return ADD_PROD_CODE
    context.user_data["new_prod_code"] = code
    types, _ = await db.get_product_types(0, 1000)
    if not types:
        await update.message.reply_text("❌ Нет типов товаров. Сначала создайте тип товара через меню.")
        await show_main_menu(update, context, new_message=True)
        return ConversationHandler.END
    type_list = "\n".join(f"`{t['id']}` – {t['name']}" for t in types)
    await update.message.reply_text(f"Выберите тип товара, отправив его ID:\n\n{type_list}\n\nВведите *число* — ID типа:",
                                    parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return ADD_PROD_TYPE_ID

async def add_product_type_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        type_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка: нужно ввести целое число (ID типа). Попробуйте снова:", reply_markup=CANCEL_KEYBOARD)
        return ADD_PROD_TYPE_ID
    types, _ = await db.get_product_types(0, 1000)
    if not any(t['id'] == type_id for t in types):
        await update.message.reply_text("❌ Тип с таким ID не найден. Попробуйте снова:", reply_markup=CANCEL_KEYBOARD)
        return ADD_PROD_TYPE_ID
    prod_id = await db.create_product(context.user_data["new_prod_code"], context.user_data["new_prod_name"], type_id)
    ensure_media_dir(context.user_data["new_prod_code"])
    await update.message.reply_text(f"✅ Товар создан, ID = {prod_id}.")
    await show_product_edit(update, context, prod_id, new_message=True)
    return ConversationHandler.END

async def delete_product_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    product = await db.get_product_by_id(prod_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден.")
        return
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"prod_del_yes_{prod_id}")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"prod_edit_cancel_{prod_id}")],
    ]
    await query.edit_message_text(f"Удалить товар *{product['name']}* (ID {prod_id}) навсегда?",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def delete_product_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    product = await db.get_product_by_id(prod_id)
    if product:
        await delete_media_folder(product["code"])
    await db.delete_product_cascade(prod_id)
    await query.edit_message_text("✅ Товар удалён вместе со всеми связями (флаги, цены, фото, характеристики).")
    await show_products(update, context)

async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_edit(update, context, prod_id)

async def edit_product_name_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    await query.edit_message_text("Введите новое название товара:", reply_markup=CANCEL_KEYBOARD)
    return EDIT_PROD_NAME

async def edit_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    prod_id = context.user_data["edit_prod_id"]
    prod = await db.get_product_by_id(prod_id)
    await db.update_product(prod_id, prod["code"], new_name, prod["products_type_id"])
    await update.message.reply_text("✅ Название обновлено.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_code_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    await query.edit_message_text("Введите новый код товара:", reply_markup=CANCEL_KEYBOARD)
    return EDIT_PROD_CODE

async def edit_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_code = update.message.text.strip()
    prod_id = context.user_data["edit_prod_id"]
    prod = await db.get_product_by_id(prod_id)
    if not await db.is_code_unique(new_code, exclude_product_id=prod_id):
        await update.message.reply_text("❌ Товар с таким кодом уже существует. Введите другой код:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_PROD_CODE
    old_code = prod["code"]
    await db.update_product(prod_id, new_code, prod["name"], prod["products_type_id"])
    await move_media_folder(old_code, new_code)
    await update.message.reply_text("✅ Код обновлён, папка с фото переименована.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_type_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    types, _ = await db.get_product_types(0, 1000)
    if not types:
        await query.edit_message_text("Нет типов товаров. Сначала создайте хотя бы один.")
        return ConversationHandler.END
    type_list = "\n".join(f"`{t['id']}` – {t['name']}" for t in types)
    await query.edit_message_text(f"Выберите новый тип товара, отправив его ID:\n\n{type_list}\n\nВведите *число* — ID типа:",
                                  parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD)
    return EDIT_PROD_TYPE_ID

async def edit_product_type_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        type_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Ошибка: нужно ввести целое число (ID типа). Попробуйте снова:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_PROD_TYPE_ID
    types, _ = await db.get_product_types(0, 1000)
    if not any(t['id'] == type_id for t in types):
        await update.message.reply_text("❌ Тип с таким ID не найден. Попробуйте снова:", reply_markup=CANCEL_KEYBOARD)
        return EDIT_PROD_TYPE_ID
    prod_id = context.user_data["edit_prod_id"]
    prod = await db.get_product_by_id(prod_id)
    await db.update_product(prod_id, prod["code"], prod["name"], type_id)
    await update.message.reply_text("✅ Тип товара обновлён.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_flags_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    all_flags, _ = await db.get_flags(0, 1000)
    current_flags = await db.get_product_flags(prod_id)
    current_ids = {f["id"] for f in current_flags}
    context.user_data["prod_flags_current"] = current_ids
    keyboard = []
    for f in all_flags:
        checked = "✅ " if f["id"] in current_ids else "❌ "
        keyboard.append([InlineKeyboardButton(f"{checked}{f['name']}", callback_data=f"prod_toggle_flag_{f['id']}_{prod_id}")])
    keyboard.append([InlineKeyboardButton("💾 Сохранить и выйти", callback_data=f"prod_save_flags_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"prod_edit_cancel_{prod_id}")])
    await query.edit_message_text("Настройте флаги для этого товара (нажимайте для переключения):",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PROD_FLAGS

async def edit_product_toggle_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    flag_id = int(parts[3])
    prod_id = int(parts[4])
    current = context.user_data.get("prod_flags_current", set())
    if flag_id in current:
        current.remove(flag_id)
    else:
        current.add(flag_id)
    context.user_data["prod_flags_current"] = current
    all_flags, _ = await db.get_flags(0, 1000)
    keyboard = []
    for f in all_flags:
        checked = "✅ " if f["id"] in current else "❌ "
        keyboard.append([InlineKeyboardButton(f"{checked}{f['name']}", callback_data=f"prod_toggle_flag_{f['id']}_{prod_id}")])
    keyboard.append([InlineKeyboardButton("💾 Сохранить и выйти", callback_data=f"prod_save_flags_{prod_id}")])
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"prod_edit_cancel_{prod_id}")])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def edit_product_save_flags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    flag_ids = list(context.user_data.get("prod_flags_current", set()))
    await db.set_product_flags(prod_id, flag_ids)
    await query.edit_message_text("✅ Флаги сохранены.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_costs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    costs = await db.get_costs_for_product(prod_id)
    text = "💰 *Текущие стоимости:*\n"
    if costs:
        text += "\n".join(f"• {c['material_name']}: {c['cost']}₽ (id записи {c['id']})" for c in costs)
    else:
        text += "Нет добавленных стоимостей."
    keyboard = [
        [InlineKeyboardButton("➕ Добавить / изменить стоимость", callback_data=f"prod_cost_add_{prod_id}")],
        [InlineKeyboardButton("🗑️ Удалить стоимость", callback_data=f"prod_cost_delete_{prod_id}")],
        [InlineKeyboardButton("◀ Назад к редактированию", callback_data=f"prod_edit_cancel_{prod_id}")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PROD_COST_MENU

async def edit_product_cost_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    material_types, _ = await db.get_material_types(0, 1000)
    if not material_types:
        await query.edit_message_text("Нет типов материалов. Сначала создайте хотя бы один.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(mt['name'], callback_data=f"cost_add_type_{mt['id']}_{prod_id}")] for mt in material_types]
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"prod_cost_cancel_{prod_id}")])
    await query.edit_message_text("Выберите тип материала для добавления/изменения стоимости:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PROD_COST_ADD_TYPE

async def edit_product_cost_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    mt_id = int(parts[3])
    prod_id = int(parts[4])
    context.user_data["cost_add_mt_id"] = mt_id
    context.user_data["edit_prod_id"] = prod_id
    await query.edit_message_text("Введите стоимость в рублях (целое число):", reply_markup=CANCEL_KEYBOARD)
    return EDIT_PROD_COST_VALUE

async def edit_product_cost_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost = int(update.message.text.strip())
        if cost < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите положительное целое число.", reply_markup=CANCEL_KEYBOARD)
        return EDIT_PROD_COST_VALUE
    prod_id = context.user_data["edit_prod_id"]
    mt_id = context.user_data["cost_add_mt_id"]
    await db.set_cost(prod_id, mt_id, cost)
    await update.message.reply_text("✅ Стоимость сохранена.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_cost_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    context.user_data["edit_prod_id"] = prod_id
    costs = await db.get_costs_for_product(prod_id)
    if not costs:
        await query.edit_message_text("Нет записей стоимости для удаления.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"{c['material_name']} – {c['cost']}₽", callback_data=f"cost_del_{c['id']}_{prod_id}")] for c in costs]
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"prod_cost_cancel_{prod_id}")])
    await query.edit_message_text("Выберите запись стоимости для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_PROD_COST_DELETE

async def edit_product_cost_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    cost_id = int(parts[2])
    prod_id = int(parts[3])
    await db.delete_cost(cost_id)
    await query.edit_message_text("✅ Стоимость удалена.")
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

async def edit_product_cost_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_edit(update, context, prod_id)
    return ConversationHandler.END

# ------------------------- Отображение товаров с пагинацией -------------------------
async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        search: str = None, type_id: int = None, flag_ids: List[int] = None,
                        page: int = 0):
    PER_PAGE = 8
    offset = page * PER_PAGE
    products, total = await db.get_products(search=search, type_id=type_id, flag_ids=flag_ids,
                                            offset=offset, limit=PER_PAGE)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * PER_PAGE
        products, total = await db.get_products(search=search, type_id=type_id, flag_ids=flag_ids,
                                                offset=offset, limit=PER_PAGE)
    text = "*Список товаров*"
    if search:
        text += f" (поиск: `{search}`)"
    if type_id:
        t = await db.get_product_type_by_id(type_id)
        text += f" (тип: {t['name'] if t else type_id})"
    if flag_ids:
        text += f" (флаги: {len(flag_ids)})"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}\n\n"
    if not products:
        text += "Нет товаров, удовлетворяющих условиям."
    else:
        for p in products:
            text += f"`{p['id']}` – {p['name']} (код: {p['code']})\n"
    keyboard = []
    for p in products:
        keyboard.append([
            InlineKeyboardButton(f"📄 {p['name']} (ID {p['id']})", callback_data=f"prod_details_{p['id']}"),
            InlineKeyboardButton("✏️", callback_data=f"prod_edit_{p['id']}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"products_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"products_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить товар", callback_data="prod_add")])
    keyboard.append([InlineKeyboardButton("🔍 Новый поиск", callback_data="menu_search")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    context.user_data["products_search"] = search
    context.user_data["products_type_id"] = type_id
    context.user_data["products_flag_ids"] = flag_ids
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def products_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    search = context.user_data.get("products_search")
    type_id = context.user_data.get("products_type_id")
    flag_ids = context.user_data.get("products_flag_ids")
    await show_products(update, context, search=search, type_id=type_id, flag_ids=flag_ids, page=page)

async def product_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_details(update, context, prod_id)

async def product_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prod_id = int(query.data.split("_")[-1])
    await show_product_edit(update, context, prod_id)

async def show_product_details(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int, new_message: bool = False):
    product = await db.get_product_by_id(prod_id)
    if not product:
        await send_or_edit_message(update, context, "❌ Товар не найден.",
                                   InlineKeyboardMarkup([[InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")]]),
                                   new_message=True)
        return
    prod_type = await db.get_product_type_by_id(product["products_type_id"])
    type_name = prod_type["name"] if prod_type else "Неизвестно"
    flags = await db.get_product_flags(prod_id)
    flags_text = ", ".join(f["name"] for f in flags) if flags else "нет"
    costs = await db.get_costs_for_product(prod_id)
    costs_text = "\n".join(f"• {c['material_name']}: {c['cost']}₽" for c in costs) if costs else "нет"
    main_feats = await db.get_product_main_features(prod_id)
    main_feats_text = "\n".join(f"• {f['feature_name']}: {f['value']}" for f in main_feats if f['value']) if main_feats else "нет"
    extra_feats = await db.get_product_extra_features(prod_id)
    extra_feats_text = "\n".join(f"• {f['name']}: {f['value']}" for f in extra_feats) if extra_feats else "нет"

    text = (
        f"*Подробнее о товаре ID {prod_id}*\n\n"
        f"📛 *Название:* {product['name']}\n"
        f"🔢 *Код:* {product['code']}\n"
        f"📂 *Тип:* {type_name}\n"
        f"🏷️ *Флаги:* {flags_text}\n"
        f"💰 *Стоимости:*\n{costs_text}\n"
        f"📋 *Основные характеристики:*\n{main_feats_text}\n"
        f"➕ *Дополнительные характеристики:*\n{extra_feats_text}"
    )
    keyboard = [
        [InlineKeyboardButton("🖼️ Посмотреть фото", callback_data=f"details_photos_{prod_id}")],
        [InlineKeyboardButton("◀ К списку товаров", callback_data="menu_products")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message)

async def show_product_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int, new_message: bool = False):
    product = await db.get_product_by_id(prod_id)
    if not product:
        await send_or_edit_message(update, context, "❌ Товар не найден.",
                                   InlineKeyboardMarkup([[InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")]]),
                                   new_message=True)
        return
    prod_type = await db.get_product_type_by_id(product["products_type_id"])
    type_name = prod_type["name"] if prod_type else "Неизвестно"
    flags = await db.get_product_flags(prod_id)
    flags_text = ", ".join(f["name"] for f in flags) if flags else "нет"
    costs = await db.get_costs_for_product(prod_id)
    costs_text = "\n".join(f"• {c['material_name']}: {c['cost']}₽ (id {c['id']})" for c in costs) if costs else "нет"
    text = (
        f"*Редактирование товара ID {prod_id}*\n\n"
        f"📛 *Название:* {product['name']}\n"
        f"🔢 *Код:* {product['code']}\n"
        f"📂 *Тип:* {type_name} (id {product['products_type_id']})\n"
        f"🏷️ *Флаги:* {flags_text}\n"
        f"💰 *Стоимости:*\n{costs_text}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Название", callback_data=f"prod_edit_name_{prod_id}"),
         InlineKeyboardButton("✏️ Код", callback_data=f"prod_edit_code_{prod_id}")],
        [InlineKeyboardButton("✏️ Тип товара", callback_data=f"prod_edit_type_{prod_id}"),
         InlineKeyboardButton("🏷️ Флаги", callback_data=f"prod_edit_flags_{prod_id}")],
        [InlineKeyboardButton("💰 Управление стоимостью", callback_data=f"prod_edit_costs_{prod_id}"),
         InlineKeyboardButton("📸 Управление фото", callback_data=f"prod_manage_photos_{prod_id}")],
        [InlineKeyboardButton("📋 Основные характеристики", callback_data=f"prod_main_features_{prod_id}"),
         InlineKeyboardButton("➕ Доп. характеристики", callback_data=f"prod_extra_features_{prod_id}")],
        [InlineKeyboardButton("🗑️ Удалить товар", callback_data=f"prod_delete_confirm_{prod_id}")],
        [InlineKeyboardButton("◀ К списку товаров", callback_data="menu_products"),
         InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message)

# ------------------------- Расширенный поиск -------------------------
async def search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🔍 Поиск по ключевому слову", callback_data="search_keyword")],
        [InlineKeyboardButton("🏷️ Поиск по типу и флагам", callback_data="search_advanced")],
        [InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")],
    ]
    await query.edit_message_text("Выберите способ поиска:", reply_markup=InlineKeyboardMarkup(keyboard))

async def search_keyword_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔍 Введите ключевое слово для поиска (по названию или коду):",
                                                  reply_markup=CANCEL_KEYBOARD)
    return SEARCH_KEYWORD

async def search_keyword_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text
    await show_products(update, context, search=keyword, page=0)
    return ConversationHandler.END

async def search_advanced_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["search_type_id"] = None
    context.user_data["search_flag_ids"] = set()
    await show_type_selection(update, context)
    return SEARCH_TYPE_SELECT

async def show_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    types, total = await db.get_product_types(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        types, total = await db.get_product_types(offset, limit)
    selected_id = context.user_data.get("search_type_id")
    text = "*Выберите тип товара (один)*\n\n"
    if selected_id:
        sel_type = await db.get_product_type_by_id(selected_id)
        text += f"✅ Выбран: {sel_type['name'] if sel_type else 'ID '+str(selected_id)}\n\n"
    else:
        text += "❌ Тип не выбран (поиск по всем типам)\n\n"
    keyboard = []
    for t in types:
        mark = "✅ " if selected_id == t['id'] else "❌ "
        keyboard.append([InlineKeyboardButton(f"{mark}{t['name']}", callback_data=f"search_type_toggle_{t['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"search_type_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"search_type_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🚫 Пропустить (выбрать все типы)", callback_data="search_type_skip")])
    keyboard.append([InlineKeyboardButton("▶ Далее → выбор флагов", callback_data="search_type_next")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    context.user_data["search_type_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def search_type_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    type_id = int(query.data.split("_")[-1])
    current = context.user_data.get("search_type_id")
    context.user_data["search_type_id"] = None if current == type_id else type_id
    await show_type_selection(update, context, context.user_data.get("search_type_page", 0))

async def search_type_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search_type_id"] = None
    await show_type_selection(update, context, context.user_data.get("search_type_page", 0))

async def search_type_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_flag_selection(update, context)
    return SEARCH_FLAGS_SELECT

async def show_flag_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    limit = 8
    offset = page * limit
    flags, total = await db.get_flags(offset, limit)
    total_pages = (total + limit - 1) // limit if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
        offset = page * limit
        flags, total = await db.get_flags(offset, limit)
    selected_ids = context.user_data.get("search_flag_ids", set())
    text = "*Выберите флаги (можно несколько)*\n\n"
    if selected_ids:
        text += f"✅ Выбрано флагов: {len(selected_ids)}\n\n"
    else:
        text += "❌ Флаги не выбраны (поиск без фильтра по флагам)\n\n"
    keyboard = []
    for f in flags:
        mark = "✅ " if f['id'] in selected_ids else "❌ "
        keyboard.append([InlineKeyboardButton(f"{mark}{f['name']}", callback_data=f"search_flag_toggle_{f['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"search_flag_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"search_flag_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🚫 Сбросить все флаги", callback_data="search_flag_reset")])
    keyboard.append([InlineKeyboardButton("🔍 Поиск", callback_data="search_flag_finish")])
    keyboard.append([InlineKeyboardButton("◀ Назад к выбору типа", callback_data="search_back_to_type")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="main_menu")])
    context.user_data["search_flag_page"] = page
    await send_or_edit_message(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)

async def search_flag_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    flag_id = int(query.data.split("_")[-1])
    selected = context.user_data.get("search_flag_ids", set())
    if flag_id in selected:
        selected.remove(flag_id)
    else:
        selected.add(flag_id)
    context.user_data["search_flag_ids"] = selected
    await show_flag_selection(update, context, context.user_data.get("search_flag_page", 0))

async def search_flag_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["search_flag_ids"] = set()
    await show_flag_selection(update, context, context.user_data.get("search_flag_page", 0))

async def search_flag_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    type_id = context.user_data.get("search_type_id")
    flag_ids = list(context.user_data.get("search_flag_ids", set()))
    await show_products(update, context, search=None, type_id=type_id, flag_ids=flag_ids, page=0)
    context.user_data.pop("search_type_id", None)
    context.user_data.pop("search_flag_ids", None)
    return ConversationHandler.END

async def search_back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_type_selection(update, context, context.user_data.get("search_type_page", 0))
    return SEARCH_TYPE_SELECT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.edit_message_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    else:
        await update.message.reply_text("Операция отменена.")
        await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END

# ------------------------- Обработчики меню -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)

@admin_only
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

@admin_only
async def menu_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("products_search", None)
    context.user_data.pop("products_type_id", None)
    context.user_data.pop("products_flag_ids", None)
    await show_products(update, context)

@admin_only
async def menu_flags_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_flags(update, context, 0)

@admin_only
async def menu_prod_types_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_prod_types(update, context, 0)

@admin_only
async def menu_mat_types_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mat_types(update, context, 0)

@admin_only
async def menu_materials_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_materials(update, context, 0)

@admin_only
async def menu_main_features_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_features(update, context, 0)

@admin_only
async def menu_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await search_menu(update, context)

# ------------------------- Регистрация обработчиков -------------------------
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))

    # Главные колбэки
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(menu_products_callback, pattern="^menu_products$"))
    app.add_handler(CallbackQueryHandler(menu_flags_callback, pattern="^menu_flags$"))
    app.add_handler(CallbackQueryHandler(menu_prod_types_callback, pattern="^menu_prod_types$"))
    app.add_handler(CallbackQueryHandler(menu_mat_types_callback, pattern="^menu_mat_types$"))
    app.add_handler(CallbackQueryHandler(menu_materials_callback, pattern="^menu_materials$"))
    app.add_handler(CallbackQueryHandler(menu_main_features_callback, pattern="^menu_main_features$"))
    app.add_handler(CallbackQueryHandler(menu_search_callback, pattern="^menu_search$"))

    # Пагинация товаров
    app.add_handler(CallbackQueryHandler(products_page_callback, pattern="^products_page_\\d+$"))
    # Просмотр и редактирование товаров
    app.add_handler(CallbackQueryHandler(product_details_callback, pattern="^prod_details_\\d+$"))
    app.add_handler(CallbackQueryHandler(product_edit_callback, pattern="^prod_edit_\\d+$"))

    # Добавление товара
    add_prod_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_product_entry, pattern="^prod_add$")],
        states={
            ADD_PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_PROD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_code)],
            ADD_PROD_TYPE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_type_id)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
            CallbackQueryHandler(cancel, pattern="^menu_flags$"),
            CallbackQueryHandler(cancel, pattern="^menu_prod_types$"),
            CallbackQueryHandler(cancel, pattern="^menu_mat_types$"),
            CallbackQueryHandler(cancel, pattern="^menu_materials$"),
            CallbackQueryHandler(cancel, pattern="^menu_main_features$"),
            CallbackQueryHandler(cancel, pattern="^menu_search$"),
        ],
        per_message=False,
    )
    app.add_handler(add_prod_conv)

    # Редактирование названия, кода, типа товара
    edit_name_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_product_name_entry, pattern="^prod_edit_name_\\d+$")],
        states={EDIT_PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_product_name)]},
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_name_conv)

    edit_code_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_product_code_entry, pattern="^prod_edit_code_\\d+$")],
        states={EDIT_PROD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_product_code)]},
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_code_conv)

    edit_type_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_product_type_entry, pattern="^prod_edit_type_\\d+$")],
        states={EDIT_PROD_TYPE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_product_type_id)]},
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_type_conv)

    # Редактирование флагов
    edit_flags_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_product_flags_start, pattern="^prod_edit_flags_\\d+$")],
        states={
            EDIT_PROD_FLAGS: [
                CallbackQueryHandler(edit_product_toggle_flag, pattern="^prod_toggle_flag_\\d+_\\d+$"),
                CallbackQueryHandler(edit_product_save_flags, pattern="^prod_save_flags_\\d+$"),
                # Обработка кнопки "Отмена"
                CallbackQueryHandler(cancel_to_product_edit, pattern="^prod_edit_cancel_\\d+$"),
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_flags_conv)

    # Управление стоимостью
    edit_cost_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_product_costs_menu, pattern="^prod_edit_costs_\\d+$")],
        states={
            EDIT_PROD_COST_MENU: [
                CallbackQueryHandler(edit_product_cost_add, pattern="^prod_cost_add_\\d+$"),
                CallbackQueryHandler(edit_product_cost_delete, pattern="^prod_cost_delete_\\d+$"),
            ],
            EDIT_PROD_COST_ADD_TYPE: [
                CallbackQueryHandler(edit_product_cost_type_selected, pattern="^cost_add_type_\\d+_\\d+$"),
                CallbackQueryHandler(edit_product_cost_cancel, pattern="^prod_cost_cancel_\\d+$"),
            ],
            EDIT_PROD_COST_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_product_cost_value)],
            EDIT_PROD_COST_DELETE: [
                CallbackQueryHandler(edit_product_cost_delete_execute, pattern="^cost_del_\\d+_\\d+$"),
                CallbackQueryHandler(edit_product_cost_cancel, pattern="^prod_cost_cancel_\\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_cost_conv)

    # Управление фото
    photo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(manage_photos_start, pattern="^prod_manage_photos_\\d+$")],
        states={
            PHOTO_MANAGE: [
                CallbackQueryHandler(photo_add_select_type, pattern="^photo_add$"),
                CallbackQueryHandler(photo_delete_select, pattern="^photo_delete$"),
                CallbackQueryHandler(photo_view, pattern="^photo_view$"),
            ],
            PHOTO_ADD_TYPE_SELECT: [CallbackQueryHandler(photo_type_selected, pattern="^photo_type_")],
            PHOTO_ADD_WAIT: [MessageHandler(filters.PHOTO, photo_add_wait)],
            PHOTO_ADD_MAIN_COLLECT: [
                MessageHandler(filters.PHOTO, photo_add_main_collect),
                CallbackQueryHandler(photo_add_main_collect, pattern="^(send_main_photos|cancel_main_photos)$"),
            ],
            PHOTO_DELETE_SELECT: [
                CallbackQueryHandler(photo_delete_execute, pattern="^photo_del_"),
                CallbackQueryHandler(photo_cancel, pattern="^photo_cancel$"),
                CallbackQueryHandler(cancel_to_product_edit, pattern="^prod_edit_cancel_\\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^main_menu$"),
            CallbackQueryHandler(cancel, pattern="^menu_products$"),
        ],
        per_message=False,
    )
    app.add_handler(photo_conv)

    # Просмотр фото из подробностей
    app.add_handler(CallbackQueryHandler(details_photos_callback, pattern="^details_photos_\\d+$"))

    # Удаление товара
    app.add_handler(CallbackQueryHandler(delete_product_confirm_callback, pattern="^prod_delete_confirm_\\d+$"))
    app.add_handler(CallbackQueryHandler(delete_product_execute, pattern="^prod_del_yes_\\d+$"))
    app.add_handler(CallbackQueryHandler(edit_cancel, pattern="^prod_edit_cancel_\\d+$"))

    # Управление флагами
    app.add_handler(CallbackQueryHandler(flags_page_callback, pattern="^flags_page_\\d+$"))
    add_flag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_flag_entry, pattern="^flag_add$")],
        states={ADD_FLAG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_flag_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(add_flag_conv)
    edit_flag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_flag_entry, pattern="^flag_edit_\\d+_\\d+$")],
        states={ADD_FLAG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_flag_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(edit_flag_conv)
    delete_flag_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_flag_confirm, pattern="^flag_del_confirm_\\d+_\\d+$")],
        states={DELETE_FLAG_CONFIRM: [CallbackQueryHandler(delete_flag_execute, pattern="^flag_del_yes$")]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_flag_conv)

    # Управление типами товаров
    app.add_handler(CallbackQueryHandler(prodtypes_page_callback, pattern="^prodtypes_page_\\d+$"))
    add_prodtype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_prodtype_entry, pattern="^prodtype_add$")],
        states={ADD_PROD_TYPE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_prodtype_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(add_prodtype_conv)
    edit_prodtype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_prodtype_entry, pattern="^prodtype_edit_\\d+_\\d+$")],
        states={ADD_PROD_TYPE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_prodtype_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(edit_prodtype_conv)
    delete_prodtype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_prodtype_confirm, pattern="^prodtype_del_confirm_\\d+_\\d+$")],
        states={DELETE_PROD_TYPE_CONFIRM: [CallbackQueryHandler(delete_prodtype_execute, pattern="^prodtype_del_yes$")]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_prodtype_conv)

    # Управление типами материалов
    app.add_handler(CallbackQueryHandler(mattypes_page_callback, pattern="^mattypes_page_\\d+$"))
    add_mattype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_mattype_entry, pattern="^mattype_add$")],
        states={ADD_MAT_TYPE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_mattype_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(add_mattype_conv)
    edit_mattype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_mattype_entry, pattern="^mattype_edit_\\d+_\\d+$")],
        states={ADD_MAT_TYPE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_mattype_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(edit_mattype_conv)
    delete_mattype_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_mattype_confirm, pattern="^mattype_del_confirm_\\d+_\\d+$")],
        states={DELETE_MAT_TYPE_CONFIRM: [CallbackQueryHandler(delete_mattype_execute, pattern="^mattype_del_yes$")]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_mattype_conv)

    # Управление основными характеристиками (справочник)
    app.add_handler(CallbackQueryHandler(mainfeat_page_callback, pattern="^mainfeat_page_\\d+$"))
    add_mainfeat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_mainfeat_entry, pattern="^mainfeat_add$")],
        states={ADD_MAIN_FEATURE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_mainfeat_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(add_mainfeat_conv)
    edit_mainfeat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_mainfeat_entry, pattern="^mainfeat_edit_\\d+_\\d+$")],
        states={EDIT_MAIN_FEATURE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_mainfeat_name)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(edit_mainfeat_conv)
    delete_mainfeat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(delete_mainfeat_confirm, pattern="^mainfeat_del_confirm_\\d+_\\d+$")],
        states={DELETE_MAIN_FEATURE_CONFIRM: [CallbackQueryHandler(delete_mainfeat_execute, pattern="^mainfeat_del_yes$")]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(delete_mainfeat_conv)

    # Основные характеристики товара
    prod_main_feat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(product_main_features_menu, pattern="^prod_main_features_\\d+$")],
        states={
            PROD_MAIN_FEATURES: [
                CallbackQueryHandler(product_main_feature_edit, pattern="^prod_mainfeat_edit_\\d+_\\d+$"),
                CallbackQueryHandler(cancel_to_product_edit, pattern="^prod_edit_cancel_\\d+$"),
            ],
            PROD_MAIN_FEATURE_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_main_feature_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(prod_main_feat_conv)

    # Дополнительные характеристики товара
    prod_extra_feat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(product_extra_features_menu, pattern="^prod_extra_features_\\d+$")],
        states={
            PROD_EXTRA_FEATURES: [
                CallbackQueryHandler(product_extra_add_name, pattern="^prod_extra_add_\\d+$"),
                CallbackQueryHandler(product_extra_edit_entry, pattern="^prod_extra_edit_\\d+_\\d+$"),
                CallbackQueryHandler(product_extra_delete, pattern="^prod_extra_del_\\d+_\\d+$"),
                CallbackQueryHandler(product_extra_cancel, pattern="^prod_extra_cancel_\\d+$"),
                CallbackQueryHandler(product_extra_edit_name, pattern="^extra_edit_name$"),
                CallbackQueryHandler(product_extra_edit_value, pattern="^extra_edit_value$"),
                CallbackQueryHandler(cancel_to_product_edit, pattern="^prod_edit_cancel_\\d+$"),
            ],
            PROD_EXTRA_FEATURE_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_extra_add_value)],
            PROD_EXTRA_FEATURE_ADD_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_extra_save_new)],
            PROD_EXTRA_FEATURE_EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_extra_update_name)],
            PROD_EXTRA_FEATURE_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_extra_update_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(prod_extra_feat_conv)

    # Поиск
    search_keyword_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_keyword_entry, pattern="^search_keyword$")],
        states={SEARCH_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_keyword_handle)]},
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(search_keyword_conv)

    search_advanced_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_advanced_entry, pattern="^search_advanced$")],
        states={
            SEARCH_TYPE_SELECT: [
                CallbackQueryHandler(search_type_toggle, pattern="^search_type_toggle_\\d+$"),
                CallbackQueryHandler(search_type_skip, pattern="^search_type_skip$"),
                CallbackQueryHandler(search_type_next, pattern="^search_type_next$"),
                CallbackQueryHandler(show_type_selection, pattern="^search_type_page_\\d+$"),
                CallbackQueryHandler(cancel, pattern="^main_menu$"),
            ],
            SEARCH_FLAGS_SELECT: [
                CallbackQueryHandler(search_flag_toggle, pattern="^search_flag_toggle_\\d+$"),
                CallbackQueryHandler(search_flag_reset, pattern="^search_flag_reset$"),
                CallbackQueryHandler(search_flag_finish, pattern="^search_flag_finish$"),
                CallbackQueryHandler(search_back_to_type, pattern="^search_back_to_type$"),
                CallbackQueryHandler(show_flag_selection, pattern="^search_flag_page_\\d+$"),
                CallbackQueryHandler(cancel, pattern="^main_menu$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(search_advanced_conv)

    # Управление материалами
    app.add_handler(CallbackQueryHandler(materials_page_callback, pattern="^materials_page_\\d+$"))
    app.add_handler(CallbackQueryHandler(material_details, pattern="^mat_details_\\d+_\\d+$"))
    app.add_handler(CallbackQueryHandler(material_view_photo, pattern="^mat_view_photo_\\d+_\\d+$"))

    add_material_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_material_entry, pattern="^mat_add$")],
        states={
            ADD_MAT_TYPE_SELECT: [CallbackQueryHandler(add_material_type_selected, pattern="^mat_add_type_\\d+$")],
            ADD_MAT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_material_code)],
            ADD_MAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_material_name)],
            ADD_MAT_PHOTO: [MessageHandler(filters.PHOTO, add_material_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(add_material_conv)

    edit_material_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(material_edit_entry, pattern="^mat_edit_\\d+_\\d+$")],
        states={
            EDIT_MAT_SELECT: [
                CallbackQueryHandler(material_edit_name, pattern="^mat_edit_name$"),
                CallbackQueryHandler(material_edit_code, pattern="^mat_edit_code$"),
                CallbackQueryHandler(material_edit_type, pattern="^mat_edit_type$"),
                CallbackQueryHandler(material_edit_photo, pattern="^mat_edit_photo$"),
                CallbackQueryHandler(material_edit_delete, pattern="^mat_edit_delete$"),
                # Выход в список материалов и главное меню
                CallbackQueryHandler(exit_to_materials_page, pattern="^materials_page_\\d+$"),
                CallbackQueryHandler(exit_to_main_menu, pattern="^main_menu$"),
            ],
            EDIT_MAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, material_update_name)],
            EDIT_MAT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, material_update_code)],
            EDIT_MAT_TYPE: [CallbackQueryHandler(material_update_type, pattern="^mat_upd_type_\\d+$")],
            EDIT_MAT_PHOTO: [MessageHandler(filters.PHOTO, material_update_photo)],
            DELETE_MAT_CONFIRM: [CallbackQueryHandler(material_delete_execute, pattern="^mat_del_yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="^main_menu$")],
        per_message=False,
    )
    app.add_handler(edit_material_conv)

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

    await application.initialize()
    await application.start()
    logging.info("Бот запущен с поддержкой материалов.")
    await application.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())