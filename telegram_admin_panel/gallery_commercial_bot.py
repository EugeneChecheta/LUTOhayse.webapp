#!/usr/bin/env python3
"""
Telegram‑бот "Галерея / Коммерческая галерея".
Версия 1.0

Функционал:
- Два независимых раздела: обычная галерея (media/gallery) и коммерческая галерея (media/commercial).
- Загрузка фото с подписью (подпись сохраняется отдельно для каждого фото).
- Подпись первого фото автоматически используется как описание проекта (если описание ещё не задано).
- Полный CRUD для проектов в обоих разделах:
    • список проектов (с пагинацией);
    • просмотр проекта (фото + описание);
    • создание нового проекта (имя = гггг-мм-дд-чч-мм-сс);
    • редактирование описания;
    • добавление новых фото (с подписью);
    • удаление отдельных фото (с автоматической перенумерацией);
    • удаление проекта целиком.
"""
import asyncio
import io
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest
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

# ----------------------- Пути и конфиг -----------------------
BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token_gallery_commercial.txt"
ADMIN_FILE = BASE_DIR / "admin_ids.txt"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def read_token() -> str:
    if not TOKEN_FILE.exists():
        logger.error(f"Файл с токеном не найден: {TOKEN_FILE}")
        sys.exit(1)
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except Exception as e:
        logger.error(f"Ошибка чтения токена: {e}")
        sys.exit(1)
    if not token:
        logger.error("Токен пустой")
        sys.exit(1)
    return token


def read_admin_ids() -> List[int]:
    if not ADMIN_FILE.exists():
        logger.warning(f"Файл admin_ids.txt не найден: {ADMIN_FILE}")
        return []
    ids: List[int] = []
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    ids.append(int(line))
    except Exception as e:
        logger.error(f"Ошибка чтения admin_ids.txt: {e}")
    return ids


TOKEN = read_token()
ADMIN_IDS = read_admin_ids()

# ----------------------- Медиа-папки -----------------------
MEDIA_BASE = BASE_DIR.parent / "media" if (BASE_DIR.parent / "media").exists() else BASE_DIR / "media"
GALLERY_BASE = MEDIA_BASE / "gallery"
COMMERCIAL_BASE = MEDIA_BASE / "commercial"
GALLERY_BASE.mkdir(parents=True, exist_ok=True)
COMMERCIAL_BASE.mkdir(parents=True, exist_ok=True)

# ----------------------- Состояния -----------------------
(
    STATE_CREATE_PHOTOS,
    STATE_EDIT_DESC,
    STATE_EDIT_ADD_PHOTOS,
) = range(3)

PROJECT_NAME_FORMAT = "%Y-%m-%d-%H-%M-%S"

CANCEL_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Отменить", callback_data="main_menu")]]
)


# ----------------------- Утилиты -----------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None:
            return ConversationHandler.END
        if ADMIN_IDS and user.id not in ADMIN_IDS:
            if update.callback_query:
                try:
                    await update.callback_query.answer("⛔ У вас нет прав.", show_alert=True)
                except Exception:
                    pass
            elif update.message:
                await update.message.reply_text("⛔ У вас нет прав на использование этого бота.")
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


async def safe_answer(query):
    try:
        await query.answer()
    except Exception:
        pass


async def send_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       text: str, keyboard: InlineKeyboardMarkup,
                       new_message: bool = False):
    """Пытается отредактировать сообщение-callback, иначе отправляет новое."""
    if update.callback_query and not new_message:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                try:
                    await update.callback_query.edit_message_reply_markup(reply_markup=keyboard)
                except Exception:
                    pass
                return
        except Exception as e:
            logger.debug(f"edit_message_text failed: {e}")
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                       reply_markup=keyboard)
    except Exception as e:
        logger.error(f"send_message failed: {e}")


# ----------------------- Работа с секциями -----------------------
def section_base(section: str) -> Path:
    return GALLERY_BASE if section == "gallery" else COMMERCIAL_BASE


def section_title(section: str) -> str:
    return "🖼️ Галерея" if section == "gallery" else "💼 Коммерческая галерея"


def get_projects(section: str) -> List[str]:
    base = section_base(section)
    if not base.exists():
        return []
    items = [p.name for p in base.iterdir() if p.is_dir()]
    items.sort(reverse=True)
    return items


