import psycopg2
import os
from flask import Flask, jsonify, send_from_directory, abort, request
from flask_cors import CORS
import glob
from pathlib import Path

app = Flask(__name__, static_folder=None)
CORS(app)

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

# ----- API: список типов товаров -----
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

# ----- API: список товаров с фильтрацией -----
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
                SELECT p.code, p.name
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
                GROUP BY p.id, p.code, p.name
                HAVING COUNT(DISTINCT fp.flags_id) = %s
                ORDER BY p.id
            """
            params.append(len(flag_ids))
            cur.execute(query, params)
        else:
            query = f"""
                SELECT code, name
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
        return jsonify([{'code': r[0], 'name': r[1]} for r in rows])
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# ----- API: детальная информация о товаре по коду -----
@app.route('/api/product/<code>')
def product_details(code):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Основные данные товара + тип
        cur.execute("""
            SELECT p.id, p.code, p.name, p.products_type_id, pt.name as type_name
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
            'products_type_id': product_row[3],
            'type_name': product_row[4]
        }

        # 2. Стоимости по типам материалов
        cur.execute("""
            SELECT mt.id, mt.name, mfp.cost
            FROM materials_for_products mfp
            JOIN materials_type mt ON mfp.materials_type_id = mt.id
            WHERE mfp.products_id = %s
            ORDER BY mt.id
        """, (prod_id,))
        costs = [{'id': r[0], 'name': r[1], 'cost': r[2]} for r in cur.fetchall()]

        # 3. Основные характеристики (глобальные + значения товара)
        cur.execute("""
            SELECT mft.id, mft.name, COALESCE(pmf.value, '') as value
            FROM main_features_types mft
            LEFT JOIN product_main_features pmf 
                ON mft.id = pmf.feature_id AND pmf.products_id = %s
            ORDER BY mft.id
        """, (prod_id,))
        main_features = [{'id': r[0], 'name': r[1], 'value': r[2]} for r in cur.fetchall()]

        # 4. Дополнительные характеристики
        cur.execute("""
            SELECT id, name, value
            FROM product_extra_features
            WHERE products_id = %s
            ORDER BY id
        """, (prod_id,))
        extra_features = [{'id': r[0], 'name': r[1], 'value': r[2]} for r in cur.fetchall()]

        cur.close()
        conn.close()

        # 5. Фотографии (сканируем папку media/products/<code>/)
        media_dir = Path(__file__).parent / 'media' / 'products' / code
        photos = {'preview': None, 'size': None, 'main': []}
        if media_dir.is_dir():
            # preview
            preview_path = media_dir / 'preview.webp'
            if preview_path.exists():
                photos['preview'] = f'/media/products/{code}/preview.webp'
            # size (схема)
            size_path = media_dir / 'size.webp'
            if size_path.exists():
                photos['size'] = f'/media/products/{code}/size.webp'
            # основные фото (числовые имена)
            main_files = sorted(media_dir.glob('[0-9]*.webp'), key=lambda p: int(p.stem))
            photos['main'] = [f'/media/products/{code}/{p.name}' for p in main_files]

        # Формируем ответ
        result = {
            'product': product_data,
            'costs': costs,
            'main_features': main_features,
            'extra_features': extra_features,
            'photos': photos
        }
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"API /api/product/<code> error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# ========= НОВЫЙ ЭНДПОИНТ: список материалов =========
@app.route('/api/materials')
def get_materials():
    """Возвращает все материалы с их типами и URL фото."""
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

# ----- Статические файлы -----
@app.route('/')
def index():
    return send_from_directory('webpages', 'catalog.html')

@app.route('/product/<code>')
def product_page(code):
    return send_from_directory('webpages', 'product_card.html')

@app.route('/catalog.css')
def catalog_css():
    return send_from_directory('webpages', 'catalog.css')

@app.route('/product_card.css')
def product_card_css():
    return send_from_directory('webpages', 'product_card.css')

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
    app.run(host='0.0.0.0', port=5000, debug=True)