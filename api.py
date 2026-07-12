import psycopg2
import os
from flask import Flask, jsonify, send_from_directory, abort, request, session
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

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(24))
CORS(app, supports_credentials=True)


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


def send_order_notification(order_id, user_name, phone, total_sum):
    """Отправляет уведомление о новом заказе всем администраторам через Telegram бота (urllib)."""
    if not ORDER_BOT_TOKEN or not ORDER_ADMIN_IDS:
        app.logger.warning("Не настроен бот для уведомлений о заказах – сообщение не отправлено")
        return

    text = (
        f"🆕 *Новый заказ!*\n"
        f"Номер: #{order_id}\n"
        f"Клиент: {user_name}\n"
        f"Телефон: {phone}\n"
        f"Сумма: {total_sum} ₽"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "📋 Посмотреть заказ", "callback_data": f"order_details_{order_id}_0"}
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


# ----- Вспомогательная функция для создания таблиц, если их нет -----
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
            quantity INTEGER DEFAULT 1
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

    conn.commit()
    cur.close()
    conn.close()


init_db()  # Вызов при старте


# ----- API: список типов товаров (без изменений) -----
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


# ----- API: список флагов (тегов) -----
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


# ----- API: список товаров с фильтрацией (ИСПРАВЛЕНО: добавлен min_cost) -----
@app.route('/api/products')
def products():
    try:
        type_id = request.args.get('type_id', type=int)
        flag_ids = request.args.getlist('flag_ids', type=int)

        conn = get_db_connection()
        cur = conn.cursor()

        hidden_flag_condition = """
            NOT EXISTS (
                SELECT 1 FROM flags_for_products fp
                JOIN flags f ON fp.flags_id = f.id
                WHERE fp.products_id = p.id AND lower(f.name) = 'скрытый'
            )
        """

        if flag_ids:
            query = f"""
                SELECT p.code, p.name, p.min_cost
                FROM products p
                JOIN flags_for_products fp ON p.id = fp.products_id
                WHERE 1=1
                AND {hidden_flag_condition}
            """
            params = []
            if type_id is not None:
                query += " AND p.products_type_id = %s"
                params.append(type_id)
            query += " AND fp.flags_id = ANY(%s)"
            params.append(flag_ids)
            query += """
                GROUP BY p.id, p.code, p.name, p.min_cost
                HAVING COUNT(DISTINCT fp.flags_id) = %s
                ORDER BY p.id
            """
            params.append(len(flag_ids))
            cur.execute(query, params)
        else:
            query = f"""
                SELECT code, name, min_cost
                FROM products p
                WHERE 1=1
                AND {hidden_flag_condition}
            """
            params = []
            if type_id is not None:
                query += " AND products_type_id = %s"
                params.append(type_id)
            query += " ORDER BY id"
            cur.execute(query, params)

        rows = cur.fetchall()
        cur.close()
        conn.close()
        # ИСПРАВЛЕНО: возвращаем min_cost
        return jsonify([{'code': r[0], 'name': r[1], 'min_cost': r[2]} for r in rows])
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


# ----- API: детальная информация о товаре по коду (ИСПРАВЛЕНО: добавлен min_cost) -----
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
            'min_cost': product_row[3],          # ИСПРАВЛЕНО
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
            'min_cost': product_data['min_cost']   # ИСПРАВЛЕНО: для обратной совместимости на фронтенде
        }
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /api/product/<code> error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


# ----- API: список материалов -----
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


# ========= ИСТОРИЯ ЗАКАЗОВ =========
@app.route('/api/orders/history')
def order_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Не авторизован'}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, user_name, phone, email, address, comment, contact_time, order_date, status
            FROM orders
            WHERE user_id = %s
            ORDER BY order_date DESC
        """, (user_id,))
        orders = []
        for row in cur.fetchall():
            orders.append({
                'id': row[0],
                'user_name': row[1],
                'phone': row[2],
                'email': row[3],
                'address': row[4],
                'comment': row[5],
                'contact_time': row[6],
                'order_date': row[7].isoformat(),
                'status': row[8]
            })
        # Для каждого заказа получим позиции
        for order in orders:
            cur.execute("""
                SELECT product_code, product_name, material_name, cost, quantity
                FROM order_items
                WHERE order_id = %s
            """, (order['id'],))
            items = [{'product_code': r[0], 'product_name': r[1], 'material_name': r[2], 'cost': r[3], 'quantity': r[4]} for r in cur.fetchall()]
            order['items'] = items
            order['total'] = sum(it['cost'] * it['quantity'] for it in items)
        return jsonify(orders)
    except Exception as e:
        app.logger.error(f"Order history error: {e}")
        return jsonify({'error': 'Server error'}), 500
    finally:
        cur.close()
        conn.close()


# ========= КОРЗИНА (сессия) =========
def get_cart_session():
    """Возвращает список товаров в корзине из сессии"""
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
        if item['product_code'] == data['product_code'] and item['material_id'] == data['material_id']:
            item['quantity'] = item.get('quantity', 1) + quantity
            save_cart_session(cart)
            return jsonify({'success': True, 'cart': cart})

    new_item = {
        'id': str(uuid.uuid4()),
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


# ========= ОФОРМЛЕНИЕ ЗАКАЗА (с привязкой к пользователю, если авторизован) =========
@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.get_json()
    required = ['user_name', 'phone']
    if not all(k in data for k in required):
        return jsonify({'error': 'Укажите имя и телефон'}), 400

    cart = get_cart_session()
    if not cart:
        return jsonify({'error': 'Корзина пуста'}), 400

    # session_id всегда генерируется, если отсутствует
    session_id = session.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id

    user_id = session.get('user_id')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO orders (session_id, user_id, user_name, phone, email, address, comment, contact_time, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (session_id, user_id, data['user_name'], data['phone'], data.get('email', ''),
              data.get('address', ''), data.get('comment', ''), data.get('contact_time', ''),
              'Ожидает подтверждения'))
        order_id = cur.fetchone()[0]

        total_sum = 0
        for item in cart:
            item_total = item['cost'] * item.get('quantity', 1)
            total_sum += item_total
            cur.execute("""
                INSERT INTO order_items (order_id, product_code, product_name, material_code, material_name, cost, quantity)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (order_id, item['product_code'], item['product_name'], item['material_code'], item['material_name'],
                  item['cost'], item.get('quantity', 1)))

        conn.commit()

        # Отправка уведомления
        send_order_notification(order_id, data['user_name'], data['phone'], total_sum)

        # Очистка корзины
        session.pop('cart', None)
        session.modified = True

        return jsonify({'success': True, 'order_id': order_id})
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
    return send_from_directory('webpages', 'profile.html')


@app.route('/media/<path:filename>')
def media_files(filename):
    return send_from_directory('media', filename)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory('media/interface', 'favicon.png')


# ----- Запуск -----
if __name__ == '__main__':
    Path('webpages').mkdir(exist_ok=True)
    Path('media/products').mkdir(parents=True, exist_ok=True)
    Path('media/materials').mkdir(parents=True, exist_ok=True)
    Path('media/interface').mkdir(parents=True, exist_ok=True)
    Path('media/index').mkdir(parents=True, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)