def get_project_path(section: str, name: str) -> Path:
    return section_base(section) / name


def get_project_description(section: str, name: str) -> str:
    path = get_project_path(section, name) / "description.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def set_project_description(section: str, name: str, text: str):
    d = get_project_path(section, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "description.txt").write_text(text, encoding="utf-8")


def get_project_photo_paths(section: str, name: str) -> List[Path]:
    d = get_project_path(section, name)
    if not d.exists():
        return []
    files = [p for p in d.glob("*.webp") if p.stem.isdigit()]
    files.sort(key=lambda p: int(p.stem))
    return files


def get_project_captions(section: str, name: str) -> dict:
    path = get_project_path(section, name) / "captions.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_caption(section: str, name: str, index: int, caption: str):
    captions = get_project_captions(section, name)
    if caption:
        captions[str(index)] = caption
    else:
        captions.pop(str(index), None)
    path = get_project_path(section, name) / "captions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_unique_name(section: str) -> str:
    base = datetime.now().strftime(PROJECT_NAME_FORMAT)
    name = base
    counter = 1
    while get_project_path(section, name).exists():
        name = f"{base}-{counter}"
        counter += 1
        if counter > 1000:
            raise RuntimeError("Не удалось сгенерировать уникальное имя проекта")
    return name


async def download_photo_with_retry(photo, max_retries: int = 3, delay: float = 2.0):
    for attempt in range(max_retries):
        try:
            return await photo.get_file()
        except Exception as e:
            logger.warning(f"Попытка {attempt+1}/{max_retries} скачать фото не удалась: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(delay)
    return None


async def save_project_photo(file, section: str, name: str, caption: str = ""):
    """Сохраняет фото в проект. Возвращает номер фото или None при ошибке."""
    try:
        photo_bytes = await file.download_as_bytearray()
        img = Image.open(io.BytesIO(photo_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = rgb_img
        d = get_project_path(section, name)
        d.mkdir(parents=True, exist_ok=True)
        existing = get_project_photo_paths(section, name)
        next_num = (max(int(p.stem) for p in existing) + 1) if existing else 1
        filepath = d / f"{next_num}.webp"
        img.save(filepath, 'WEBP', quality=85)
        if caption:
            save_caption(section, name, next_num, caption)
        return next_num
    except Exception as e:
        logger.error(f"Ошибка сохранения фото: {e}")
        return None


async def delete_project_photo(section: str, name: str, index: int) -> bool:
    d = get_project_path(section, name)
    filepath = d / f"{index}.webp"
    if not filepath.exists():
        return False
    filepath.unlink()
    captions = get_project_captions(section, name)
    captions.pop(str(index), None)
    # Перенумерация
    files = get_project_photo_paths(section, name)
    new_caps: dict = {}
    new_index = 1
    for p in files:
        cur = int(p.stem)
        cap = captions.get(str(cur), "")
        if cur != new_index:
            p.rename(d / f"{new_index}.webp")
        if cap:
            new_caps[str(new_index)] = cap
        new_index += 1
    path = d / "captions.json"
    if new_caps:
        path.write_text(json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()
    return True


async def delete_project_folder(section: str, name: str):
    d = get_project_path(section, name)
    if d.exists():
        try:
            shutil.rmtree(d)
        except Exception as e:
            logger.error(f"Ошибка удаления папки проекта {name}: {e}")


async def send_project_photos(chat_id: int, section: str, name: str,
                              context: ContextTypes.DEFAULT_TYPE):
    """Отправляет все фото проекта медиагруппами, с подписями. Файлы корректно закрываются."""
    paths = get_project_photo_paths(section, name)
    if not paths:
        return
    captions = get_project_captions(section, name)
    for i in range(0, len(paths), 10):
        chunk = paths[i:i + 10]
        opened = []
        media = []
        try:
            for p in chunk:
                f = open(p, 'rb')
                opened.append(f)
                cap = captions.get(p.stem, "")
                if cap:
                    media.append(InputMediaPhoto(media=f, caption=cap[:1024]))
                else:
                    media.append(InputMediaPhoto(media=f))
            await context.bot.send_media_group(chat_id=chat_id, media=media)
        except Exception as e:
            logger.error(f"Ошибка отправки медиагруппы проекта: {e}")
        finally:
            for f in opened:
                try:
                    f.close()
                except Exception:
                    pass


# =====================================================================
# =========================== МЕНЮ ====================================
# =====================================================================
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context, new_message=True)


@admin_only
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_answer(update.callback_query)
    await show_main_menu(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    text = (
        "🎨 Главное меню\n\n"
        "Выберите раздел:\n"
        "• 🖼️ Галерея — обычная галерея проектов\n"
        "• 💼 Коммерческая галерея — коммерческие проекты"
    )
    keyboard = [
        [InlineKeyboardButton("🖼️ Галерея", callback_data="sec_gallery")],
        [InlineKeyboardButton("💼 Коммерческая галерея", callback_data="sec_commercial")],
    ]
    await send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard), new_message)


@admin_only
async def section_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    section = query.data.split("_", 1)[1]
    if section not in ("gallery", "commercial"):
        return
    context.user_data["section"] = section
    context.user_data["list_page"] = 0
    context.user_data.pop("current_project", None)
    await show_project_list(update, context, 0)


# =====================================================================
# ====================== СПИСОК ПРОЕКТОВ ==============================
# =====================================================================
@admin_only
async def list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    try:
        page = int(query.data.split("_", 1)[1])
    except ValueError:
        page = 0
    await show_project_list(update, context, page)


async def show_project_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    section = context.user_data.get("section", "gallery")
    projects = get_projects(section)
    PER_PAGE = 8
    total = len(projects)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    start = page * PER_PAGE
    end = min(start + PER_PAGE, total)
    page_projects = projects[start:end]

    text = f"{section_title(section)}\n\n"
    if not page_projects:
        text += "Нет проектов. Нажмите «Создать проект», чтобы добавить первый."
    else:
        for name in page_projects:
            desc = get_project_description(section, name)
            cnt = len(get_project_photo_paths(section, name))
            short = (desc[:40] + "…") if len(desc) > 40 else desc
            text += f"📁 {name}\n   📷 {cnt} фото\n   📝 {short or '(без описания)'}\n\n"
    text += f"\nСтраница {page+1} из {max(1, total_pages)}"

    keyboard = []
    for name in page_projects:
        keyboard.append([InlineKeyboardButton(f"📁 {name}", callback_data=f"view_{name}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"list_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"list_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Создать проект", callback_data="create_new")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    context.user_data["list_page"] = page
    await send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard), new_message=False)


# =====================================================================
# ===================== ПРОСМОТР ПРОЕКТА ==============================
# =====================================================================
@admin_only
async def view_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    name = query.data[len("view_"):]
    section = context.user_data.get("section", "gallery")
    if not get_project_path(section, name).exists():
        await query.edit_message_text("❌ Проект не найден.")
        return
    context.user_data["current_project"] = name

    await send_project_photos(update.effective_chat.id, section, name, context)

    desc = get_project_description(section, name)
    cnt = len(get_project_photo_paths(section, name))
    text = (
        f"📁 {name}\n\n"
        f"📝 Описание:\n{desc or '(пусто)'}\n\n"
        f"📷 Фото: {cnt}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{name}")],
        [InlineKeyboardButton("◀ К списку", callback_data=f"list_{context.user_data.get('list_page', 0)}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text,
                                               reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e2:
                logger.error(f"send view_project: {e2}")


# =====================================================================
# ==================== РЕДАКТИРОВАНИЕ ПРОЕКТА =========================
# =====================================================================
async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         section: str, name: str, new_message: bool = False):
    context.user_data["current_project"] = name
    desc = get_project_description(section, name)
    cnt = len(get_project_photo_paths(section, name))
    text = (
        f"✏️ Редактирование: {name}\n\n"
        f"📝 Описание:\n{desc or '(пусто)'}\n\n"
        f"📷 Фото: {cnt}"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить описание", callback_data="e_desc")],
        [InlineKeyboardButton("➕ Добавить фото", callback_data="e_addp")],
        [InlineKeyboardButton("🗑️ Удалить фото", callback_data="e_delp")],
        [InlineKeyboardButton("🗑️ Удалить проект", callback_data="e_delproj")],
        [InlineKeyboardButton("◀ Назад к проекту", callback_data=f"view_{name}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ]
    await send_or_edit(update, context, text, InlineKeyboardMarkup(keyboard), new_message)


@admin_only
async def edit_project_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    name = query.data[len("edit_"):]
    section = context.user_data.get("section", "gallery")
    if not get_project_path(section, name).exists():
        await query.edit_message_text("❌ Проект не найден.")
        return
    await show_edit_menu(update, context, section, name)


# ---------------------- Создание проекта ----------------------
@admin_only
async def create_new_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    section = context.user_data.get("section", "gallery")
    try:
        name = generate_unique_name(section)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END
    d = get_project_path(section, name)
    d.mkdir(parents=True, exist_ok=True)
    context.user_data["create_section"] = section
    context.user_data["create_name"] = name

    msg = await query.edit_message_text(
        f"📁 Создаётся проект {name}\n\n"
        f"Отправьте фото (можно с подписью).\n"
        f"Подпись первого фото будет использована как описание проекта.\n"
        f"Когда закончите — нажмите «Готово».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data="cdone")],
            [InlineKeyboardButton("❌ Отменить", callback_data="ccancel")],
        ])
    )
    context.user_data["create_status_msg"] = msg.message_id
    return STATE_CREATE_PHOTOS


async def create_photos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    section = context.user_data.get("create_section")
    name = context.user_data.get("create_name")
    if not section or not name:
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text("❌ Сессия истекла.")
            except Exception:
                pass
        elif update.message:
            await update.message.reply_text("❌ Сессия истекла.")
        return ConversationHandler.END

    # --- Callback: Готово / Отменить ---
    if update.callback_query:
        query = update.callback_query
        await safe_answer(query)
        data = query.data
        if data == "cdone":
            cnt = len(get_project_photo_paths(section, name))
            for k in ("create_section", "create_name", "create_status_msg"):
                context.user_data.pop(k, None)
            if cnt == 0:
                await delete_project_folder(section, name)
                try:
                    await query.edit_message_text("❌ Проект не создан: не получено ни одного фото.")
                except Exception:
                    pass
                await show_project_list(update, context, 0)
                return ConversationHandler.END
            try:
                await query.edit_message_text(f"✅ Проект {name} создан ({cnt} фото).")
            except Exception:
                pass
            await show_project_list(update, context, 0)
            return ConversationHandler.END
        elif data == "ccancel":
            await delete_project_folder(section, name)
            for k in ("create_section", "create_name", "create_status_msg"):
                context.user_data.pop(k, None)
            try:
                await query.edit_message_text("❌ Создание проекта отменено.")
            except Exception:
                pass
            await show_project_list(update, context, 0)
            return ConversationHandler.END
        return STATE_CREATE_PHOTOS

    # --- Фото ---
    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "Отправьте фото или нажмите «Готово».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Готово", callback_data="cdone")],
                [InlineKeyboardButton("❌ Отменить", callback_data="ccancel")],
            ]))
        return STATE_CREATE_PHOTOS

    try:
        caption = update.message.caption or ""
        file = await download_photo_with_retry(update.message.photo[-1])
        num = await save_project_photo(file, section, name, caption)
        if num is None:
            await update.message.reply_text("❌ Ошибка сохранения фото.")
            return STATE_CREATE_PHOTOS

        # Подпись первого фото → описание проекта (если ещё не задано)
        existing_desc = get_project_description(section, name)
        if num == 1 and caption and not existing_desc:
            set_project_description(section, name, caption)

        cnt = len(get_project_photo_paths(section, name))
        msg_id = context.user_data.get("create_status_msg")
        if msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=msg_id,
                    text=(
                        f"📁 Проект {name}\n\n"
                        f"📷 Сохранено фото: {cnt}.\n"
                        f"Отправьте ещё или нажмите «Готово»."
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Готово", callback_data="cdone")],
                        [InlineKeyboardButton("❌ Отменить", callback_data="ccancel")],
                    ]))
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.debug(f"edit status: {e}")
            except Exception as e:
                logger.debug(f"edit status: {e}")
    except Exception as e:
        logger.error(f"create_photos_handler: {e}")
        await update.message.reply_text("❌ Не удалось обработать фото.")
    return STATE_CREATE_PHOTOS


