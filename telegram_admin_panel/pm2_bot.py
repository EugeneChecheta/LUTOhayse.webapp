#!/usr/bin/env python3
"""
Telegram‑бот для управления скриптами PM2.
Версия 2.1
- При выполнении restart/start/stop для отдельного скрипта вместо результата действия
  выводится статус всех скриптов (как при нажатии кнопки "Статус всех").
- После отправки статуса меню с кнопками обновляется (редактируется) – чат не захламляется.
"""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ------------------------- Пути и конфигурация -------------------------
BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token_pm2.txt"
ADMIN_FILE = BASE_DIR / "admin_ids.txt"
SCRIPTS_DIR = BASE_DIR / "scripts"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------- Чтение конфигураций -------------------------
def read_token() -> str:
    if not TOKEN_FILE.exists():
        logger.error(f"Файл token.txt не найден: {TOKEN_FILE}")
        sys.exit(1)
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
            if not token:
                logger.error("Токен пустой")
                sys.exit(1)
            return token
    except Exception as e:
        logger.error(f"Ошибка чтения token.txt: {e}")
        sys.exit(1)

def read_admin_ids() -> List[int]:
    if not ADMIN_FILE.exists():
        logger.warning(f"Файл admin_ids.txt не найден: {ADMIN_FILE}")
        return []
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            ids = [int(line.strip()) for line in f if line.strip().isdigit()]
            return ids
    except Exception as e:
        logger.error(f"Ошибка чтения admin_ids.txt: {e}")
        return []

TOKEN = read_token()
ADMIN_IDS = read_admin_ids()

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
            return
        return await func(update, context)
    return wrapper

# ------------------------- Работа со скриптами PM2 -------------------------
def get_scripts_info() -> List[Dict[str, str]]:
    """Возвращает список скриптов: {name, description} из файлов scripts/*.txt"""
    if not SCRIPTS_DIR.exists():
        return []
    scripts = []
    for file_path in SCRIPTS_DIR.glob("*.txt"):
        name = file_path.stem
        description = file_path.read_text(encoding="utf-8").strip()
        scripts.append({"name": name, "description": description})
    return sorted(scripts, key=lambda x: x["name"])

def parse_pm2_status(raw_output: str) -> str:
    """
    Парсит вывод `pm2 status` и возвращает краткий отчёт:
    имя_скрипта: состояние (online/offline/stopped/...)
    """
    # Удаляем ANSI-последовательности
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
    clean = ansi_escape.sub('', raw_output)

    lines = clean.splitlines()
    result_lines = []

    for line in lines:
        # Нас интересуют строки, начинающиеся с '│' и не являющиеся линиями таблицы
        if not line.startswith('│'):
            continue
        if any(ch in line for ch in ('─', '┬', '┼', '┌', '├', '└')):
            continue
        # Разбиваем по вертикальной черте
        parts = [p.strip() for p in line.split('│')]
        # Убираем пустые части (первая и последняя могут быть пустыми)
        parts = [p for p in parts if p]
        if len(parts) < 9:
            continue
        # Имя находится во второй колонке (индекс 1), статус — в девятой (индекс 8)
        name = parts[1].strip()
        status = parts[8].strip()
        if name and status:
            result_lines.append(f"{name}: {status}")

    if not result_lines:
        return "Нет данных о статусе скриптов."
    return "\n".join(result_lines)

