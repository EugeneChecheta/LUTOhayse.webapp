# admin.py
#!/usr/bin/env python3
"""
Flask Blueprint для управления заказами (продукты и матрасы) в админ-панели.
Регистрируется в основном приложении api.py:

    from admin import admin_bp
    app.register_blueprint(admin_bp)

Использует ту же сессию, что и OTP-авторизация из api.py
(session['admin_authenticated']), поэтому отдельный вход не требуется.
"""

import json
from pathlib import Path

import psycopg2
from flask import Blueprint, jsonify, request, session

# ------------------------- Конфигурация -------------------------
BASE_DIR = Path(__file__).resolve().parent

PAGE_SIZE = 8
STATUSES = [
    "Ожидает подтверждения",
    "ожидает оплаты",
    "в сборке",
    "в пути",
    "доставлен",
]


def _read_db_config():
    config_path = BASE_DIR / 'telegram_admin_panel' / 'config_db.txt'
    config = {}
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                config[k] = v
    return config


_DB_CONFIG = _read_db_config()


def _get_db_connection():
    return psycopg2.connect(
        host=_DB_CONFIG.get('host', 'localhost'),
        port=_DB_CONFIG.get('port', '5432'),
        database=_DB_CONFIG['database'],
        user=_DB_CONFIG['user'],
        password=_DB_CONFIG['password'],
    )


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


# ------------------------- Статусы -------------------------
@admin_bp.route('/api/admin/orders/statuses')
def get_statuses():
    if not _require_admin():
        return _unauthorized()
    return jsonify({'statuses': STATUSES})


# ------------------------- Заказы продуктов -------------------------
@admin_bp.route('/api/admin/orders/products')
def list_product_orders():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/products/<int:order_id>')
def product_order_details(order_id):
    if not _require_admin():
        return _unauthorized()
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ------------------------- Заказы матрасов -------------------------
@admin_bp.route('/api/admin/orders/mattresses')
def list_mattress_orders():
    if not _require_admin():
        return _unauthorized()
    page = max(0, request.args.get('page', 0, type=int))
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>')
def mattress_order_details(order_id):
    if not _require_admin():
        return _unauthorized()
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ------------------------- Изменение статуса -------------------------
@admin_bp.route('/api/admin/orders/products/<int:order_id>/status', methods=['PUT'])
def update_product_status(order_id):
    if not _require_admin():
        return _unauthorized()
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip()
    if new_status not in STATUSES:
        return jsonify({'error': 'Недопустимый статус'}), 400
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>/status', methods=['PUT'])
def update_mattress_status(order_id):
    if not _require_admin():
        return _unauthorized()
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip()
    if new_status not in STATUSES:
        return jsonify({'error': 'Недопустимый статус'}), 400
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ------------------------- Удаление -------------------------
@admin_bp.route('/api/admin/orders/products/<int:order_id>', methods=['DELETE'])
def delete_product_order(order_id):
    if not _require_admin():
        return _unauthorized()
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@admin_bp.route('/api/admin/orders/mattresses/<int:order_id>', methods=['DELETE'])
def delete_mattress_order(order_id):
    if not _require_admin():
        return _unauthorized()
    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ------------------------- Другие заказы клиента -------------------------
@admin_bp.route('/api/admin/orders/customer/<phone>')
def customer_orders(phone):
    if not _require_admin():
        return _unauthorized()
    exclude_type = request.args.get('exclude_type', '')
    exclude_id = request.args.get('exclude_id', type=int)

    conn = _get_db_connection()
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
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()