# ---------------------- Редактирование описания ----------------------
@admin_only
async def edit_desc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not context.user_data.get("current_project"):
        await query.edit_message_text("❌ Проект не выбран.")
        return ConversationHandler.END
    await query.edit_message_text(
        "Введите новое описание (отправьте «-», чтобы очистить):",
        reply_markup=CANCEL_KEYBOARD)
    return STATE_EDIT_DESC


async def edit_desc_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "-":
        text = ""
    name = context.user_data.get("current_project")
    section = context.user_data.get("section", "gallery")
    if not name:
        await update.message.reply_text("❌ Проект не найден.")
        return ConversationHandler.END
    set_project_description(section, name, text)
    await update.message.reply_text("✅ Описание обновлено.")
    await show_edit_menu(update, context, section, name, new_message=True)
    return ConversationHandler.END


# ---------------------- Добавление фото ----------------------
@admin_only
async def edit_addp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    name = context.user_data.get("current_project")
    if not name:
        await query.edit_message_text("❌ Проект не выбран.")
        return ConversationHandler.END
    await query.edit_message_text(
        "📸 Отправьте фото (можно с подписью).\n"
        "Когда закончите — нажмите «Готово».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Готово", callback_data="adone")],
            [InlineKeyboardButton("❌ Отменить", callback_data="acancel")],
        ]))
    return STATE_EDIT_ADD_PHOTOS


