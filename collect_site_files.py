#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

# Разрешенные расширения: только HTML и CSS
ALLOWED_EXTENSIONS = {'.html', '.htm', '.css'}

# Имя выходного файла
OUTPUT_FILE = 'code.txt'

def collect_html_css_and_api(root_dir, output_file):
    """
    Рекурсивно обходит root_dir, находит файлы с расширениями .html, .htm, .css,
    а также файл с именем api.py (ровно такое имя) и записывает их содержимое
    в output_file с указанием относительного пути.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if root_dir is None:
        root_dir = script_dir
    else:
        root_dir = os.path.abspath(root_dir)

    output_path = os.path.join(root_dir, output_file)

    processed = 0
    skipped = 0
    errors = 0

    with open(output_path, 'w', encoding='utf-8') as out_f:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Исключаем выходной файл, если он попадётся
            if output_file in filenames:
                filenames.remove(output_file)

            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                ext = os.path.splitext(filename)[1].lower()

                # Проверяем условие: расширение в списке разрешённых ИЛИ имя файла точно равно "api.py"
                is_allowed_ext = ext in ALLOWED_EXTENSIONS
                is_api_py = (filename == 'api.py')   # только файл с точным именем api.py

                if not (is_allowed_ext or is_api_py):
                    skipped += 1
                    continue

                rel_path = os.path.relpath(file_path, start=root_dir)

                try:
                    with open(file_path, 'r', encoding='utf-8') as in_f:
                        content = in_f.read()
                except UnicodeDecodeError:
                    try:
                        with open(file_path, 'r', encoding='cp1251') as in_f:
                            content = in_f.read()
                    except Exception as e:
                        errors += 1
                        print(f"Ошибка чтения {file_path}: {e}", file=sys.stderr)
                        content = f"[Ошибка: невозможно прочитать файл в текстовом режиме]"
                except Exception as e:
                    errors += 1
                    print(f"Ошибка при открытии {file_path}: {e}", file=sys.stderr)
                    continue

                out_f.write(f"===== Файл: {rel_path} =====\n")
                out_f.write(content)
                if not content.endswith('\n'):
                    out_f.write('\n')
                out_f.write('\n')

                processed += 1

    print(f"Готово. Обработано файлов: {processed}, пропущено: {skipped}, ошибок: {errors}")
    print(f"Результат записан в: {output_path}")

if __name__ == '__main__':
    root = sys.argv[1] if len(sys.argv) > 1 else None
    collect_html_css_and_api(root, OUTPUT_FILE)