#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

# Расширения файлов, которые считаем текстовыми
TEXT_EXTENSIONS = {
    '.txt', '.html', '.htm', '.css', '.js', '.py', '.json',
    '.xml', '.csv', '.md', '.rst', '.ini', '.cfg', '.conf',
    '.yaml', '.yml', '.toml', '.sh', '.bat', '.ps1', '.rb',
    '.pl', '.php', '.java', '.c', '.cpp', '.h', '.hpp', '.go',
    '.rs', '.swift', '.kt', '.scala', '.sql', '.r', '.jl'
}

# Имя выходного файла
OUTPUT_FILE = 'code.txt'

def collect_text_files(root_dir, output_file):
    """
    Рекурсивно обходит root_dir, находит текстовые файлы с заданными расширениями
    и записывает их содержимое в output_file с указанием относительного пути.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Используем root_dir как отправную точку для обхода
    # Если root_dir не указан, используем директорию скрипта
    if root_dir is None:
        root_dir = script_dir
    else:
        root_dir = os.path.abspath(root_dir)

    output_path = os.path.join(root_dir, output_file)

    # Счётчики для статистики
    processed = 0
    skipped = 0
    errors = 0

    with open(output_path, 'w', encoding='utf-8') as out_f:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Исключаем выходной файл, если он попадёт в обход
            if output_file in filenames:
                filenames.remove(output_file)

            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                # Проверяем расширение
                ext = os.path.splitext(filename)[1].lower()
                if ext not in TEXT_EXTENSIONS:
                    skipped += 1
                    continue

                # Относительный путь от корня обхода (root_dir)
                rel_path = os.path.relpath(file_path, start=root_dir)
                # Если root_dir совпадает с директорией скрипта, то rel_path – это
                # путь от корня проекта (места запуска) до файла

                try:
                    # Пытаемся прочитать файл с кодировкой UTF-8
                    with open(file_path, 'r', encoding='utf-8') as in_f:
                        content = in_f.read()
                except UnicodeDecodeError:
                    # Если не UTF-8, пробуем другие распространённые кодировки
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

                # Записываем в выходной файл
                out_f.write(f"===== Файл: {rel_path} =====\n")
                out_f.write(content)
                # Добавляем пустую строку между файлами для читаемости, если содержимое не заканчивается переводом строки
                if not content.endswith('\n'):
                    out_f.write('\n')
                out_f.write('\n')  # дополнительный разделитель

                processed += 1

    print(f"Готово. Обработано файлов: {processed}, пропущено (неподходящее расширение): {skipped}, ошибок: {errors}")
    print(f"Результат записан в: {output_path}")

if __name__ == '__main__':
    # Если передан аргумент командной строки – используем его как корневую директорию
    # Иначе обходим директорию, в которой находится скрипт
    root = sys.argv[1] if len(sys.argv) > 1 else None
    collect_text_files(root, OUTPUT_FILE)