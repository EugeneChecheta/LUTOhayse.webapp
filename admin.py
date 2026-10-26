# admin.py
#!/usr/bin/env python3
"""
Flask Blueprint для управления заказами (продукты и матрасы), клиентами, галереей
и конструктором матрасов (слои, чехлы, размеры, цены, характеристики) в админ-панели.
"""

import io
import json
import logging
import os
import shutil
import stat
from decimal import Decimal
from datetime import datetime, date, time
from pathlib import Path

import psycopg2
from flask import Blueprint, request, session, send_file
from flask import jsonify as _flask_jsonify
from werkzeug.security import generate_password_hash

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None
    PIL_AVAILABLE = False

try:
    from openpyxl import Workbook as _XLWorkbook
    from openpyxl.styles import (
        Font as _XLFont, Alignment as _XLAlign,
        Border as _XLBorder, Side as _XLSide, PatternFill as _XLFill,
    )
    from openpyxl.utils import get_column_letter as _xl_col_letter
    OPENPYXL_AVAILABLE = True
except ImportError:  # pragma: no cover
    OPENPYXL_AVAILABLE = False

logger = logging.getLogger(__name__)


# ------------------------- JSON-сериализация -------------------------
def _json_safe(obj):
    """Рекурсивно приводит значения из БД к JSON-сериализуемым типам."""
    if isinstance(obj, Decimal):
        if obj == obj.to_integral_value():
            return int(obj)
        return float(obj)
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(obj, date):
        return obj.strftime('%Y-%m-%d')
    if isinstance(obj, time):
        return obj.strftime('%H:%M:%S')
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def jsonify(data):
    """Обёртка над flask.jsonify, предварительно приводящая данные к JSON-типам."""
    return _flask_jsonify(_json_safe(data))


def _to_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _send_excel_bytes(data: io.BytesIO, filename: str):
    """Совместимость send_file между версиями Flask."""
    data.seek(0)
    mimetype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    try:
        return send_file(
            data,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )
    except TypeError:
        # Flask < 2.0
        return send_file(
            data,
            mimetype=mimetype,
            as_attachment=True,
            attachment_filename=filename,
        )


# ------------------------- Конфигурация -------------------------
BASE_DIR = Path(__file__).resolve().parent

PAGE_SIZE = 8
CLIENTS_PAGE_SIZE = 8
CLIENT_ORDERS_PAGE_SIZE = 5
GALLERY_PAGE_SIZE = 8
MATTRESS_PAGE_SIZE = 8

STATUSES = [
    "Ожидает подтверждения",
    "ожидает оплаты",
    "в сборке",
    "в пути",
    "доставлен",
]

MEDIA_BASE = BASE_DIR.parent / "media" if (BASE_DIR.parent / "media").exists() else BASE_DIR / "media"
GALLERY_BASE = MEDIA_BASE / "gallery"
COMMERCIAL_BASE = MEDIA_BASE / "commercial"
MATTRESS_LAYERS_MEDIA = MEDIA_BASE / "layers"
MATTRESS_COVERS_MEDIA = MEDIA_BASE / "covers"

GALLERY_BASE.mkdir(parents=True, exist_ok=True)
COMMERCIAL_BASE.mkdir(parents=True, exist_ok=True)
MATTRESS_LAYERS_MEDIA.mkdir(parents=True, exist_ok=True)
MATTRESS_COVERS_MEDIA.mkdir(parents=True, exist_ok=True)

GALLERY_SECTIONS = {
    'gallery':    {'base': GALLERY_BASE,    'title': 'Индивидуальная галерея'},
    'commercial': {'base': COMMERCIAL_BASE, 'title': 'Коммерческая галерея'},
}


def _read_db_config():
    candidates = [
        BASE_DIR / 'config_db.txt',
        BASE_DIR / 'telegram_admin_panel' / 'config_db.txt',
        BASE_DIR.parent / 'config_db.txt',
        BASE_DIR.parent / 'telegram_admin_panel' / 'config_db.txt',
    ]
    for config_path in candidates:
        if not config_path.exists():
            continue
        config = {}
        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    config[k.strip()] = v.strip()
        required = ('database', 'user', 'password')
        if all(key in config for key in required):
            logger.info(f"admin.py: использую конфиг БД {config_path}")
            return config
        logger.warning(f"admin.py: в {config_path} не хватает обязательных ключей {required}")

    raise FileNotFoundError(
        "config_db.txt не найден ни по одному из путей: "
        + ", ".join(str(p) for p in candidates)
    )


_DB_CONFIG = _read_db_config()


def _get_db_connection():
    return psycopg2.connect(
        host=_DB_CONFIG.get('host', 'localhost'),
        port=_DB_CONFIG.get('port', '5432'),
        database=_DB_CONFIG['database'],
        user=_DB_CONFIG['user'],
        password=_DB_CONFIG['password'],
    )


