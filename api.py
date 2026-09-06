# api.py
# Полностью переработанный файл API для поддержки конструктора матрацев

import psycopg2
import os
from flask import Flask, jsonify, send_from_directory, abort, request, session, redirect
from flask_cors import CORS
import glob
from pathlib import Path
import uuid
from datetime import datetime
import secrets
import threading
import json
import urllib.request
import urllib.error
from werkzeug.security import generate_password_hash, check_password_hash
import logging

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(24))
CORS(app, supports_credentials=True)

# Настройка логирования для отладки
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ----- Чтение конфигурации БД из файла telegram_admin_panel/config_db.txt -----
def get_db_config():
    config_path = Path(__file__).parent / 'telegram_admin_panel' / 'config_db.txt'
    config = {}
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                config[k] = v
    return config

DB_CONFIG = get_db_config()

def get_db_connection():
    return psycopg2.connect(
        host=DB_CONFIG.get('host', 'localhost'),
        port=DB_CONFIG.get('port', '5432'),
        database=DB_CONFIG['database'],
        user=DB_CONFIG['user'],
        password=DB_CONFIG['password']
    )

# ----- Загрузка конфигурации бота для уведомлений о заказах -----
ORDER_BOT_TOKEN = None
ORDER_ADMIN_IDS = None

def load_order_bot_config():
    global ORDER_BOT_TOKEN, ORDER_ADMIN_IDS
    token_path = Path(__file__).parent / 'telegram_order_panel' / 'token.txt'
    admin_path = Path(__file__).parent / 'telegram_order_panel' / 'admin_ids.txt'
    try:
        with open(token_path, 'r', encoding='utf-8') as f:
            ORDER_BOT_TOKEN = f.read().strip()
    except Exception as e:
        app.logger.error(f"Не удалось прочитать token.txt для бота заказов: {e}")
    try:
        with open(admin_path, 'r', encoding='utf-8') as f:
            ORDER_ADMIN_IDS = [int(line.strip()) for line in f if line.strip().isdigit()]
    except Exception as e:
        app.logger.error(f"Не удалось прочитать admin_ids.txt для бота заказов: {e}")

load_order_bot_config()

def send_order_notification(order_id, user_name, phone, total_sum, order_type='product'):
    """Отправляет уведомление о новом заказе всем администраторам через Telegram бота (urllib)."""
    if not ORDER_BOT_TOKEN or not ORDER_ADMIN_IDS:
        app.logger.warning("Не настроен бот для уведомлений о заказах – сообщение не отправлено")
        return

    if order_type == 'mattress':
        text = (
            f"🛏️ *Новый заказ матраца!*\n"
            f"Номер: #{order_id}\n"
            f"Клиент: {user_name}\n"
            f"Телефон: {phone}\n"
            f"Сумма: {total_sum} ₽"
        )
        callback = f"mattress_order_details_{order_id}"
    else:
        text = (
            f"🆕 *Новый заказ!*\n"
            f"Номер: #{order_id}\n"
            f"Клиент: {user_name}\n"
            f"Телефон: {phone}\n"
            f"Сумма: {total_sum} ₽"
        )
        callback = f"order_details_{order_id}_0"

    keyboard = {
        "inline_keyboard": [[
            {"text": "📋 Посмотреть заказ", "callback_data": callback}
        ]]
    }
    reply_markup = json.dumps(keyboard)

    def send_to_admin(admin_id):
        url = f"https://api.telegram.org/bot{ORDER_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": admin_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": reply_markup
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            app.logger.error(f"Ошибка отправки уведомления админу {admin_id}: {e}")

    for admin_id in ORDER_ADMIN_IDS:
        threading.Thread(target=send_to_admin, args=(admin_id,)).start()