async def edit_addp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("current_project")
    section = context.user_data.get("section", "gallery")
    if not name or not get_project_path(section, name).exists():
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text("❌ Проект не найден.")
            except Exception:
                pass
        elif update.message:
            await update.message.reply_text("❌ Проект не найден.")
        return ConversationHandler.END

    if update.callback_query:
        query = update.callback_query
        await safe_answer(query)
        if query.data == "adone":
            try:
                await query.edit_message_text("✅ Готово. Фото сохранены.")
            except Exception:
                pass
            await show_edit_menu(update, context, section, name, new_message=True)
            return ConversationHandler.END
        elif query.data == "acancel":
            try:
                await query.edit_message_text("❌ Добавление фото отменено.")
            except Exception:
                pass
            await show_edit_menu(update, context, section, name, new_message=True)
            return ConversationHandler.END
        return STATE_EDIT_ADD_PHOTOS

    if not update.message or not update.message.photo:
        await update.message.reply_text(
            "Отправьте фото или нажмите «Готово».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Готово", callback_data="adone")],
                [InlineKeyboardButton("❌ Отменить", callback_data="acancel")],
            ]))
        return STATE_EDIT_ADD_PHOTOS

    try:
        caption = update.message.caption or ""
        file = await download_photo_with_retry(update.message.photo[-1])
        num = await save_project_photo(file, section, name, caption)
        if num:
            await update.message.reply_text(f"✅ Фото #{num} добавлено.")
        else:
            await update.message.reply_text("❌ Ошибка сохранения фото.")
    except Exception as e:
        logger.error(f"edit_addp_handler: {e}")
        await update.message.reply_text("❌ Не удалось обработать фото.")
    return STATE_EDIT_ADD_PHOTOS


