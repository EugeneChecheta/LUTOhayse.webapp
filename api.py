import psycopg2
import os
from flask import Flask, jsonify, send_from_directory, abort, request
from flask_cors import CORS

app = Flask(__name__, static_folder=None)
CORS(app)

# ----- Чтение конфигурации БД из файла telegram_admin_panel/config_db.txt -----
def get_db_config():
    config_path = os.path.join(os.path.dirname(__file__), 'telegram_admin_panel', 'config_db.txt')
    config = {}
    with open(config_path, 'r') as f:
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
        # Исключаем служебный флаг "Скрытый" из списка для фильтрации
        cur.execute("SELECT id, name FROM flags WHERE lower(name) != 'скрытый' ORDER BY id;")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([{'id': r[0], 'name': r[1]} for r in rows])
    except Exception as e:
        app.logger.error(f"API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# ----- API: список товаров с фильтрацией по типу и флагам, исключая скрытые -----
@app.route('/api/products')
def products():
    try:
        # Получаем параметры фильтрации
        type_id = request.args.get('type_id', type=int)
        flag_ids = request.args.getlist('flag_ids', type=int)

        conn = get_db_connection()
        cur = conn.cursor()

        # Подзапрос для исключения товаров с флагом "Скрытый" (регистронезависимо)
        hidden_flag_condition = """
            NOT EXISTS (
                SELECT 1 FROM flags_for_products fp
                JOIN flags f ON fp.flags_id = f.id
                WHERE fp.products_id = p.id AND lower(f.name) = 'скрытый'
            )
        """

        if flag_ids:
            # Формируем запрос: товары, у которых есть ВСЕ указанные флаги, и нет скрытого флага
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
            # Без фильтра по флагам, но исключаем скрытые товары
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

# ----- Статические файлы -----
@app.route('/')
def index():
    return send_from_directory('webpages', 'catalog.html')

@app.route('/catalog.css')
def catalog_css():
    return send_from_directory('webpages', 'catalog.css')

@app.route('/media/<path:filename>')
def media_files(filename):
    return send_from_directory('media', filename)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('media/interface', 'favicon.png')

# ----- Запуск -----
if __name__ == '__main__':
    os.makedirs('webpages', exist_ok=True)
    os.makedirs('media/products', exist_ok=True)
    os.makedirs('media/interface', exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=True)