# ----- Вспомогательная функция для создания таблиц, если их нет, и добавления недостающих колонок -----
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # Таблицы для пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(256) NOT NULL,
            full_name VARCHAR(200),
            email VARCHAR(200),
            address TEXT,
            contact_time VARCHAR(100),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Заказы с привязкой к пользователю (user_id может быть NULL для гостей)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(100) NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            user_name VARCHAR(200) NOT NULL,
            phone VARCHAR(50) NOT NULL,
            email VARCHAR(200),
            address TEXT,
            comment TEXT,
            contact_time VARCHAR(100),
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'Ожидает подтверждения'
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
            product_code VARCHAR(100) NOT NULL,
            product_name VARCHAR(200) NOT NULL,
            material_code VARCHAR(100) NOT NULL,
            material_name VARCHAR(200) NOT NULL,
            cost INTEGER NOT NULL,
            quantity INTEGER DEFAULT 1,
            extra_data TEXT
        );
    """)

    # Проверяем, есть ли колонка user_id в таблице orders (если таблица уже существовала)
    cur.execute("""
        SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name='orders' AND column_name='user_id'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("""
            ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
        """)

    # Добавляем колонку extra_data в order_items для хранения деталей топперов
    cur.execute("""
        SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name='order_items' AND column_name='extra_data'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("""
            ALTER TABLE order_items ADD COLUMN extra_data TEXT;
        """)

    # --- Таблицы для топперов (создаются, если отсутствуют) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topper_sizes (
            id SERIAL PRIMARY KEY,
            size VARCHAR(50) NOT NULL UNIQUE
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topper_layers (
            id SERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(200) NOT NULL,
            description TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topper_covers (
            id SERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(200) NOT NULL,
            description TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topper_layer_prices (
            id SERIAL PRIMARY KEY,
            layer_id INTEGER NOT NULL,
            size_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK (price >= 0),
            CONSTRAINT fk_tlp_layer
                FOREIGN KEY (layer_id)
                REFERENCES topper_layers(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_tlp_size
                FOREIGN KEY (size_id)
                REFERENCES topper_sizes(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_tlp_layer_size UNIQUE(layer_id, size_id)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topper_cover_prices (
            id SERIAL PRIMARY KEY,
            cover_id INTEGER NOT NULL,
            size_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK (price >= 0),
            CONSTRAINT fk_tcp_cover
                FOREIGN KEY (cover_id)
                REFERENCES topper_covers(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_tcp_size
                FOREIGN KEY (size_id)
                REFERENCES topper_sizes(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_tcp_cover_size UNIQUE(cover_id, size_id)
        );
    """)

    # --- Добавление новых колонок в существующие таблицы, если их нет ---
    # Для topper_layers: color_text, is_hidden
    cur.execute("""
        SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name='topper_layers' AND column_name='color_text'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("ALTER TABLE topper_layers ADD COLUMN color_text VARCHAR(20) DEFAULT '#000000';")

    cur.execute("""
        SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name='topper_layers' AND column_name='is_hidden'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("ALTER TABLE topper_layers ADD COLUMN is_hidden BOOLEAN DEFAULT FALSE;")

    # Для topper_covers: is_hidden
    cur.execute("""
        SELECT COUNT(*) 
        FROM information_schema.columns 
        WHERE table_name='topper_covers' AND column_name='is_hidden'
    """)
    if cur.fetchone()[0] == 0:
        cur.execute("ALTER TABLE topper_covers ADD COLUMN is_hidden BOOLEAN DEFAULT FALSE;")

    # ========== НОВЫЕ ТАБЛИЦЫ ДЛЯ КОНСТРУКТОРА МАТРАЦЕВ ==========
    # Размеры
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_sizes (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE
        );
    """)
    # Слои
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_layers (
            id SERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            color_text VARCHAR(20) DEFAULT '#000000',
            is_hidden BOOLEAN DEFAULT FALSE
        );
    """)
    # Чехлы
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_cover (
            id SERIAL PRIMARY KEY,
            code VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(200) NOT NULL,
            description TEXT,
            is_hidden BOOLEAN DEFAULT FALSE
        );
    """)
    # Цены слоёв
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_layers_prices (
            id SERIAL PRIMARY KEY,
            layer_id INTEGER NOT NULL,
            size_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK (price >= 0),
            CONSTRAINT fk_mlp_layer
                FOREIGN KEY (layer_id)
                REFERENCES mattress_layers(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_mlp_size
                FOREIGN KEY (size_id)
                REFERENCES mattress_sizes(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_mlp_layer_size UNIQUE (layer_id, size_id)
        );
    """)
    # Цены чехлов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_cover_prices (
            id SERIAL PRIMARY KEY,
            cover_id INTEGER NOT NULL,
            size_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK (price >= 0),
            CONSTRAINT fk_mcp_cover
                FOREIGN KEY (cover_id)
                REFERENCES mattress_cover(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_mcp_size
                FOREIGN KEY (size_id)
                REFERENCES mattress_sizes(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_mcp_cover_size UNIQUE (cover_id, size_id)
        );
    """)
    # Типы основных характеристик слоёв
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_layer_main_features_types (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE
        );
    """)
    # Основные характеристики слоёв
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_layer_main_features (
            id SERIAL PRIMARY KEY,
            layer_id INTEGER NOT NULL,
            feature_id INTEGER NOT NULL,
            value TEXT,
            CONSTRAINT fk_mlmf_layer
                FOREIGN KEY (layer_id)
                REFERENCES mattress_layers(id)
                ON DELETE CASCADE,
            CONSTRAINT fk_mlmf_feature
                FOREIGN KEY (feature_id)
                REFERENCES mattress_layer_main_features_types(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_mlmf_layer_feature UNIQUE (layer_id, feature_id)
        );
    """)
    # Дополнительные характеристики слоёв
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_layer_extra_features (
            id SERIAL PRIMARY KEY,
            layer_id INTEGER NOT NULL,
            name VARCHAR(100) NOT NULL,
            value TEXT,
            CONSTRAINT fk_mlef_layer
                FOREIGN KEY (layer_id)
                REFERENCES mattress_layers(id)
                ON DELETE CASCADE,
            CONSTRAINT uq_mlef_layer_name UNIQUE (layer_id, name)
        );
    """)
    # Заказы матрацев
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_orders (
            id SERIAL PRIMARY KEY,
            session_id VARCHAR(100) NOT NULL,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            user_name VARCHAR(200) NOT NULL,
            phone VARCHAR(50) NOT NULL,
            email VARCHAR(200),
            address TEXT,
            comment TEXT,
            contact_time VARCHAR(100),
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'Ожидает подтверждения',
            size_id INTEGER NOT NULL REFERENCES mattress_sizes(id) ON DELETE RESTRICT,
            initial_height INTEGER DEFAULT 0,
            cover_id INTEGER NOT NULL REFERENCES mattress_cover(id) ON DELETE RESTRICT,
            cover_price INTEGER NOT NULL CHECK (cover_price >= 0)
        );
    """)
    # Позиции заказов матрацев (слои)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mattress_order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES mattress_orders(id) ON DELETE CASCADE,
            layer_id INTEGER NOT NULL REFERENCES mattress_layers(id) ON DELETE RESTRICT,
            layer_code VARCHAR(100) NOT NULL,
            layer_name VARCHAR(200) NOT NULL,
            quantity INTEGER DEFAULT 1 CHECK (quantity > 0),
            price_per_unit INTEGER NOT NULL CHECK (price_per_unit >= 0),
            total_price INTEGER NOT NULL CHECK (total_price >= 0)
        );
    """)

    # Индексы для новых таблиц
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_layers_code ON mattress_layers(code);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_cover_code ON mattress_cover(code);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_sizes_name ON mattress_sizes(name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mlp_layer ON mattress_layers_prices(layer_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mlp_size ON mattress_layers_prices(size_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_cover ON mattress_cover_prices(cover_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mcp_size ON mattress_cover_prices(size_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_orders_session ON mattress_orders(session_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_orders_date ON mattress_orders(order_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_orders_user ON mattress_orders(user_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_orders_size ON mattress_orders(size_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mattress_orders_cover ON mattress_orders(cover_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_moi_order ON mattress_order_items(order_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_moi_layer ON mattress_order_items(layer_id);")

    conn.commit()
    cur.close()
    conn.close()

init_db()  # Вызов при старте

# ==================== СУЩЕСТВУЮЩИЕ API (без изменений) ====================

@app.route('/api/types')
def product_types():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, name FROM products_type ORDER BY id;')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1]} for r in rows])
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/flags')
def flags():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM flags WHERE lower(name) != 'скрытый' ORDER BY id;")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1]} for r in rows])
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/products')
def products():
    try:
        type_id = request.args.get('type_id', type=int)
        flag_ids = request.args.getlist('flag_ids', type=int)
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 12, type=int)
        if page < 1:
            page = 1
        if limit < 1 or limit > 50:
            limit = 12

        conn = get_db_connection()
        cur = conn.cursor()

        hidden_flag_condition = """
            NOT EXISTS (
                SELECT 1 FROM flags_for_products fp
                JOIN flags f ON fp.flags_id = f.id
                WHERE fp.products_id = p.id AND lower(f.name) = 'скрытый'
            )
        """

        base_query = f"""
            SELECT p.code, p.name, p.min_cost
            FROM products p
            WHERE 1=1
            AND {hidden_flag_condition}
        """
        params = []

        if type_id is not None:
            base_query += " AND p.products_type_id = %s"
            params.append(type_id)

        if flag_ids:
            base_query += """
                AND EXISTS (
                    SELECT 1 FROM flags_for_products fp
                    WHERE fp.products_id = p.id AND fp.flags_id = ANY(%s)
                    GROUP BY fp.products_id
                    HAVING COUNT(DISTINCT fp.flags_id) = %s
                )
            """
            params.append(flag_ids)
            params.append(len(flag_ids))

        count_query = f"SELECT COUNT(*) FROM ({base_query}) AS sub"
        cur.execute(count_query, params)
        total = cur.fetchone()[0]

        paginated_query = base_query + " ORDER BY p.id LIMIT %s OFFSET %s"
        params_paginated = params + [limit, (page - 1) * limit]
        cur.execute(paginated_query, params_paginated)
        rows = cur.fetchall()

        cur.close()
        conn.close()

        items = [{'code': r[0], 'name': r[1], 'min_cost': r[2]} for r in rows]
        return jsonify({'items': items, 'total': total, 'page': page, 'limit': limit})
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/product/<code>')
def product_details(code):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT p.id, p.code, p.name, p.min_cost, p.products_type_id, pt.name as type_name
            FROM products p
            JOIN products_type pt ON p.products_type_id = pt.id
            WHERE p.code = %s
        """, (code,))
        product_row = cur.fetchone()
        if not product_row:
            cur.close()
            conn.close()
            return jsonify({'error': 'Product not found'}), 404

        prod_id = product_row[0]
        product_data = {
            'id': prod_id,
            'code': product_row[1],
            'name': product_row[2],
            'min_cost': product_row[3],
            'products_type_id': product_row[4],
            'type_name': product_row[5]
        }

        cur.execute("""
            SELECT mt.id, mt.name, mfp.cost
            FROM materials_for_products mfp
            JOIN materials_type mt ON mfp.materials_type_id = mt.id
            WHERE mfp.products_id = %s
            ORDER BY mt.id
        """, (prod_id,))
        costs = [{'id': r[0], 'name': r[1], 'cost': r[2]} for r in cur.fetchall()]

        cur.execute("""
            SELECT mft.id, mft.name, COALESCE(pmf.value, '') as value
            FROM main_features_types mft
            LEFT JOIN product_main_features pmf 
                ON mft.id = pmf.feature_id AND pmf.products_id = %s
            ORDER BY mft.id
        """, (prod_id,))
        main_features = [{'id': r[0], 'name': r[1], 'value': r[2]} for r in cur.fetchall()]

        cur.execute("""
            SELECT id, name, value
            FROM product_extra_features
            WHERE products_id = %s
            ORDER BY id
        """, (prod_id,))
        extra_features = [{'id': r[0], 'name': r[1], 'value': r[2]} for r in cur.fetchall()]

        cur.close()
        conn.close()

        media_dir = Path(__file__).parent / 'media' / 'products' / code
        photos = {'preview': None, 'size': None, 'main': []}
        if media_dir.is_dir():
            preview_path = media_dir / 'preview.webp'
            if preview_path.exists():
                photos['preview'] = f'/media/products/{code}/preview.webp'
            size_path = media_dir / 'size.webp'
            if size_path.exists():
                photos['size'] = f'/media/products/{code}/size.webp'
            main_files = sorted(media_dir.glob('[0-9]*.webp'), key=lambda p: int(p.stem))
            photos['main'] = [f'/media/products/{code}/{p.name}' for p in main_files]

        result = {
            'product': product_data,
            'costs': costs,
            'main_features': main_features,
            'extra_features': extra_features,
            'photos': photos,
            'min_cost': product_data['min_cost']
        }
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /api/product/<code> error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/materials')
def get_materials():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.code, m.name, m.materials_type_id, mt.name as type_name
            FROM materials m
            JOIN materials_type mt ON m.materials_type_id = mt.id
            ORDER BY mt.id, m.id
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        materials = []
        media_base = Path(__file__).parent / 'media' / 'materials'
        for row in rows:
            code = row[1]
            photo_path = media_base / f"{code}.webp"
            photo_url = f"/media/materials/{code}.webp" if photo_path.exists() else None
            materials.append({
                'id': row[0],
                'code': code,
                'name': row[2],
                'materials_type_id': row[3],
                'type_name': row[4],
                'photo_url': photo_url
            })
        return jsonify(materials)
    except Exception as e:
        app.logger.error(f"API /api/materials error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# ========= ГАЛЕРЕЯ =========
GALLERY_DIR = Path(__file__).parent / 'media' / 'gallery'

def get_gallery_projects(page=1, limit=10):
    if not GALLERY_DIR.exists():
        return [], 0
    folders = [f for f in GALLERY_DIR.iterdir() if f.is_dir()]
    folders.sort(key=lambda x: x.name, reverse=True)
    total = len(folders)
    start = (page - 1) * limit
    end = start + limit
    paginated = folders[start:end]
    projects = []
    for folder in paginated:
        desc_file = folder / 'description.txt'
        description = ''
        if desc_file.exists():
            with open(desc_file, 'r', encoding='utf-8') as f:
                description = f.read().strip()
        images = sorted(folder.glob('*.webp'))
        preview = f'/media/gallery/{folder.name}/{images[0].name}' if images else None
        projects.append({
            'folder': folder.name,
            'preview': preview,
            'image_count': len(images),
            'description': description,
            'date': folder.name
        })
    return projects, total

@app.route('/api/gallery/projects')
def gallery_projects():
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    if page < 1:
        page = 1
    if limit < 1 or limit > 50:
        limit = 10
    projects, total = get_gallery_projects(page, limit)
    return jsonify({
        'projects': projects,
        'total': total,
        'page': page,
        'limit': limit
    })

@app.route('/api/gallery/project/<folder>')
def gallery_project(folder):
    folder_path = GALLERY_DIR / folder
    if not folder_path.is_dir():
        return jsonify({'error': 'Project not found'}), 404
    images = sorted(folder_path.glob('*.webp'))
    image_urls = [f'/media/gallery/{folder}/{img.name}' for img in images]
    desc_file = folder_path / 'description.txt'
    description = ''
    if desc_file.exists():
        with open(desc_file, 'r', encoding='utf-8') as f:
            description = f.read().strip()
    return jsonify({
        'folder': folder,
        'images': image_urls,
        'description': description
    })

# ========= АВТЕНТИФИКАЦИЯ И ПРОФИЛЬ =========
@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()
    full_name = data.get('full_name', '').strip()
    if not phone or not password:
        return jsonify({'error': 'Телефон и пароль обязательны'}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE phone = %s", (phone,))
        if cur.fetchone():
            return jsonify({'error': 'Пользователь с таким телефоном уже существует'}), 409
        password_hash = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (phone, password_hash, full_name) VALUES (%s, %s, %s) RETURNING id",
            (phone, password_hash, full_name)
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        session['user_id'] = user_id
        return jsonify({'success': True, 'user_id': user_id})
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Register error: {e}")
        return jsonify({'error': 'Ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    phone = data.get('phone', '').strip()
    password = data.get('password', '').strip()
    if not phone or not password:
        return jsonify({'error': 'Телефон и пароль обязательны'}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, password_hash, full_name, email, address, contact_time FROM users WHERE phone = %s", (phone,))
        user = cur.fetchone()
        if not user or not check_password_hash(user[1], password):
            return jsonify({'error': 'Неверный телефон или пароль'}), 401
        session['user_id'] = user[0]
        return jsonify({
            'success': True,
            'user': {
                'id': user[0],
                'phone': phone,
                'full_name': user[2],
                'email': user[3],
                'address': user[4],
                'contact_time': user[5]
            }
        })
    except Exception as e:
        app.logger.error(f"Login error: {e}")
        return jsonify({'error': 'Ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({'success': True})

@app.route('/api/auth/me')
def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'authenticated': False}), 200
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, phone, full_name, email, address, contact_time FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            session.pop('user_id', None)
            return jsonify({'authenticated': False}), 200
        return jsonify({
            'authenticated': True,
            'user': {
                'id': user[0],
                'phone': user[1],
                'full_name': user[2],
                'email': user[3],
                'address': user[4],
                'contact_time': user[5]
            }
        })
    except Exception as e:
        app.logger.error(f"Get user error: {e}")
        return jsonify({'error': 'Server error'}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/auth/profile', methods=['PUT'])
def update_profile():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Не авторизован'}), 401
    data = request.get_json()
    full_name = data.get('full_name', '').strip()
    email = data.get('email', '').strip()
    address = data.get('address', '').strip()
    contact_time = data.get('contact_time', '').strip()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET full_name = %s, email = %s, address = %s, contact_time = %s
            WHERE id = %s
        """, (full_name, email, address, contact_time, user_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Update profile error: {e}")
        return jsonify({'error': 'Ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/auth/change-password', methods=['POST'])
def change_password():
    app.logger.info("Запрос на смену пароля получен")
    user_id = session.get('user_id')
    if not user_id:
        app.logger.warning("Попытка смены пароля без авторизации")
        return jsonify({'error': 'Не авторизован'}), 401

    data = request.get_json()
    if not data:
        app.logger.warning("Пустой JSON в запросе")
        return jsonify({'error': 'Отсутствуют данные'}), 400

    current_password = data.get('current_password')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not current_password or not new_password or not confirm_password:
        app.logger.warning("Не все поля заполнены")
        return jsonify({'error': 'Все поля обязательны'}), 400

    if new_password != confirm_password:
        app.logger.warning("Новый пароль и подтверждение не совпадают")
        return jsonify({'error': 'Новый пароль и подтверждение не совпадают'}), 400

    if len(new_password) < 6:
        app.logger.warning("Слишком короткий новый пароль")
        return jsonify({'error': 'Новый пароль должен содержать не менее 6 символов'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            app.logger.warning(f"Пользователь с id {user_id} не найден")
            return jsonify({'error': 'Пользователь не найден'}), 404

        if not check_password_hash(row[0], current_password):
            app.logger.warning(f"Неверный текущий пароль для пользователя {user_id}")
            return jsonify({'error': 'Неверный текущий пароль'}), 401

        new_hash = generate_password_hash(new_password)
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))
        conn.commit()
        app.logger.info(f"Пароль успешно изменён для пользователя {user_id}")
        return jsonify({'success': True})

    except Exception as e:
        conn.rollback()
        app.logger.error(f"Ошибка при смене пароля: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

# ========= ИСТОРИЯ ЗАКАЗОВ (объединённая) =========
@app.route('/api/orders/history')
def order_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Не авторизован'}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Получаем заказы продуктов
        cur.execute("""
            SELECT id, user_name, phone, email, address, comment, contact_time, order_date, status, 'product' as type
            FROM orders
            WHERE user_id = %s
        """, (user_id,))
        product_orders = cur.fetchall()

        # Получаем заказы матрацев
        cur.execute("""
            SELECT id, user_name, phone, email, address, comment, contact_time, order_date, status, 'mattress' as type
            FROM mattress_orders
            WHERE user_id = %s
        """, (user_id,))
        mattress_orders = cur.fetchall()

        # Объединяем
        all_orders = list(product_orders) + list(mattress_orders)
        # Сортируем по дате (поле order_date - индекс 7)
        all_orders.sort(key=lambda x: x[7], reverse=True)

        result = []
        for row in all_orders:
            order_id = row[0]
            order_type = row[9]
            order_data = {
                'id': order_id,
                'type': order_type,
                'user_name': row[1],
                'phone': row[2],
                'email': row[3],
                'address': row[4],
                'comment': row[5],
                'contact_time': row[6],
                'order_date': row[7].isoformat(),
                'status': row[8]
            }

            if order_type == 'product':
                cur.execute("""
                    SELECT product_code, product_name, material_name, cost, quantity, extra_data
                    FROM order_items
                    WHERE order_id = %s
                """, (order_id,))
                items = []
                for r in cur.fetchall():
                    item = {
                        'product_code': r[0],
                        'product_name': r[1],
                        'material_name': r[2],
                        'cost': r[3],
                        'quantity': r[4]
                    }
                    if r[5] is not None:
                        try:
                            extra = json.loads(r[5])
                            item['extra_data'] = extra
                            item['is_topper'] = True
                        except:
                            pass
                    items.append(item)
                order_data['items'] = items
                order_data['total'] = sum(it['cost'] * it['quantity'] for it in items)

            else:  # mattress
                # Получаем детали заказа матраца
                cur.execute("""
                    SELECT size_id, initial_height, cover_id, cover_price
                    FROM mattress_orders
                    WHERE id = %s
                """, (order_id,))
                mattress_row = cur.fetchone()
                if mattress_row:
                    order_data['size_id'] = mattress_row[0]
                    order_data['initial_height'] = mattress_row[1]
                    order_data['cover_id'] = mattress_row[2]
                    order_data['cover_price'] = mattress_row[3]

                cur.execute("""
                    SELECT layer_id, layer_code, layer_name, quantity, price_per_unit, total_price
                    FROM mattress_order_items
                    WHERE order_id = %s
                """, (order_id,))
                items = []
                total = 0
                for r in cur.fetchall():
                    items.append({
                        'layer_id': r[0],
                        'layer_code': r[1],
                        'layer_name': r[2],
                        'quantity': r[3],
                        'price_per_unit': r[4],
                        'total_price': r[5]
                    })
                    total += r[5]
                order_data['items'] = items
                order_data['total'] = total + (order_data.get('cover_price', 0))

            result.append(order_data)

        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Order history error: {e}")
        return jsonify({'error': 'Server error'}), 500
    finally:
        cur.close()
        conn.close()

# ========= КОРЗИНА (сессия) =========
def get_cart_session():
    if 'cart' not in session:
        session['cart'] = []
    return session['cart']

def save_cart_session(cart):
    session['cart'] = cart
    session.modified = True

@app.route('/api/cart', methods=['GET'])
def get_cart():
    cart = get_cart_session()
    total = sum(item['cost'] * item.get('quantity', 1) for item in cart)
    return jsonify({'items': cart, 'total': total})

@app.route('/api/cart/add', methods=['POST'])
def add_to_cart():
    data = request.get_json()
    required = ['product_code', 'product_name', 'material_id', 'material_code', 'material_name', 'cost']
    if not all(k in data for k in required):
        return jsonify({'error': 'Missing fields'}), 400

    quantity = data.get('quantity', 1)
    if not isinstance(quantity, int) or quantity < 1:
        quantity = 1

    cart = get_cart_session()
    for item in cart:
        if item.get('type') == 'product' and item['product_code'] == data['product_code'] and item['material_id'] == data['material_id']:
            item['quantity'] = item.get('quantity', 1) + quantity
            save_cart_session(cart)
            return jsonify({'success': True, 'cart': cart})

    new_item = {
        'id': str(uuid.uuid4()),
        'type': 'product',
        'product_code': data['product_code'],
        'product_name': data['product_name'],
        'material_id': data['material_id'],
        'material_code': data['material_code'],
        'material_name': data['material_name'],
        'cost': data['cost'],
        'quantity': quantity
    }
    cart.append(new_item)
    save_cart_session(cart)
    return jsonify({'success': True, 'cart': cart})

@app.route('/api/cart/update/<item_id>', methods=['PUT'])
def update_cart_item(item_id):
    data = request.get_json()
    if 'quantity' not in data:
        return jsonify({'error': 'Missing quantity'}), 400
    try:
        new_quantity = int(data['quantity'])
    except ValueError:
        return jsonify({'error': 'Invalid quantity'}), 400

    cart = get_cart_session()
    for idx, item in enumerate(cart):
        if item.get('id') == item_id:
            if new_quantity <= 0:
                cart.pop(idx)
            else:
                item['quantity'] = new_quantity
            save_cart_session(cart)
            return jsonify({'success': True, 'cart': cart})
    return jsonify({'error': 'Item not found'}), 404

@app.route('/api/cart/remove/<item_id>', methods=['DELETE'])
def remove_from_cart(item_id):
    cart = get_cart_session()
    new_cart = [item for item in cart if item.get('id') != item_id]
    save_cart_session(new_cart)
    return jsonify({'success': True, 'cart': new_cart})

@app.route('/api/cart/clear', methods=['DELETE'])
def clear_cart():
    save_cart_session([])
    return jsonify({'success': True})

# ========= ТОППЕРЫ (старые) =========
@app.route('/api/topper/sizes')
def topper_sizes():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, size FROM topper_sizes ORDER BY id")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for r in rows:
            size_str = r[1]
            width = None
            length = None
            if 'x' in size_str:
                parts = size_str.split('x')
                if len(parts) == 2:
                    try:
                        width = int(parts[0].strip())
                        length = int(parts[1].strip())
                    except:
                        pass
            result.append({
                'id': r[0],
                'name': size_str,
                'width': width,
                'length': length,
                'price': 0
            })
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /topper/sizes error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/topper/layers')
def topper_layers():
    size_id = request.args.get('size_id', type=int)
    if size_id is None:
        return jsonify({'error': 'Параметр size_id обязателен'}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT l.id, l.name, l.description, p.price, l.color_text, l.is_hidden
            FROM topper_layers l
            JOIN topper_layer_prices p ON l.id = p.layer_id
            WHERE p.size_id = %s AND (l.is_hidden IS NULL OR l.is_hidden = false)
            ORDER BY l.id
        """, (size_id,))
        rows = cur.fetchall()
        layer_ids = [r[0] for r in rows]
        main_features = {}
        if layer_ids:
            cur.execute("""
                SELECT lmf.layer_id, tmft.name AS feature_name, lmf.value
                FROM topper_layer_main_features lmf
                JOIN topper_layer_main_features_types tmft ON lmf.feature_id = tmft.id
                WHERE lmf.layer_id = ANY(%s)
            """, (layer_ids,))
            for layer_id, fname, fvalue in cur.fetchall():
                main_features.setdefault(layer_id, []).append({'name': fname, 'value': fvalue})
        extra_features = {}
        if layer_ids:
            cur.execute("""
                SELECT layer_id, name, value
                FROM topper_layer_extra_features
                WHERE layer_id = ANY(%s)
            """, (layer_ids,))
            for layer_id, name, value in cur.fetchall():
                extra_features.setdefault(layer_id, []).append({'name': name, 'value': value})
        cur.close()
        conn.close()
        result = []
        for r in rows:
            layer_id = r[0]
            result.append({
                'id': layer_id,
                'name': r[1],
                'description': r[2] or '',
                'price': r[3] or 0,
                'color_text': r[4] if r[4] is not None else '#000000',
                'is_hidden': r[5] if r[5] is not None else False,
                'main_features': main_features.get(layer_id, []),
                'extra_features': extra_features.get(layer_id, [])
            })
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /topper/layers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/topper/covers')
def topper_covers():
    size_id = request.args.get('size_id', type=int)
    if size_id is None:
        return jsonify({'error': 'Параметр size_id обязателен'}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.name, c.description, p.price, c.is_hidden
            FROM topper_covers c
            JOIN topper_cover_prices p ON c.id = p.cover_id
            WHERE p.size_id = %s AND (c.is_hidden IS NULL OR c.is_hidden = false)
            ORDER BY c.id
        """, (size_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{
            'id': r[0],
            'name': r[1],
            'description': r[2] or '',
            'price': r[3] or 0,
            'is_hidden': r[4] if r[4] is not None else False
        } for r in rows])
    except Exception as e:
        app.logger.error(f"API /topper/covers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/cart/add-topper', methods=['POST'])
def add_topper_to_cart():
    data = request.get_json()
    size_id = data.get('size_id')
    layer_ids = data.get('layer_ids', [])
    cover_id = data.get('cover_id')
    quantity = data.get('quantity', 1)

    if not size_id or not layer_ids or cover_id is None:
        return jsonify({'error': 'Необходимо указать размер, хотя бы один слой и чехол'}), 400

    if not isinstance(layer_ids, list) or len(layer_ids) == 0:
        return jsonify({'error': 'Выберите хотя бы один слой'}), 400

    try:
        quantity = int(quantity)
        if quantity < 1:
            quantity = 1
    except:
        quantity = 1

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, size FROM topper_sizes WHERE id = %s", (size_id,))
        size_row = cur.fetchone()
        if not size_row:
            return jsonify({'error': 'Размер не найден'}), 404
        size_name = size_row[1]

        unique_layer_ids = list(set(layer_ids))
        if unique_layer_ids:
            cur.execute("""
                SELECT l.id, l.name, COALESCE(p.price, 0)
                FROM topper_layers l
                LEFT JOIN topper_layer_prices p ON l.id = p.layer_id AND p.size_id = %s
                WHERE l.id = ANY(%s)
            """, (size_id, unique_layer_ids))
            layer_info = {row[0]: {'name': row[1], 'price': row[2]} for row in cur.fetchall()}
            layers_names = []
            total_layers_price = 0
            for lid in layer_ids:
                if lid not in layer_info:
                    return jsonify({'error': f'Слой с id {lid} не найден или не имеет цены для данного размера'}), 404
                layers_names.append(layer_info[lid]['name'])
                total_layers_price += layer_info[lid]['price']
        else:
            total_layers_price = 0
            layers_names = []

        cur.execute("""
            SELECT c.id, c.name, COALESCE(p.price, 0)
            FROM topper_covers c
            LEFT JOIN topper_cover_prices p ON c.id = p.cover_id AND p.size_id = %s
            WHERE c.id = %s
        """, (size_id, cover_id))
        cover_row = cur.fetchone()
        if not cover_row:
            return jsonify({'error': 'Чехол не найден или не имеет цены для данного размера'}), 404
        cover_price = cover_row[2] or 0
        cover_name = cover_row[1]

        total_price = total_layers_price + cover_price

        cart = get_cart_session()
        new_item = {
            'id': str(uuid.uuid4()),
            'type': 'topper',
            'product_code': 'TOPPER',
            'product_name': f'Собранный топпер ({size_name})',
            'material_code': '',
            'material_name': '',
            'cost': total_price,
            'quantity': quantity,
            'extra_data': {
                'size_id': size_id,
                'size_name': size_name,
                'layer_ids': layer_ids,
                'layer_names': layers_names,
                'cover_id': cover_id,
                'cover_name': cover_name,
                'total_price': total_price
            }
        }
        cart.append(new_item)
        save_cart_session(cart)

        return jsonify({'success': True, 'cart': cart})
    except Exception as e:
        app.logger.error(f"API /cart/add-topper error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

# ========= НОВЫЕ ЭНДПОИНТЫ ДЛЯ МАТРАЦЕВ =========

@app.route('/api/mattress/sizes')
def mattress_sizes():
    """Возвращает список размеров матрацев."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM mattress_sizes ORDER BY id")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for r in rows:
            size_str = r[1]
            width = None
            length = None
            if 'x' in size_str:
                parts = size_str.split('x')
                if len(parts) == 2:
                    try:
                        width = int(parts[0].strip())
                        length = int(parts[1].strip())
                    except:
                        pass
            result.append({
                'id': r[0],
                'name': size_str,
                'width': width,
                'length': length
            })
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /mattress/sizes error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/mattress/layers')
def mattress_layers():
    """Возвращает слои матрацев для указанного размера с ценами и характеристиками."""
    size_id = request.args.get('size_id', type=int)
    if size_id is None:
        return jsonify({'error': 'Параметр size_id обязателен'}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Получаем слои с ценами
        cur.execute("""
            SELECT l.id, l.code, l.name, l.description, p.price, l.color_text, l.is_hidden
            FROM mattress_layers l
            JOIN mattress_layers_prices p ON l.id = p.layer_id
            WHERE p.size_id = %s AND (l.is_hidden IS NULL OR l.is_hidden = false)
            ORDER BY l.id
        """, (size_id,))
        rows = cur.fetchall()
        layer_ids = [r[0] for r in rows]

        # Основные характеристики
        main_features = {}
        if layer_ids:
            cur.execute("""
                SELECT lmf.layer_id, tmft.name AS feature_name, lmf.value
                FROM mattress_layer_main_features lmf
                JOIN mattress_layer_main_features_types tmft ON lmf.feature_id = tmft.id
                WHERE lmf.layer_id = ANY(%s)
            """, (layer_ids,))
            for layer_id, fname, fvalue in cur.fetchall():
                main_features.setdefault(layer_id, []).append({'name': fname, 'value': fvalue})

        # Дополнительные характеристики
        extra_features = {}
        if layer_ids:
            cur.execute("""
                SELECT layer_id, name, value
                FROM mattress_layer_extra_features
                WHERE layer_id = ANY(%s)
            """, (layer_ids,))
            for layer_id, name, value in cur.fetchall():
                extra_features.setdefault(layer_id, []).append({'name': name, 'value': value})

        cur.close()
        conn.close()

        result = []
        for r in rows:
            layer_id = r[0]
            result.append({
                'id': layer_id,
                'code': r[1],
                'name': r[2],
                'description': r[3] or '',
                'price': r[4] or 0,
                'color_text': r[5] if r[5] is not None else '#000000',
                'is_hidden': r[6] if r[6] is not None else False,
                'main_features': main_features.get(layer_id, []),
                'extra_features': extra_features.get(layer_id, [])
            })
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /mattress/layers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/mattress/covers')
def mattress_covers():
    """Возвращает чехлы матрацев для указанного размера с ценами."""
    size_id = request.args.get('size_id', type=int)
    if size_id is None:
        return jsonify({'error': 'Параметр size_id обязателен'}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.code, c.name, c.description, p.price, c.is_hidden
            FROM mattress_cover c
            JOIN mattress_cover_prices p ON c.id = p.cover_id
            WHERE p.size_id = %s AND (c.is_hidden IS NULL OR c.is_hidden = false)
            ORDER BY c.id
        """, (size_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{
            'id': r[0],
            'code': r[1],
            'name': r[2],
            'description': r[3] or '',
            'price': r[4] or 0,
            'is_hidden': r[5] if r[5] is not None else False
        } for r in rows])
    except Exception as e:
        app.logger.error(f"API /mattress/covers error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/cart/add-mattress', methods=['POST'])
def add_mattress_to_cart():
    """Добавляет собранный матрац в корзину."""
    data = request.get_json()
    size_id = data.get('size_id')
    layer_ids = data.get('layer_ids', [])
    cover_id = data.get('cover_id')
    initial_height = data.get('initial_height', 0)
    is_new = data.get('is_new', True)  # если true, initial_height = 0
    quantity = data.get('quantity', 1)

    if not size_id or not layer_ids or cover_id is None:
        return jsonify({'error': 'Необходимо указать размер, хотя бы один слой и чехол'}), 400

    if not isinstance(layer_ids, list) or len(layer_ids) == 0:
        return jsonify({'error': 'Выберите хотя бы один слой'}), 400

    try:
        initial_height = int(initial_height)
        if initial_height < 0:
            initial_height = 0
    except:
        initial_height = 0

    try:
        quantity = int(quantity)
        if quantity < 1:
            quantity = 1
    except:
        quantity = 1

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Получаем размер
        cur.execute("SELECT id, name FROM mattress_sizes WHERE id = %s", (size_id,))
        size_row = cur.fetchone()
        if not size_row:
            return jsonify({'error': 'Размер не найден'}), 404
        size_name = size_row[1]

        # Получаем информацию о слоях (цены)
        unique_layer_ids = list(set(layer_ids))
        if unique_layer_ids:
            cur.execute("""
                SELECT l.id, l.code, l.name, COALESCE(p.price, 0)
                FROM mattress_layers l
                LEFT JOIN mattress_layers_prices p ON l.id = p.layer_id AND p.size_id = %s
                WHERE l.id = ANY(%s)
            """, (size_id, unique_layer_ids))
            layer_info = {row[0]: {'code': row[1], 'name': row[2], 'price': row[3]} for row in cur.fetchall()}
            layers_codes = []
            layers_names = []
            total_layers_price = 0
            for lid in layer_ids:
                if lid not in layer_info:
                    return jsonify({'error': f'Слой с id {lid} не найден или не имеет цены для данного размера'}), 404
                layers_codes.append(layer_info[lid]['code'])
                layers_names.append(layer_info[lid]['name'])
                total_layers_price += layer_info[lid]['price']
        else:
            total_layers_price = 0
            layers_codes = []
            layers_names = []

        # Получаем цену чехла
        cur.execute("""
            SELECT c.id, c.code, c.name, COALESCE(p.price, 0)
            FROM mattress_cover c
            LEFT JOIN mattress_cover_prices p ON c.id = p.cover_id AND p.size_id = %s
            WHERE c.id = %s
        """, (size_id, cover_id))
        cover_row = cur.fetchone()
        if not cover_row:
            return jsonify({'error': 'Чехол не найден или не имеет цены для данного размера'}), 404
        cover_price = cover_row[3] or 0
        cover_code = cover_row[1]
        cover_name = cover_row[2]

        total_price = total_layers_price + cover_price
        total_height = initial_height + len(layer_ids) * 5

        cart = get_cart_session()
        new_item = {
            'id': str(uuid.uuid4()),
            'type': 'mattress',
            'product_code': 'MATTRESS',
            'product_name': f'Собранный матрац ({size_name})',
            'material_code': '',
            'material_name': '',
            'cost': total_price,
            'quantity': quantity,
            'extra_data': {
                'size_id': size_id,
                'size_name': size_name,
                'initial_height': initial_height,
                'is_new': is_new,
                'layer_ids': layer_ids,
                'layer_codes': layers_codes,
                'layer_names': layers_names,
                'layer_prices': [layer_info[lid]['price'] for lid in layer_ids],
                'cover_id': cover_id,
                'cover_code': cover_code,
                'cover_name': cover_name,
                'cover_price': cover_price,
                'total_price': total_price,
                'total_height': total_height
            }
        }
        cart.append(new_item)
        save_cart_session(cart)

        return jsonify({'success': True, 'cart': cart})
    except Exception as e:
        app.logger.error(f"API /cart/add-mattress error: {e}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500
    finally:
        cur.close()
        conn.close()

# ========= ОФОРМЛЕНИЕ ЗАКАЗА (обновлённое) =========
@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.get_json()
    required = ['user_name', 'phone']
    if not all(k in data for k in required):
        return jsonify({'error': 'Укажите имя и телефон'}), 400

    cart = get_cart_session()
    if not cart:
        return jsonify({'error': 'Корзина пуста'}), 400

    session_id = session.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id

    user_id = session.get('user_id')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Разделяем элементы корзины на обычные товары, топперы и матрацы
        product_items = [item for item in cart if item.get('type') in ('product', 'topper')]
        mattress_items = [item for item in cart if item.get('type') == 'mattress']

        # Создаём заказ для обычных товаров (если есть)
        order_id = None
        if product_items:
            cur.execute("""
                INSERT INTO orders (session_id, user_id, user_name, phone, email, address, comment, contact_time, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (session_id, user_id, data['user_name'], data['phone'], data.get('email', ''),
                  data.get('address', ''), data.get('comment', ''), data.get('contact_time', ''),
                  'Ожидает подтверждения'))
            order_id = cur.fetchone()[0]

            total_sum = 0
            for item in product_items:
                item_total = item['cost'] * item.get('quantity', 1)
                total_sum += item_total

                if item.get('type') == 'topper' and item.get('extra_data'):
                    extra_json = json.dumps(item['extra_data'])
                    cur.execute("""
                        INSERT INTO order_items (order_id, product_code, product_name, material_code, material_name, cost, quantity, extra_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (order_id, item['product_code'], item['product_name'],
                          item.get('material_code', ''), item.get('material_name', ''),
                          item['cost'], item.get('quantity', 1), extra_json))
                else:
                    cur.execute("""
                        INSERT INTO order_items (order_id, product_code, product_name, material_code, material_name, cost, quantity)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (order_id, item['product_code'], item['product_name'],
                          item['material_code'], item['material_name'],
                          item['cost'], item.get('quantity', 1)))
            # Отправляем уведомление для обычного заказа
            if order_id:
                send_order_notification(order_id, data['user_name'], data['phone'], total_sum, order_type='product')

        # Создаём заказы для матрацев (каждый матрац - отдельный заказ)
        mattress_order_ids = []
        for mattress_item in mattress_items:
            extra = mattress_item.get('extra_data', {})
            # Извлекаем данные
            size_id = extra.get('size_id')
            initial_height = extra.get('initial_height', 0)
            cover_id = extra.get('cover_id')
            cover_price = extra.get('cover_price', 0)
            layer_ids = extra.get('layer_ids', [])
            layer_codes = extra.get('layer_codes', [])
            layer_names = extra.get('layer_names', [])
            layer_prices = extra.get('layer_prices', [])

            if not size_id or not cover_id or not layer_ids:
                app.logger.error(f"Некорректные данные матраца в корзине: {extra}")
                continue

            # Вставляем заказ матраца
            cur.execute("""
                INSERT INTO mattress_orders
                (session_id, user_id, user_name, phone, email, address, comment, contact_time, status,
                 size_id, initial_height, cover_id, cover_price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (session_id, user_id, data['user_name'], data['phone'], data.get('email', ''),
                  data.get('address', ''), data.get('comment', ''), data.get('contact_time', ''),
                  'Ожидает подтверждения', size_id, initial_height, cover_id, cover_price))
            mattress_order_id = cur.fetchone()[0]
            mattress_order_ids.append(mattress_order_id)

            # Вставляем слои заказа
            total_layers_price = 0
            for idx, layer_id in enumerate(layer_ids):
                price = layer_prices[idx] if idx < len(layer_prices) else 0
                total_layers_price += price
                cur.execute("""
                    INSERT INTO mattress_order_items
                    (order_id, layer_id, layer_code, layer_name, quantity, price_per_unit, total_price)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (mattress_order_id, layer_id, layer_codes[idx] if idx < len(layer_codes) else '',
                      layer_names[idx] if idx < len(layer_names) else '', 1, price, price))

            # Отправляем уведомление для заказа матраца
            total_mattress_price = cover_price + total_layers_price
            send_order_notification(mattress_order_id, data['user_name'], data['phone'], total_mattress_price, order_type='mattress')

        conn.commit()
        # Очищаем корзину
        session.pop('cart', None)
        session.modified = True

        # Возвращаем список id созданных заказов
        response = {'success': True}
        if order_id:
            response['order_id'] = order_id
        if mattress_order_ids:
            response['mattress_order_ids'] = mattress_order_ids
        return jsonify(response)

    except Exception as e:
        conn.rollback()
        app.logger.error(f"Order creation error: {e}")
        return jsonify({'error': 'Ошибка при создании заказа'}), 500
    finally:
        cur.close()
        conn.close()

# ========= СТАТИЧЕСКИЕ МАРШРУТЫ =========
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/index.css')
def index_css():
    return send_from_directory('.', 'index.css')

@app.route('/product/<code>')
def product_page(code):
    return send_from_directory('webpages', 'product_card.html')

@app.route('/cart')
def cart_page():
    return send_from_directory('webpages', 'cart.html')

@app.route('/catalog')
def catalog_page():
    return send_from_directory('webpages', 'catalog.html')

@app.route('/catalog.css')
def catalog_css():
    return send_from_directory('webpages', 'catalog.css')

@app.route('/product_card.css')
def product_card_css():
    return send_from_directory('webpages', 'product_card.css')

@app.route('/cart.css')
def cart_css():
    return send_from_directory('webpages', 'cart.css')

@app.route('/login')
def login_page():
    return send_from_directory('webpages', 'login.html')

@app.route('/register')
def register_page():
    return send_from_directory('webpages', 'register.html')

@app.route('/profile')
def profile_page():
    if 'user_id' not in session:
        return redirect('/login?next=/profile')
    return send_from_directory('webpages', 'profile.html')

@app.route('/gallery')
def gallery_page():
    return send_from_directory('webpages', 'gallery.html')

@app.route('/gallery.css')
def gallery_css():
    return send_from_directory('webpages', 'gallery.css')

@app.route('/toppers')
def toppers_page():
    return send_from_directory('webpages', 'toppers.html')

@app.route('/toppers.css')
def toppers_css():
    return send_from_directory('webpages', 'toppers.css')

# Новые маршруты для конструктора матрацев
@app.route('/mattress')
def mattress_page():
    return send_from_directory('webpages', 'mattress.html')

@app.route('/mattress.css')
def mattress_css():
    return send_from_directory('webpages', 'mattress.css')

@app.route('/media/<path:filename>')
def media_files(filename):
    return send_from_directory('media', filename)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('media/interface', 'favicon.png')

if __name__ == '__main__':
    Path('webpages').mkdir(exist_ok=True)
    Path('media/products').mkdir(parents=True, exist_ok=True)
    Path('media/materials').mkdir(parents=True, exist_ok=True)
    Path('media/interface').mkdir(parents=True, exist_ok=True)
    Path('media/index').mkdir(parents=True, exist_ok=True)
    Path('media/gallery').mkdir(parents=True, exist_ok=True)
    Path('media/layers').mkdir(parents=True, exist_ok=True)
    Path('media/covers').mkdir(parents=True, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)