# ---------------------- Удаление фото ----------------------
@admin_only
async def edit_delete_photo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    section = context.user_data.get("section", "gallery")
    name = context.user_data.get("current_project")
    if not name or not get_project_path(section, name).exists():
        await query.edit_message_text("❌ Проект не найден.")
        return
    paths = get_project_photo_paths(section, name)
    if not paths:
        await query.edit_message_text("❌ Нет фото для удаления.")
        return

    await send_project_photos(update.effective_chat.id, section, name, context)

    captions = get_project_captions(section, name)
    keyboard = []
    for p in paths:
        num = int(p.stem)
        cap = captions.get(str(num), "")
        label = f"🗑️ Фото #{num}"
        if cap:
            short = (cap[:20] + "…") if len(cap) > 20 else cap
            label += f": {short}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"delp_{num}")])
    keyboard.append([InlineKeyboardButton("◀ Отмена", callback_data=f"edit_{name}")])
    await query.edit_message_text("Выберите фото для удаления (фото выше):",
                                  reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def delete_photo_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    section = context.user_data.get("section", "gallery")
    name = context.user_data.get("current_project")
    if not name:
        await query.edit_message_text("❌ Проект не выбран.")
        return
    try:
        num = int(query.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Неверный номер фото.")
        return
    ok = await delete_project_photo(section, name, num)
    if ok:
        try:
            await query.edit_message_text(f"✅ Фото #{num} удалено, остальные перенумерованы.")
        except Exception:
            pass
    else:
        try:
            await query.edit_message_text(f"❌ Фото #{num} не найдено.")
        except Exception:
            pass
    await show_edit_menu(update, context, section, name, new_message=True)


# ---------------------- Удаление проекта ----------------------
@admin_only
async def delete_project_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    name = context.user_data.get("current_project")
    if not name:
        await query.edit_message_text("❌ Проект не выбран.")
        return
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="delproj_yes")],
        [InlineKeyboardButton("❌ Нет", callback_data=f"edit_{name}")],
    ]
    await query.edit_message_text(
        f"⚠️ Удалить проект {name} и все его фото навсегда?",
        reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def delete_project_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    name = context.user_data.get("current_project")
    section = context.user_data.get("section", "gallery")
    if name:
        await delete_project_folder(section, name)
        context.user_data.pop("current_project", None)
        try:
            await query.edit_message_text(f"✅ Проект {name} удалён.")
        except Exception:
            pass
    await show_project_list(update, context, context.user_data.get("list_page", 0))


# ---------------------- Отмена ----------------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если пользователь в середине создания проекта — удалим пустой проект
    if context.user_data.get("create_name") and context.user_data.get("create_section"):
        name = context.user_data.get("create_name")
        section = context.user_data.get("create_section")
        if len(get_project_photo_paths(section, name)) == 0:
            await delete_project_folder(section, name)
        for k in ("create_section", "create_name", "create_status_msg"):
            context.user_data.pop(k, None)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text("Операция отменена.")
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text("Операция отменена.")
    await show_main_menu(update, context, new_message=True)
    return ConversationHandler.END


# =====================================================================
# ==================== РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ =======================
# =====================================================================
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # --- Основное меню и разделы ---
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern=r"^main_menu$"))
    app.add_handler(CallbackQueryHandler(section_switch, pattern=r"^sec_(gallery|commercial)$"))
    app.add_handler(CallbackQueryHandler(list_page_callback, pattern=r"^list_\d+$"))

    # --- Просмотр и меню редактирования ---
    app.add_handler(CallbackQueryHandler(view_project, pattern=r"^view_"))
    app.add_handler(CallbackQueryHandler(edit_project_menu, pattern=r"^edit_"))

    # --- Удаление фото / проекта ---
    app.add_handler(CallbackQueryHandler(edit_delete_photo_menu, pattern=r"^e_delp$"))
    app.add_handler(CallbackQueryHandler(delete_photo_execute, pattern=r"^delp_\d+$"))
    app.add_handler(CallbackQueryHandler(delete_project_confirm, pattern=r"^e_delproj$"))
    app.add_handler(CallbackQueryHandler(delete_project_execute, pattern=r"^delproj_yes$"))

    # --- ConversationHandler: создание проекта ---
    create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_new_project, pattern=r"^create_new$")],
        states={
            STATE_CREATE_PHOTOS: [
                MessageHandler(filters.PHOTO, create_photos_handler),
                CallbackQueryHandler(create_photos_handler, pattern=r"^(cdone|ccancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(create_conv)

    # --- ConversationHandler: редактирование описания ---
    edit_desc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_desc_start, pattern=r"^e_desc$")],
        states={
            STATE_EDIT_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_desc_save),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_desc_conv)

    # --- ConversationHandler: добавление фото ---
    edit_addp_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_addp_start, pattern=r"^e_addp$")],
        states={
            STATE_EDIT_ADD_PHOTOS: [
                MessageHandler(filters.PHOTO, edit_addp_handler),
                CallbackQueryHandler(edit_addp_handler, pattern=r"^(adone|acancel)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^main_menu$"),
        ],
        per_message=False,
    )
    app.add_handler(edit_addp_conv)


# =====================================================================
# ============================ ЗАПУСК =================================
# =====================================================================
async def main():
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    application = Application.builder().token(TOKEN).request(request).build()
    register_handlers(application)

    await application.initialize()
    await application.start()
    logger.info("Бот 'Галерея / Коммерческая галерея' запущен (v1.0).")
    await application.updater.start_polling()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())