def _ensure_mattress_tables():
    """
    Проверяет и создает необходимые таблицы и колонки для раздела матрасов,
    чтобы предотвратить ошибки 500 при отсутствии структуры в БД.
    """
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        
        # 1. Таблицы заказов матрасов
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mattress_orders (
                id SERIAL PRIMARY KEY,
                user_name TEXT,
                phone TEXT,
                email TEXT,
                address TEXT,
                comment TEXT,
                contact_time TEXT,
                order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'Ожидает подтверждения'
            );
        """)
        cur.execute("ALTER TABLE mattress_orders ADD COLUMN IF NOT EXISTS size_id INTEGER REFERENCES mattress_sizes(id) ON DELETE SET NULL;")
        cur.execute("ALTER TABLE mattress_orders ADD COLUMN IF NOT EXISTS cover_id INTEGER REFERENCES mattress_cover(id) ON DELETE SET NULL;")
        cur.execute("ALTER TABLE mattress_orders ADD COLUMN IF NOT EXISTS initial_height NUMERIC DEFAULT 0;")
        cur.execute("ALTER TABLE mattress_orders ADD COLUMN IF NOT EXISTS cover_price NUMERIC DEFAULT 0;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS mattress_order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES mattress_orders(id) ON DELETE CASCADE,
                layer_code TEXT,
                layer_name TEXT,
                quantity INTEGER DEFAULT 1,
                price_per_unit NUMERIC DEFAULT 0,
                total_price NUMERIC DEFAULT 0
            );
        """)

        # 2. Таблицы характеристик (аналогично toppers_bot.py)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mattress_layer_main_features_types (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mattress_layer_main_features (
                id SERIAL PRIMARY KEY,
                layer_id INTEGER REFERENCES mattress_layers(id) ON DELETE CASCADE,
                feature_id INTEGER REFERENCES mattress_layer_main_features_types(id) ON DELETE CASCADE,
                value TEXT NOT NULL,
                UNIQUE(layer_id, feature_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mattress_layer_extra_features (
                id SERIAL PRIMARY KEY,
                layer_id INTEGER REFERENCES mattress_layers(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                value TEXT NOT NULL
            );
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info("admin.py: Схема таблиц матрасов успешно проверена/обновлена.")
    except Exception as e:
        logger.error(f"admin.py: Ошибка при инициализации таблиц матрасов: {e}")


# ------------------------- Blueprint -------------------------
admin_bp = Blueprint('admin_orders', __name__)


def _require_admin():
    return bool(session.get('admin_authenticated'))


def _unauthorized():
    return jsonify({'error': 'Не авторизован'}), 401


def _format_dt(dt):
    if not dt:
        return None
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _format_dt_ru(dt):
    if not dt:
        return None
    return dt.strftime('%d.%m.%Y %H:%M')


def _json_body():
    data = request.get_json(force=True, silent=True)
    if data is None:
        data = {}
    return data


# ============================================================
#                    СТАТУСЫ
# ============================================================
@admin_bp.route('/api/admin/orders/statuses')
def get_statuses():
    if not _require_admin():
        return _unauthorized()
    return jsonify({'statuses': STATUSES})


# ============================================================
#                    ЗАКАЗЫ ПРОДУКТОВ
# ============================================================
@admin_bp.route('/api/admin/orders/products')
def list_product_orders():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM orders")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * PAGE_SIZE

        cur.execute(
            "SELECT id, user_name, phone, order_date, status FROM orders "
            "ORDER BY order_date DESC OFFSET %s LIMIT %s",
            (offset, PAGE_SIZE),
        )
        rows = cur.fetchall()
        orders = [{
            'id': r[0],
            'user_name': r[1],
            'phone': r[2],
            'order_date': _format_dt(r[3]),
            'status': r[4],
        } for r in rows]

        return jsonify({
            'orders': orders,
            'total': total,
            'page': page,
            'page_size': PAGE_SIZE,
            'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: list_product_orders")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/products/<int:order_id>')
def product_order_details(order_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, user_name, phone, email, address, comment,
                   contact_time, order_date, status
            FROM orders WHERE id = %s
        """, (order_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Заказ не найден'}), 404

        order = {
            'id': r[0],
            'user_name': r[1],
            'phone': r[2],
            'email': r[3],
            'address': r[4],
            'comment': r[5],
            'contact_time': r[6],
            'order_date': _format_dt(r[7]),
            'status': r[8],
        }

        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='order_items' AND column_name='extra_data'
        """)
        has_extra = cur.fetchone()[0] > 0

        if has_extra:
            cur.execute("""
                SELECT product_code, product_name, material_code, material_name,
                       cost, quantity, extra_data
                FROM order_items WHERE order_id = %s ORDER BY id
            """, (order_id,))
        else:
            cur.execute("""
                SELECT product_code, product_name, material_code, material_name,
                       cost, quantity, NULL::text AS extra_data
                FROM order_items WHERE order_id = %s ORDER BY id
            """, (order_id,))

        items = []
        total_sum = 0
        for it in cur.fetchall():
            qty = it[5] if it[5] else 1
            item_total = (it[4] or 0) * qty
            total_sum += item_total
            extra = None
            is_topper = False
            if it[6]:
                if isinstance(it[6], dict):
                    extra = it[6]
                    is_topper = True
                else:
                    try:
                        extra = json.loads(it[6])
                        is_topper = True
                    except (ValueError, TypeError):
                        extra = None
            items.append({
                'product_code': it[0],
                'product_name': it[1],
                'material_code': it[2],
                'material_name': it[3],
                'cost': it[4],
                'quantity': qty,
                'total': item_total,
                'extra_data': extra,
                'is_topper': is_topper,
            })

        return jsonify({
            'order': order,
            'items': items,
            'total_sum': total_sum,
        })
    except Exception as e:
        logger.exception("admin.py: product_order_details")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
#                    ЗАКАЗЫ МАТРАСОВ
# ============================================================
@admin_bp.route('/api/admin/orders/mattresses')
def list_mattress_orders():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM mattress_orders")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * PAGE_SIZE

        cur.execute("""
            SELECT mo.id, mo.user_name, mo.phone, mo.order_date, mo.status,
                   ms.name AS size_name
            FROM mattress_orders mo
            LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
            ORDER BY mo.order_date DESC OFFSET %s LIMIT %s
        """, (offset, PAGE_SIZE))
        rows = cur.fetchall()
        orders = [{
            'id': r[0],
            'user_name': r[1],
            'phone': r[2],
            'order_date': _format_dt(r[3]),
            'status': r[4],
            'size_name': r[5] or '—',
        } for r in rows]

        return jsonify({
            'orders': orders,
            'total': total,
            'page': page,
            'page_size': PAGE_SIZE,
            'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: list_mattress_orders")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>')
def mattress_order_details(order_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT mo.id, mo.user_name, mo.phone, mo.email, mo.address, mo.comment,
                   mo.contact_time, mo.order_date, mo.status, mo.initial_height,
                   mo.cover_price, ms.name AS size_name,
                   mc.name AS cover_name, mc.code AS cover_code
            FROM mattress_orders mo
            LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
            LEFT JOIN mattress_cover mc ON mo.cover_id = mc.id
            WHERE mo.id = %s
        """, (order_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Заказ не найден'}), 404

        order = {
            'id': r[0],
            'user_name': r[1],
            'phone': r[2],
            'email': r[3],
            'address': r[4],
            'comment': r[5],
            'contact_time': r[6],
            'order_date': _format_dt(r[7]),
            'status': r[8],
            'initial_height': r[9] or 0,
            'cover_price': r[10] or 0,
            'size_name': r[11] or '—',
            'cover_name': r[12] or '—',
            'cover_code': r[13] or '',
        }

        cur.execute("""
            SELECT layer_code, layer_name, quantity, price_per_unit, total_price
            FROM mattress_order_items WHERE order_id = %s ORDER BY id
        """, (order_id,))
        items = []
        layers_total = 0
        layers_height = 0
        for it in cur.fetchall():
            qty = it[2] or 1
            item_total = it[4] if it[4] is not None else (it[3] or 0) * qty
            layers_total += item_total
            layers_height += 5 * qty
            items.append({
                'layer_code': it[0],
                'layer_name': it[1],
                'quantity': qty,
                'price_per_unit': it[3] or 0,
                'total_price': item_total,
            })

        total_height = order['initial_height'] + layers_height
        grand_total = layers_total + order['cover_price']

        return jsonify({
            'order': order,
            'items': items,
            'layers_total': layers_total,
            'layers_height': layers_height,
            'total_height': total_height,
            'grand_total': grand_total,
        })
    except Exception as e:
        logger.exception("admin.py: mattress_order_details")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
#                ИЗМЕНЕНИЕ СТАТУСА / УДАЛЕНИЕ
# ============================================================
@admin_bp.route('/api/admin/orders/products/<int:order_id>/status', methods=['PUT'])
def update_product_status(order_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_status = (data.get('status') or '').strip()
    if new_status not in STATUSES:
        return jsonify({'error': 'Недопустимый статус'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("UPDATE orders SET status = %s WHERE id = %s", (new_status, order_id))
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Заказ не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: update_product_status")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>/status', methods=['PUT'])
def update_mattress_status(order_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_status = (data.get('status') or '').strip()
    if new_status not in STATUSES:
        return jsonify({'error': 'Недопустимый статус'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("UPDATE mattress_orders SET status = %s WHERE id = %s",
                    (new_status, order_id))
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Заказ не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: update_mattress_status")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/products/<int:order_id>', methods=['DELETE'])
def delete_product_order(order_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Заказ не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: delete_product_order")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>', methods=['DELETE'])
def delete_mattress_order(order_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mattress_orders WHERE id = %s", (order_id,))
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Заказ не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: delete_mattress_order")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
#              ДРУГИЕ ЗАКАЗЫ КЛИЕНТА
# ============================================================
@admin_bp.route('/api/admin/orders/customer/<phone>')
def customer_orders(phone):
    if not _require_admin():
        return _unauthorized()
    exclude_type = request.args.get('exclude_type', '')
    exclude_id = request.args.get('exclude_id', type=int)

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, user_name, phone, order_date, status FROM orders "
            "WHERE phone = %s ORDER BY order_date DESC",
            (phone,),
        )
        products = []
        for r in cur.fetchall():
            if exclude_type == 'product' and r[0] == exclude_id:
                continue
            products.append({
                'id': r[0],
                'user_name': r[1],
                'phone': r[2],
                'order_date': _format_dt(r[3]),
                'status': r[4],
            })

        cur.execute("""
            SELECT mo.id, mo.user_name, mo.phone, mo.order_date, mo.status,
                   ms.name AS size_name
            FROM mattress_orders mo
            LEFT JOIN mattress_sizes ms ON mo.size_id = ms.id
            WHERE mo.phone = %s ORDER BY mo.order_date DESC
        """, (phone,))
        mattresses = []
        for r in cur.fetchall():
            if exclude_type == 'mattress' and r[0] == exclude_id:
                continue
            mattresses.append({
                'id': r[0],
                'user_name': r[1],
                'phone': r[2],
                'order_date': _format_dt(r[3]),
                'status': r[4],
                'size_name': r[5] or '—',
            })

        return jsonify({
            'phone': phone,
            'products': products,
            'mattresses': mattresses,
        })
    except Exception as e:
        logger.exception("admin.py: customer_orders")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
#                    КЛИЕНТЫ
# ============================================================
@admin_bp.route('/api/admin/clients')
def list_clients():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * CLIENTS_PAGE_SIZE

        cur.execute(
            "SELECT id, phone, full_name FROM users ORDER BY id OFFSET %s LIMIT %s",
            (offset, CLIENTS_PAGE_SIZE),
        )
        clients = [{'id': r[0], 'phone': r[1], 'full_name': r[2]} for r in cur.fetchall()]

        return jsonify({
            'clients': clients,
            'total': total,
            'page': page,
            'page_size': CLIENTS_PAGE_SIZE,
            'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: list_clients")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/search')
def search_clients():
    if not _require_admin():
        return _unauthorized()
    phone = (request.args.get('phone') or '').strip()
    if not phone:
        return jsonify({'error': 'Введите номер телефона'}), 400

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, phone, full_name FROM users WHERE phone ILIKE %s ORDER BY id",
            (f'%{phone}%',),
        )
        clients = [{'id': r[0], 'phone': r[1], 'full_name': r[2]} for r in cur.fetchall()]
        return jsonify({'clients': clients})
    except Exception as e:
        logger.exception("admin.py: search_clients")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/<int:client_id>')
def get_client_details(client_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, phone, full_name, email, address, contact_time, created_at, password_hash "
            "FROM users WHERE id = %s",
            (client_id,),
        )
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Клиент не найден'}), 404

        password_hash = r[7]
        if isinstance(password_hash, (bytes, bytearray)):
            try:
                password_hash = password_hash.decode('utf-8', errors='replace')
            except Exception:
                password_hash = str(password_hash)

        return jsonify({
            'client': {
                'id': r[0],
                'phone': r[1],
                'full_name': r[2],
                'email': r[3],
                'address': r[4],
                'contact_time': r[5],
                'created_at': _format_dt_ru(r[6]),
                'password_hash': password_hash,
            }
        })
    except Exception as e:
        logger.exception("admin.py: get_client_details")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/<int:client_id>', methods=['PUT'])
def update_client(client_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()

    allowed = {'phone', 'full_name', 'email', 'address', 'contact_time', 'password'}
    updates = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k == 'password':
            if v:
                updates['password_hash'] = generate_password_hash(v)
        else:
            updates[k] = v

    if not updates:
        return jsonify({'error': 'Нет данных для обновления'}), 400

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        if 'phone' in updates:
            cur.execute("SELECT id FROM users WHERE phone = %s AND id != %s",
                        (updates['phone'], client_id))
            if cur.fetchone():
                return jsonify({'error': 'Такой номер телефона уже занят другим клиентом'}), 400

        set_parts = []
        values = []
        for col, val in updates.items():
            set_parts.append(f"{col} = %s")
            values.append(val)
        values.append(client_id)

        cur.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE id = %s", values)
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Клиент не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: update_client")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/<int:client_id>', methods=['DELETE'])
def delete_client(client_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM users WHERE id = %s", (client_id,))
        affected = cur.rowcount
        conn.commit()
        if affected == 0:
            return jsonify({'error': 'Клиент не найден'}), 404
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: delete_client")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/<int:client_id>/orders')
def get_client_orders(client_id):
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM orders WHERE user_id = %s", (client_id,))
        total = cur.fetchone()[0]
        total_pages = max(1, (total + CLIENT_ORDERS_PAGE_SIZE - 1) // CLIENT_ORDERS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * CLIENT_ORDERS_PAGE_SIZE

        cur.execute(
            """SELECT o.id, o.order_date, o.status,
                      COALESCE((SELECT SUM(cost * quantity) FROM order_items WHERE order_id = o.id), 0) AS total
               FROM orders o
               WHERE o.user_id = %s
               ORDER BY o.order_date DESC
               OFFSET %s LIMIT %s""",
            (client_id, offset, CLIENT_ORDERS_PAGE_SIZE),
        )
        orders = [{
            'id': r[0],
            'order_date': _format_dt_ru(r[1]),
            'status': r[2],
            'total': r[3],
        } for r in cur.fetchall()]

        return jsonify({
            'orders': orders,
            'total': total,
            'page': page,
            'page_size': CLIENT_ORDERS_PAGE_SIZE,
            'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: get_client_orders")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/clients/<int:client_id>/orders/<int:order_id>')
def get_client_order_details(client_id, order_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, order_date, status, user_name, phone, email, address,
                   comment, contact_time
            FROM orders WHERE id = %s AND user_id = %s
        """, (order_id, client_id))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Заказ не найден'}), 404

        order = {
            'id': r[0],
            'order_date': _format_dt_ru(r[1]),
            'status': r[2],
            'user_name': r[3],
            'phone': r[4],
            'email': r[5],
            'address': r[6],
            'comment': r[7],
            'contact_time': r[8],
        }

        cur.execute("""
            SELECT product_code, product_name, material_code, material_name,
                   cost, quantity
            FROM order_items WHERE order_id = %s ORDER BY id
        """, (order_id,))
        items = []
        total_sum = 0
        for it in cur.fetchall():
            qty = it[5] or 1
            item_total = (it[4] or 0) * qty
            total_sum += item_total
            items.append({
                'product_code': it[0],
                'product_name': it[1],
                'material_code': it[2],
                'material_name': it[3],
                'cost': it[4] or 0,
                'quantity': qty,
                'total': item_total,
            })

        return jsonify({
            'order': order,
            'items': items,
            'total_sum': total_sum,
        })
    except Exception as e:
        logger.exception("admin.py: get_client_order_details")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ============================================================
#                    ГАЛЕРЕЯ
# ============================================================
def _gallery_section_info(section):
    return GALLERY_SECTIONS.get(section)


def _gallery_validate_name(name):
    if not name:
        return False
    if '/' in name or '\\' in name or '..' in name:
        return False
    return True


def _gallery_projects(section):
    info = _gallery_section_info(section)
    if not info:
        return []
    base = info['base']
    if not base.exists():
        return []
    items = [p.name for p in base.iterdir() if p.is_dir()]
    items.sort(reverse=True)
    return items


def _gallery_project_path(section, name):
    info = _gallery_section_info(section)
    if not info:
        return None
    return info['base'] / name


def _gallery_photo_paths(section, name):
    d = _gallery_project_path(section, name)
    if not d or not d.exists():
        return []
    files = [p for p in d.glob("*.webp") if p.stem.isdigit()]
    files.sort(key=lambda p: int(p.stem))
    return files


def _gallery_read_description(section, name):
    d = _gallery_project_path(section, name)
    if not d:
        return ""
    path = d / "description.txt"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def _gallery_write_description(section, name, text):
    d = _gallery_project_path(section, name)
    if not d:
        return
    d.mkdir(parents=True, exist_ok=True)
    _ensure_write_permission(d)
    path = d / "description.txt"
    path.write_text(text, encoding="utf-8")
    _ensure_write_permission(path)


def _gallery_read_captions(section, name):
    d = _gallery_project_path(section, name)
    if not d:
        return {}
    path = d / "captions.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _gallery_write_caption(section, name, index, caption):
    d = _gallery_project_path(section, name)
    if not d:
        return
    d.mkdir(parents=True, exist_ok=True)
    _ensure_write_permission(d)
    captions = _gallery_read_captions(section, name)
    if caption:
        captions[str(index)] = caption
    else:
        captions.pop(str(index), None)
    path = d / "captions.json"
    path.write_text(
        json.dumps(captions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _ensure_write_permission(path)


def _gallery_unique_name(section):
    base = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    name = base
    counter = 1
    while _gallery_project_path(section, name).exists():
        name = f"{base}-{counter}"
        counter += 1
        if counter > 1000:
            raise RuntimeError("Не удалось сгенерировать уникальное имя проекта")
    return name


def _ensure_write_permission(path):
    try:
        if path.is_dir():
            os.chmod(path, 0o777)
        else:
            os.chmod(path, 0o777)
    except Exception as e:
        logger.warning(f"Не удалось изменить права для {path}: {e}")


def _remove_readonly(func, path, excinfo):
    try:
        os.chmod(path, 0o777)
        func(path)
    except Exception as e:
        logger.error(f"Не удалось удалить {path}: {e}")
        raise


def _force_delete_directory(path):
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                os.chmod(file_path, 0o777)
            except Exception as e:
                logger.warning(f"Не удалось изменить права для файла {file_path}: {e}")
        for name in dirs:
            dir_path = os.path.join(root, name)
            try:
                os.chmod(dir_path, 0o777)
            except Exception as e:
                logger.warning(f"Не удалось изменить права для папки {dir_path}: {e}")
    try:
        os.chmod(path, 0o777)
    except Exception as e:
        logger.warning(f"Не удалось изменить права для корневой папки {path}: {e}")
    shutil.rmtree(path, onerror=_remove_readonly)


def _gallery_save_photo_file(section, name, file_storage, caption=""):
    if not PIL_AVAILABLE:
        logger.error("admin.py: PIL не установлен, невозможно сохранить фото")
        return None
    try:
        project_path = _gallery_project_path(section, name)
        project_path.mkdir(parents=True, exist_ok=True)
        _ensure_write_permission(project_path)

        file_bytes = file_storage.read()
        img = Image.open(io.BytesIO(file_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = rgb_img

        existing = _gallery_photo_paths(section, name)
        next_num = (max(int(p.stem) for p in existing) + 1) if existing else 1

        filepath = project_path / f"{next_num}.webp"
        img.save(filepath, 'WEBP', quality=85)
        _ensure_write_permission(filepath)

        if caption:
            _gallery_write_caption(section, name, next_num, caption)

        return next_num
    except Exception as e:
        logger.error(f"admin.py: ошибка сохранения фото галереи: {e}")
        return None


@admin_bp.route('/api/admin/gallery/sections')
def gallery_list_sections():
    if not _require_admin():
        return _unauthorized()
    return jsonify({
        'sections': [
            {'key': 'gallery',    'title': GALLERY_SECTIONS['gallery']['title']},
            {'key': 'commercial', 'title': GALLERY_SECTIONS['commercial']['title']},
        ]
    })


@admin_bp.route('/api/admin/gallery/<section>/projects')
def gallery_list_projects(section):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400

    page = max(0, request.args.get('page', 0, type=int))
    projects = _gallery_projects(section)
    total = len(projects)
    total_pages = max(1, (total + GALLERY_PAGE_SIZE - 1) // GALLERY_PAGE_SIZE)
    if page >= total_pages:
        page = total_pages - 1
    offset = page * GALLERY_PAGE_SIZE
    page_items = projects[offset:offset + GALLERY_PAGE_SIZE]

    result = []
    for pname in page_items:
        photos = _gallery_photo_paths(section, pname)
        desc = _gallery_read_description(section, pname)
        result.append({
            'name': pname,
            'description': desc,
            'photo_count': len(photos),
        })

    return jsonify({
        'section': section,
        'title': _gallery_section_info(section)['title'],
        'projects': result,
        'total': total,
        'page': page,
        'page_size': GALLERY_PAGE_SIZE,
        'total_pages': total_pages,
    })


@admin_bp.route('/api/admin/gallery/<section>/projects', methods=['POST'])
def gallery_create_project(section):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not PIL_AVAILABLE:
        return jsonify({'error': 'На сервере отсутствует библиотека Pillow'}), 500

    try:
        name = _gallery_unique_name(section)
        project_path = _gallery_project_path(section, name)
        project_path.mkdir(parents=True, exist_ok=True)
        _ensure_write_permission(project_path)

        description = (request.form.get('description') or '').strip()
        if description:
            _gallery_write_description(section, name, description)

        files = request.files.getlist('photos')
        saved = []
        for f in files:
            if f and f.filename:
                num = _gallery_save_photo_file(section, name, f)
                if num is not None:
                    saved.append(num)

        return jsonify({
            'success': True,
            'name': name,
            'description': description,
            'photos_saved': saved,
        })
    except Exception as e:
        logger.exception("admin.py: gallery_create_project")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>')
def gallery_get_project(section, name):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404

    photos = _gallery_photo_paths(section, name)
    captions = _gallery_read_captions(section, name)
    desc = _gallery_read_description(section, name)

    photos_data = []
    for p in photos:
        idx = int(p.stem)
        photos_data.append({
            'index': idx,
            'url': f"/media/{section}/{name}/{p.stem}.webp",
            'caption': captions.get(p.stem, ''),
        })

    return jsonify({
        'project': {
            'name': name,
            'description': desc,
            'photos': photos_data,
        }
    })


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>/description', methods=['PUT'])
def gallery_update_description(section, name):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404

    data = _json_body()
    text = (data.get('description') or '').strip()
    _gallery_write_description(section, name, text)
    return jsonify({'success': True, 'description': text})


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>/photos', methods=['POST'])
def gallery_upload_photos(section, name):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    if not PIL_AVAILABLE:
        return jsonify({'error': 'На сервере отсутствует библиотека Pillow'}), 500
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404

    files = request.files.getlist('photos')
    if not files:
        return jsonify({'error': 'Не переданы файлы'}), 400

    saved = []
    for f in files:
        if f and f.filename:
            num = _gallery_save_photo_file(section, name, f)
            if num is not None:
                saved.append(num)

    if not saved:
        return jsonify({'error': 'Не удалось сохранить ни одного фото'}), 500
    return jsonify({'success': True, 'saved': saved})


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>/photos/<int:index>/caption', methods=['PUT'])
def gallery_update_caption(section, name, index):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404
    filepath = project_path / f"{index}.webp"
    if not filepath.exists():
        return jsonify({'error': 'Фото не найдено'}), 404

    data = _json_body()
    caption = (data.get('caption') or '').strip()
    _gallery_write_caption(section, name, index, caption)
    return jsonify({'success': True})


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>/photos/<int:index>', methods=['DELETE'])
def gallery_delete_photo(section, name, index):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404
    filepath = project_path / f"{index}.webp"
    if not filepath.exists():
        return jsonify({'error': 'Фото не найдено'}), 404

    try:
        _ensure_write_permission(project_path)
        _ensure_write_permission(filepath)

        filepath.unlink()
        captions = _gallery_read_captions(section, name)
        captions.pop(str(index), None)

        remaining = _gallery_photo_paths(section, name)
        new_caps = {}
        new_idx = 1
        for p in remaining:
            cur = int(p.stem)
            cap = captions.get(str(cur), '')
            if cur != new_idx:
                new_path = project_path / f"{new_idx}.webp"
                _ensure_write_permission(p)
                p.rename(new_path)
            if cap:
                new_caps[str(new_idx)] = cap
            new_idx += 1

        captions_path = project_path / "captions.json"
        if new_caps:
            captions_path.write_text(
                json.dumps(new_caps, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _ensure_write_permission(captions_path)
        elif captions_path.exists():
            _ensure_write_permission(captions_path)
            captions_path.unlink()

        return jsonify({'success': True})
    except Exception as e:
        logger.exception("admin.py: gallery_delete_photo")
        return jsonify({'error': str(e)}), 500


@admin_bp.route('/api/admin/gallery/<section>/projects/<name>', methods=['DELETE'])
def gallery_delete_project(section, name):
    if not _require_admin():
        return _unauthorized()
    if not _gallery_section_info(section):
        return jsonify({'error': 'Неверный раздел'}), 400
    if not _gallery_validate_name(name):
        return jsonify({'error': 'Неверное имя проекта'}), 400
    project_path = _gallery_project_path(section, name)
    if not project_path.exists():
        return jsonify({'error': 'Проект не найден'}), 404
    try:
        _force_delete_directory(project_path)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("admin.py: gallery_delete_project")
        return jsonify({'error': str(e)}), 500


# ============================================================
#        КОНСТРУКТОР МАТРАСОВ: СЛОИ, ЧЕХЛЫ, РАЗМЕРЫ, ЦЕНЫ, ХАРАКТЕРИСТИКИ
# ============================================================

# ---------- Медиа-файлы ----------
def _layer_photo_path(code):
    return MATTRESS_LAYERS_MEDIA / f"{code}.webp"


def _cover_photo_path(code):
    return MATTRESS_COVERS_MEDIA / f"{code}.webp"


def _save_mattress_photo(file_storage, target_path):
    if not PIL_AVAILABLE:
        return False
    try:
        file_bytes = file_storage.read()
        img = Image.open(io.BytesIO(file_bytes))
        if img.mode in ('RGBA', 'LA', 'P'):
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = rgb_img
        target_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(target_path, 'WEBP', quality=85)
        try:
            os.chmod(target_path, 0o666)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"admin.py: ошибка сохранения фото матраса: {e}")
        return False


# ---------- СЛОИ ----------
@admin_bp.route('/api/admin/mattresses/layers')
def mattresses_layers_list():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM mattress_layers")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + MATTRESS_PAGE_SIZE - 1) // MATTRESS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * MATTRESS_PAGE_SIZE
        cur.execute(
            "SELECT id, code, name, description FROM mattress_layers "
            "ORDER BY code OFFSET %s LIMIT %s",
            (offset, MATTRESS_PAGE_SIZE),
        )
        layers = []
        for r in cur.fetchall():
            has_photo = _layer_photo_path(r[1]).exists()
            layers.append({
                'id': r[0],
                'code': r[1],
                'name': r[2],
                'description': r[3] or '',
                'has_photo': has_photo,
                'photo_url': f"/media/layers/{r[1]}.webp" if has_photo else None,
            })
        return jsonify({
            'layers': layers, 'total': total, 'page': page,
            'page_size': MATTRESS_PAGE_SIZE, 'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_layers_list")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>')
def mattresses_layer_detail(layer_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, code, name, description FROM mattress_layers WHERE id = %s",
            (layer_id,),
        )
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Слой не найден'}), 404
        has_photo = _layer_photo_path(r[1]).exists()
        return jsonify({
            'layer': {
                'id': r[0],
                'code': r[1],
                'name': r[2],
                'description': r[3] or '',
                'has_photo': has_photo,
                'photo_url': f"/media/layers/{r[1]}.webp" if has_photo else None,
            }
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_layer_detail")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers', methods=['POST'])
def mattresses_layer_create():
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    code = (data.get('code') or '').strip()
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()

    if not code or not name:
        return jsonify({'error': 'Код и название обязательны'}), 400

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM mattress_layers WHERE code = %s", (code,))
        if cur.fetchone():
            return jsonify({'error': 'Слой с таким кодом уже существует'}), 400

        cur.execute(
            "INSERT INTO mattress_layers (code, name, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (code, name, description),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_layer_create")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>', methods=['PUT'])
def mattresses_layer_update(layer_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_code = (data.get('code') or '').strip()
    new_name = (data.get('name') or '').strip()
    new_desc = data.get('description', '') or ''

    if not new_code or not new_name:
        return jsonify({'error': 'Код и название обязательны'}), 400

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_layers WHERE id = %s", (layer_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Слой не найден'}), 404
        old_code = r[0]

        if new_code != old_code:
            cur.execute("SELECT id FROM mattress_layers WHERE code = %s AND id != %s",
                        (new_code, layer_id))
            if cur.fetchone():
                return jsonify({'error': 'Слой с таким кодом уже существует'}), 400

        cur.execute(
            "UPDATE mattress_layers SET code=%s, name=%s, description=%s WHERE id=%s",
            (new_code, new_name, new_desc, layer_id),
        )
        conn.commit()

        if new_code != old_code:
            old_p = _layer_photo_path(old_code)
            new_p = _layer_photo_path(new_code)
            if old_p.exists():
                try:
                    if new_p.exists():
                        new_p.unlink()
                    old_p.rename(new_p)
                except Exception as e:
                    logger.warning(f"admin.py: не удалось переименовать фото слоя: {e}")

        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_layer_update")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>', methods=['DELETE'])
def mattresses_layer_delete(layer_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_layers WHERE id = %s", (layer_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Слой не найден'}), 404
        code = r[0]

        cur.execute("DELETE FROM mattress_layers_prices WHERE layer_id = %s", (layer_id,))
        cur.execute("DELETE FROM mattress_layer_main_features WHERE layer_id = %s", (layer_id,))
        cur.execute("DELETE FROM mattress_layer_extra_features WHERE layer_id = %s", (layer_id,))
        cur.execute("DELETE FROM mattress_layers WHERE id = %s", (layer_id,))
        conn.commit()

        photo = _layer_photo_path(code)
        if photo.exists():
            try:
                photo.unlink()
            except Exception as e:
                logger.warning(f"admin.py: не удалось удалить фото слоя: {e}")

        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_layer_delete")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/photo', methods=['POST'])
def mattresses_layer_photo_upload(layer_id):
    if not _require_admin():
        return _unauthorized()
    if not PIL_AVAILABLE:
        return jsonify({'error': 'На сервере отсутствует библиотека Pillow'}), 500

    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_layers WHERE id = %s", (layer_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Слой не найден'}), 404
        code = r[0]
    finally:
        cur.close()
        conn.close()

    file = request.files.get('photo')
    if not file or not file.filename:
        return jsonify({'error': 'Файл не передан'}), 400

    target = _layer_photo_path(code)
    if target.exists():
        try:
            target.unlink()
        except Exception:
            pass

    if not _save_mattress_photo(file, target):
        return jsonify({'error': 'Не удалось сохранить фото'}), 500
    return jsonify({'success': True})


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/photo', methods=['DELETE'])
def mattresses_layer_photo_delete(layer_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_layers WHERE id = %s", (layer_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Слой не найден'}), 404
        code = r[0]
    finally:
        cur.close()
        conn.close()

    target = _layer_photo_path(code)
    if target.exists():
        try:
            target.unlink()
        except Exception as e:
            logger.warning(f"admin.py: не удалось удалить фото: {e}")
            return jsonify({'error': str(e)}), 500
    return jsonify({'success': True})


# ---------- ЧЕХЛЫ ----------
@admin_bp.route('/api/admin/mattresses/covers')
def mattresses_covers_list():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM mattress_cover")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + MATTRESS_PAGE_SIZE - 1) // MATTRESS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * MATTRESS_PAGE_SIZE
        cur.execute(
            "SELECT id, code, name, description FROM mattress_cover "
            "ORDER BY code OFFSET %s LIMIT %s",
            (offset, MATTRESS_PAGE_SIZE),
        )
        covers = []
        for r in cur.fetchall():
            has_photo = _cover_photo_path(r[1]).exists()
            covers.append({
                'id': r[0],
                'code': r[1],
                'name': r[2],
                'description': r[3] or '',
                'has_photo': has_photo,
                'photo_url': f"/media/covers/{r[1]}.webp" if has_photo else None,
            })
        return jsonify({
            'covers': covers, 'total': total, 'page': page,
            'page_size': MATTRESS_PAGE_SIZE, 'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_covers_list")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/covers/<int:cover_id>')
def mattresses_cover_detail(cover_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        logger.exception("admin.py: ошибка подключения к БД")
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, code, name, description FROM mattress_cover WHERE id = %s",
            (cover_id,),
        )
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Чехол не найден'}), 404
        has_photo = _cover_photo_path(r[1]).exists()
        return jsonify({
            'cover': {
                'id': r[0],
                'code': r[1],
                'name': r[2],
                'description': r[3] or '',
                'has_photo': has_photo,
                'photo_url': f"/media/covers/{r[1]}.webp" if has_photo else None,
            }
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_cover_detail")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/covers', methods=['POST'])
def mattresses_cover_create():
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    code = (data.get('code') or '').strip()
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()
    if not code or not name:
        return jsonify({'error': 'Код и название обязательны'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM mattress_cover WHERE code = %s", (code,))
        if cur.fetchone():
            return jsonify({'error': 'Чехол с таким кодом уже существует'}), 400
        cur.execute(
            "INSERT INTO mattress_cover (code, name, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (code, name, description),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_cover_create")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/covers/<int:cover_id>', methods=['PUT'])
def mattresses_cover_update(cover_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_code = (data.get('code') or '').strip()
    new_name = (data.get('name') or '').strip()
    new_desc = data.get('description', '') or ''
    if not new_code or not new_name:
        return jsonify({'error': 'Код и название обязательны'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_cover WHERE id = %s", (cover_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Чехол не найден'}), 404
        old_code = r[0]
        if new_code != old_code:
            cur.execute("SELECT id FROM mattress_cover WHERE code = %s AND id != %s",
                        (new_code, cover_id))
            if cur.fetchone():
                return jsonify({'error': 'Чехол с таким кодом уже существует'}), 400
        cur.execute(
            "UPDATE mattress_cover SET code=%s, name=%s, description=%s WHERE id=%s",
            (new_code, new_name, new_desc, cover_id),
        )
        conn.commit()
        if new_code != old_code:
            old_p = _cover_photo_path(old_code)
            new_p = _cover_photo_path(new_code)
            if old_p.exists():
                try:
                    if new_p.exists():
                        new_p.unlink()
                    old_p.rename(new_p)
                except Exception as e:
                    logger.warning(f"admin.py: не удалось переименовать фото чехла: {e}")
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_cover_update")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/covers/<int:cover_id>', methods=['DELETE'])
def mattresses_cover_delete(cover_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_cover WHERE id = %s", (cover_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Чехол не найден'}), 404
        code = r[0]
        cur.execute("DELETE FROM mattress_cover_prices WHERE cover_id = %s", (cover_id,))
        cur.execute("DELETE FROM mattress_cover WHERE id = %s", (cover_id,))
        conn.commit()
        photo = _cover_photo_path(code)
        if photo.exists():
            try:
                photo.unlink()
            except Exception as e:
                logger.warning(f"admin.py: не удалось удалить фото чехла: {e}")
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_cover_delete")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/covers/<int:cover_id>/photo', methods=['POST'])
def mattresses_cover_photo_upload(cover_id):
    if not _require_admin():
        return _unauthorized()
    if not PIL_AVAILABLE:
        return jsonify({'error': 'На сервере отсутствует библиотека Pillow'}), 500
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_cover WHERE id = %s", (cover_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Чехол не найден'}), 404
        code = r[0]
    finally:
        cur.close()
        conn.close()

    file = request.files.get('photo')
    if not file or not file.filename:
        return jsonify({'error': 'Файл не передан'}), 400
    target = _cover_photo_path(code)
    if target.exists():
        try:
            target.unlink()
        except Exception:
            pass
    if not _save_mattress_photo(file, target):
        return jsonify({'error': 'Не удалось сохранить фото'}), 500
    return jsonify({'success': True})


@admin_bp.route('/api/admin/mattresses/covers/<int:cover_id>/photo', methods=['DELETE'])
def mattresses_cover_photo_delete(cover_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT code FROM mattress_cover WHERE id = %s", (cover_id,))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'Чехол не найден'}), 404
        code = r[0]
    finally:
        cur.close()
        conn.close()
    target = _cover_photo_path(code)
    if target.exists():
        try:
            target.unlink()
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'success': True})


# ---------- РАЗМЕРЫ ----------
@admin_bp.route('/api/admin/mattresses/sizes')
def mattresses_sizes_list():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM mattress_sizes")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + MATTRESS_PAGE_SIZE - 1) // MATTRESS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * MATTRESS_PAGE_SIZE
        cur.execute(
            "SELECT id, name FROM mattress_sizes ORDER BY name OFFSET %s LIMIT %s",
            (offset, MATTRESS_PAGE_SIZE),
        )
        sizes = [{'id': r[0], 'name': r[1]} for r in cur.fetchall()]
        return jsonify({
            'sizes': sizes, 'total': total, 'page': page,
            'page_size': MATTRESS_PAGE_SIZE, 'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_sizes_list")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/sizes/all')
def mattresses_sizes_all():
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name FROM mattress_sizes ORDER BY name")
        sizes = [{'id': r[0], 'name': r[1]} for r in cur.fetchall()]
        return jsonify({'sizes': sizes})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/sizes', methods=['POST'])
def mattresses_size_create():
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Название обязательно'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM mattress_sizes WHERE name = %s", (name,))
        if cur.fetchone():
            return jsonify({'error': 'Размер с таким названием уже существует'}), 400
        cur.execute("INSERT INTO mattress_sizes (name) VALUES (%s) RETURNING id", (name,))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/sizes/<int:size_id>', methods=['PUT'])
def mattresses_size_update(size_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_name = (data.get('name') or '').strip()
    if not new_name:
        return jsonify({'error': 'Название обязательно'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM mattress_sizes WHERE name = %s AND id != %s",
                    (new_name, size_id))
        if cur.fetchone():
            return jsonify({'error': 'Размер с таким названием уже существует'}), 400
        cur.execute("UPDATE mattress_sizes SET name = %s WHERE id = %s", (new_name, size_id))
        if cur.rowcount == 0:
            return jsonify({'error': 'Размер не найден'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/sizes/<int:size_id>', methods=['DELETE'])
def mattresses_size_delete(size_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mattress_layers_prices WHERE size_id = %s", (size_id,))
        cur.execute("DELETE FROM mattress_cover_prices WHERE size_id = %s", (size_id,))
        cur.execute("DELETE FROM mattress_sizes WHERE id = %s", (size_id,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Размер не найден'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ---------- ЦЕНЫ ----------
@admin_bp.route('/api/admin/mattresses/prices/<string:price_type>/<int:entity_id>')
def mattresses_prices_list(price_type, entity_id):
    if not _require_admin():
        return _unauthorized()
    if price_type not in ('layers', 'covers'):
        return jsonify({'error': 'Неверный тип'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name FROM mattress_sizes ORDER BY name")
        all_sizes = [{'id': r[0], 'name': r[1]} for r in cur.fetchall()]

        if price_type == 'layers':
            cur.execute(
                "SELECT size_id, price FROM mattress_layers_prices WHERE layer_id = %s",
                (entity_id,),
            )
        else:
            cur.execute(
                "SELECT size_id, price FROM mattress_cover_prices WHERE cover_id = %s",
                (entity_id,),
            )
        prices_map = {r[0]: r[1] for r in cur.fetchall()}

        result = []
        for s in all_sizes:
            result.append({
                'size_id': s['id'],
                'size_name': s['name'],
                'price': prices_map.get(s['id']),
            })
        return jsonify({
            'price_type': price_type,
            'entity_id': entity_id,
            'prices': result,
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_prices_list")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/prices/<string:price_type>/<int:entity_id>/<int:size_id>', methods=['PUT'])
def mattresses_price_set(price_type, entity_id, size_id):
    if not _require_admin():
        return _unauthorized()
    if price_type not in ('layers', 'covers'):
        return jsonify({'error': 'Неверный тип'}), 400
    data = _json_body()
    raw = data.get('price')
    if raw is None or raw == '':
        price = None
    else:
        try:
            price = int(raw)
            if price < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({'error': 'Введите положительное целое число'}), 400

    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        if price is None:
            if price_type == 'layers':
                cur.execute(
                    "DELETE FROM mattress_layers_prices WHERE layer_id = %s AND size_id = %s",
                    (entity_id, size_id),
                )
            else:
                cur.execute(
                    "DELETE FROM mattress_cover_prices WHERE cover_id = %s AND size_id = %s",
                    (entity_id, size_id),
                )
        else:
            if price_type == 'layers':
                cur.execute(
                    "INSERT INTO mattress_layers_prices (layer_id, size_id, price) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (layer_id, size_id) DO UPDATE SET price = EXCLUDED.price",
                    (entity_id, size_id, price),
                )
            else:
                cur.execute(
                    "INSERT INTO mattress_cover_prices (cover_id, size_id, price) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (cover_id, size_id) DO UPDATE SET price = EXCLUDED.price",
                    (entity_id, size_id, price),
                )
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.exception("admin.py: mattresses_price_set")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ---------- ОСНОВНЫЕ ХАРАКТЕРИСТИКИ (СПРАВОЧНИК) ----------
@admin_bp.route('/api/admin/mattresses/feature-types')
def mattresses_feature_types_list():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM mattress_layer_main_features_types")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + MATTRESS_PAGE_SIZE - 1) // MATTRESS_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
        offset = page * MATTRESS_PAGE_SIZE
        cur.execute(
            "SELECT id, name FROM mattress_layer_main_features_types "
            "ORDER BY name OFFSET %s LIMIT %s",
            (offset, MATTRESS_PAGE_SIZE),
        )
        types = [{'id': r[0], 'name': r[1]} for r in cur.fetchall()]
        return jsonify({
            'types': types, 'total': total, 'page': page,
            'page_size': MATTRESS_PAGE_SIZE, 'total_pages': total_pages,
        })
    except Exception as e:
        logger.exception("admin.py: mattresses_feature_types_list")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/feature-types', methods=['POST'])
def mattresses_feature_type_create():
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Название обязательно'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO mattress_layer_main_features_types (name) "
                    "VALUES (%s) RETURNING id", (name,))
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/feature-types/<int:feat_id>', methods=['PUT'])
def mattresses_feature_type_update(feat_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    new_name = (data.get('name') or '').strip()
    if not new_name:
        return jsonify({'error': 'Название обязательно'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("UPDATE mattress_layer_main_features_types SET name = %s WHERE id = %s",
                    (new_name, feat_id))
        if cur.rowcount == 0:
            return jsonify({'error': 'Не найдено'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/feature-types/<int:feat_id>', methods=['DELETE'])
def mattresses_feature_type_delete(feat_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mattress_layer_main_features_types WHERE id = %s", (feat_id,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Не найдено'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ---------- ОСНОВНЫЕ ХАРАКТЕРИСТИКИ СЛОЯ ----------
@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/main-features')
def mattresses_layer_main_features(layer_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT mft.id, mft.name, COALESCE(pmf.value, '')
            FROM mattress_layer_main_features_types mft
            LEFT JOIN mattress_layer_main_features pmf
                ON mft.id = pmf.feature_id AND pmf.layer_id = %s
            ORDER BY mft.name
        """, (layer_id,))
        features = [{'feature_id': r[0], 'feature_name': r[1], 'value': r[2]}
                    for r in cur.fetchall()]
        return jsonify({'features': features})
    except Exception as e:
        logger.exception("admin.py: mattresses_layer_main_features")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/main-features/<int:feature_id>', methods=['PUT'])
def mattresses_layer_main_feature_set(layer_id, feature_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    value = (data.get('value') or '').strip()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO mattress_layer_main_features (layer_id, feature_id, value) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (layer_id, feature_id) DO UPDATE SET value = EXCLUDED.value",
            (layer_id, feature_id, value),
        )
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ---------- ДОПОЛНИТЕЛЬНЫЕ ХАРАКТЕРИСТИКИ СЛОЯ ----------
@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/extra-features')
def mattresses_layer_extra_features(layer_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, value FROM mattress_layer_extra_features "
            "WHERE layer_id = %s ORDER BY name",
            (layer_id,),
        )
        features = [{'id': r[0], 'name': r[1], 'value': r[2]} for r in cur.fetchall()]
        return jsonify({'features': features})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/layers/<int:layer_id>/extra-features', methods=['POST'])
def mattresses_layer_extra_feature_create(layer_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    name = (data.get('name') or '').strip()
    value = (data.get('value') or '').strip()
    if not name:
        return jsonify({'error': 'Название обязательно'}), 400
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO mattress_layer_extra_features (layer_id, name, value) "
            "VALUES (%s, %s, %s) RETURNING id",
            (layer_id, name, value),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return jsonify({'success': True, 'id': new_id})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/extra-features/<int:feat_id>', methods=['PUT'])
def mattresses_layer_extra_feature_update(feat_id):
    if not _require_admin():
        return _unauthorized()
    data = _json_body()
    name = data.get('name')
    value = data.get('value')
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        set_parts, vals = [], []
        if name is not None:
            set_parts.append("name = %s")
            vals.append(_to_str(name))
        if value is not None:
            set_parts.append("value = %s")
            vals.append(_to_str(value))
        if not set_parts:
            return jsonify({'error': 'Нет данных'}), 400
        vals.append(feat_id)
        cur.execute(
            f"UPDATE mattress_layer_extra_features SET {', '.join(set_parts)} WHERE id = %s",
            vals,
        )
        if cur.rowcount == 0:
            return jsonify({'error': 'Не найдено'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/mattresses/extra-features/<int:feat_id>', methods=['DELETE'])
def mattresses_layer_extra_feature_delete(feat_id):
    if not _require_admin():
        return _unauthorized()
    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM mattress_layer_extra_features WHERE id = %s", (feat_id,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Не найдено'}), 404
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ---------- ЭКСПОРТ В EXCEL ----------
@admin_bp.route('/api/admin/mattresses/export')
def mattresses_export():
    if not _require_admin():
        return _unauthorized()
    if not OPENPYXL_AVAILABLE:
        return jsonify({'error': 'openpyxl не установлен на сервере'}), 500

    try:
        conn = _get_db_connection()
    except Exception as e:
        return jsonify({'error': f'Ошибка подключения к БД: {e}'}), 500
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, code FROM mattress_layers ORDER BY code")
        layers = cur.fetchall()
        cur.execute("SELECT id, code FROM mattress_cover ORDER BY code")
        covers = cur.fetchall()
        cur.execute("SELECT id, name FROM mattress_sizes ORDER BY name")
        sizes = cur.fetchall()
        cur.execute("SELECT layer_id, size_id, price FROM mattress_layers_prices")
        layer_prices = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        cur.execute("SELECT cover_id, size_id, price FROM mattress_cover_prices")
        cover_prices = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        wb = _XLWorkbook()
        ws = wb.active
        ws.title = "000000"

        headers = ["Размер"]
        for l in layers:
            headers.append(l[1])
        for c in covers:
            headers.append(c[1])
        ws.append(headers)

        def excel_price(v):
            if v is None:
                return ""
            if isinstance(v, Decimal):
                if v == v.to_integral_value():
                    return int(v)
                return float(v)
            return v

        for size in sizes:
            row = [size[1]]
            for layer in layers:
                row.append(excel_price(layer_prices.get((layer[0], size[0]), "")))
            for cover in covers:
                row.append(excel_price(cover_prices.get((cover[0], size[0]), "")))
            ws.append(row)

        header_font = _XLFont(bold=True)
        header_fill = _XLFill("solid", fgColor="DDDDDD")
        thin = _XLSide(style="thin", color="000000")
        border = _XLBorder(left=thin, right=thin, top=thin, bottom=thin)

        for cell in ws[1]:
            cell.font = header_font
            cell.alignment = _XLAlign(horizontal="center", vertical="center", wrap_text=True)
            cell.fill = header_fill
            cell.border = border

        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=ws.max_column):
            for cell in row:
                cell.border = border
                if cell.column == 1:
                    cell.alignment = _XLAlign(horizontal="left", vertical="center")
                else:
                    cell.alignment = _XLAlign(horizontal="center", vertical="center")

        ws.freeze_panes = "A2"

        for col_idx in range(1, ws.max_column + 1):
            letter = _xl_col_letter(col_idx)
            max_len = 0
            for row_idx in range(1, ws.max_row + 1):
                value = ws.cell(row=row_idx, column=col_idx).value
                if value is not None:
                    max_len = max(max_len, len(str(value)))
            ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 50)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        return _send_excel_bytes(output, '000000.xlsx')
    except Exception as e:
        logger.exception("admin.py: mattresses_export")
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# Автоматическая инициализация схемы таблиц при импорте модуля
_ensure_mattress_tables()