async def run_pm2_command(action: str, script_name: Optional[str] = None) -> str:
    """
    Выполняет команду pm2.
    action: 'restart', 'start', 'stop', 'status'
    script_name: имя скрипта или None/'all' для всех
    Возвращает строку с результатом.
    Для action='status' возвращает краткий отформатированный список.
    """
    if action == "status":
        # Всегда запрашиваем статус всех процессов
        cmd = ["pm2", "status"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        out = stdout.decode().strip() or stderr.decode().strip()
        return parse_pm2_status(out) if out else "Команда выполнена без вывода."

    # Действия restart/start/stop — только для отдельных скриптов (не для всех)
    if script_name is None or script_name == "all":
        # Групповые операции удалены, но на всякий случай оставляем обработку
        scripts = get_scripts_info()
        if not scripts:
            return "Нет скриптов для управления."
        results = []
        for s in scripts:
            name = s["name"]
            cmd = ["pm2", action, name]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode().strip() or stderr.decode().strip()
            results.append(f"{name}: {output or 'OK'}")
        return "\n".join(results)
    else:
        cmd = ["pm2", action, script_name]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        out = stdout.decode().strip() or stderr.decode().strip()
        return out or f"Команда {action} для {script_name} выполнена."

# ------------------------- Обработчики бота -------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update, context, new_message=True)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    """Отображает главное меню со списком скриптов и кнопками управления."""
    scripts = get_scripts_info()
    text = "⚙️ *Управление скриптами PM2*\n\n"
    if not scripts:
        text += "❌ Нет файлов описаний скриптов в папке `scripts/`.\n"
        text += "Создайте файлы вида `имя_скрипта.txt` с описанием."
    else:
        for s in scripts:
            text += f"📄 `{s['name']}`\n"
            if s['description']:
                text += f"   {s['description']}\n"

    keyboard = []
    # Кнопки для каждого скрипта: перезапуск, запуск, остановка (без статуса)
    for s in scripts:
        name = s['name']
        row = [
            InlineKeyboardButton("🔄", callback_data=f"restart_{name}"),
            InlineKeyboardButton("▶️", callback_data=f"start_{name}"),
            InlineKeyboardButton("⏹", callback_data=f"stop_{name}"),
        ]
        keyboard.append(row)

    # Отдельная кнопка статуса всех
    if scripts:
        keyboard.append([InlineKeyboardButton("📊 Статус всех", callback_data="status_all")])

    # Вспомогательные кнопки
    keyboard.append([InlineKeyboardButton("🔄 Обновить список", callback_data="refresh")])
    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if new_message:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        query = update.callback_query
        if query:
            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            # Запасной вариант (например, при вызове из команды)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

@admin_only
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все нажатия кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_menu":
        await show_menu(update, context, new_message=False)
        return

    if data == "refresh":
        await show_menu(update, context, new_message=False)
        return

    if data == "status_all":
        # Запрос статуса всех скриптов
        result = await run_pm2_command("status")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📊 *Статус всех скриптов:*\n```\n{result}\n```",
            parse_mode="Markdown"
        )
        await show_menu(update, context, new_message=False)
        return

    # Обработка действий для отдельных скриптов: restart_xxx, start_xxx, stop_xxx
    parts = data.split("_", 1)
    if len(parts) != 2:
        await query.edit_message_text("❌ Неверная команда.")
        return

    action, script = parts
    if action not in ("restart", "start", "stop"):
        await query.edit_message_text("❌ Неизвестное действие.")
        return

    # Выполняем команду для конкретного скрипта (результат не выводим)
    await run_pm2_command(action, script)

    # Отправляем статус всех скриптов (как при нажатии на кнопку "Статус всех")
    status_result = await run_pm2_command("status")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📊 *Статус всех скриптов после действия `{action}` для `{script}`:*\n```\n{status_result}\n```",
        parse_mode="Markdown"
    )

    # Возвращаемся к меню (редактируем текущее сообщение с кнопками)
    await show_menu(update, context, new_message=False)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status – показывает статус всех скриптов в кратком виде."""
    result = await run_pm2_command("status")
    await update.message.reply_text(
        f"📊 *Статус всех скриптов:*\n```\n{result}\n```",
        parse_mode="Markdown"
    )

@admin_only
async def reboot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /reboot – перезапускает самого бота через pm2."""
    try:
        process = await asyncio.create_subprocess_exec(
            "pm2", "restart", "pm2_bot",   # предполагается, что бот запущен как pm2_bot
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            await update.message.reply_text(f"✅ Бот перезапущен.\n{stdout.decode()}")
        else:
            await update.message.reply_text(f"❌ Ошибка перезапуска.\n{stderr.decode()}")
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось перезапустить: {e}")

# ------------------------- Регистрация обработчиков -------------------------
def main():
    application = Application.builder().token(TOKEN).request(
        HTTPXRequest(connect_timeout=30, read_timeout=120)
    ).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("reboot", reboot_command))

    # Обработчик всех callback-кнопок (единый)
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Бот управления PM2 запущен (v2.1)")
    application.run_polling()

if __name__ == "__main__":
    main()