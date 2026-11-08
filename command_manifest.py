"""Каталог команд. shortcuts — кандидаты; проверка меню не проверяет клавиши."""

# Строки соответствуют номерам исходного списка.
from skill_engine import COMMAND_OUTCOMES


CORE = [
    ("Новая вкладка", "Ctrl+T", "new_tab"),
    ("Закрыть текущую вкладку", "Ctrl+W", "close_tab"),
    ("Новое окно", "Ctrl+Shift+N", "new_window"),
    ("Следующая вкладка", "Ctrl+Tab", "key_tab_next"),
    ("Предыдущая вкладка", "Ctrl+Shift+Tab", "key_tab_previous"),
    ("Восстановить закрытую вкладку", "Ctrl+Shift+T", "key_reopen_tab"),
    ("Новый документ, определить поведение", "Ctrl+N", "key_ctrl_n"),
    ("Открыть файл", "Ctrl+O", "open_file"),
    ("Сохранить", "Ctrl+S", "save_file"),
    ("Сохранить как", "F12 / Ctrl+Shift+S", "save_copy"),
    ("Печать", "Файл → Печать / Ctrl+P (резерв)", "print_dialog"),
    ("Закрыть окно", "Alt+F4", "close_window"),
    ("Отменить правку", "Ctrl+Z", "undo"),
    ("Повторить правку", "Ctrl+Y", "key_redo"),
    ("Вырезать", "Ctrl+X", "key_cut"),
    ("Копировать", "Ctrl+C", "key_copy"),
    ("Вставить", "Ctrl+V", "key_paste"),
    ("Выделить весь текст", "Ctrl+A", "select_all"),
    ("Удалить символ справа", "Delete", "key_delete"),
    ("Удалить слово слева", "Ctrl+Backspace", "key_delete_left_word"),
    ("Удалить слово справа", "Ctrl+Delete", "key_delete_right_word"),
    ("Вставить дату и время", "F5", "date"),
    ("Начало строки", "Home", "key_line_start"),
    ("Конец строки", "End", "key_line_end"),
    ("Начало документа", "Ctrl+Home", "key_doc_start"),
    ("Конец документа", "Ctrl+End", "key_doc_end"),
    ("Выделить символ или строку", "Shift+стрелки", "key_select_chars_lines"),
    ("Выделить слово", "Ctrl+Shift+стрелки", "key_select_word"),
    ("Выделить до края строки", "Shift+Home / End", "key_select_line_edges"),
    ("Выделить до края документа", "Ctrl+Shift+Home / End", "key_select_doc_edges"),
    ("Поиск", "Ctrl+F", "find_text"),
    ("Найти далее", "F3", "find_next"),
    ("Найти ранее", "Shift+F3", "find_previous"),
    ("Найти и заменить", "Ctrl+H", "replace_text"),
    ("Перейти к строке", "Ctrl+G / меню «Правка» (резерв)", "key_goto_line"),
    ("Увеличить масштаб", "Ctrl++", "zoom_in"),
    ("Уменьшить масштаб", "Ctrl+-", "zoom_out"),
    ("Сбросить масштаб", "Ctrl+0", "zoom_reset"),
    ("Перенос по словам", "Просмотр / Alt+Z (кандидат)", "word_wrap"),
    ("Шрифт: семейство и размер", "Параметры", "font_surface"),
]

EXTRA = [
    ("Переместить вкладку", "Alt+Shift+стрелки (кандидат)", None),
    ("Вынести вкладку в отдельное окно", "Перетаскивание", None),
    ("Восстановить сессию с несохранённым текстом", "Параметры сессии", None),
    ("Вставить как обычный текст", "Ctrl+Shift+V (кандидат)", "key_paste_plain"),
    ("Перефразировать текст", "Ctrl+D / Rewrite (если доступно)", None),
    ("Настроить длину и тон Rewrite", "Rewrite (если доступно)", None),
    ("Генерация текста и суммаризация", "Write / Summarize (если доступны)", None),
    ("Строка состояния", "Просмотр", "status_bar"),
    ("Символы, строки и кодировка", "Строка состояния", None),
    ("Масштаб колесом", "Ctrl+колесо", None),
    ("Светлая, тёмная и системная тема", "Параметры", None),
    ("Направление справа налево", "Right Ctrl+Right Shift (кандидат)", None),
    ("Направление слева направо", "Left Ctrl+Left Shift (кандидат)", None),
    ("Сохранить в ANSI", "Сохранить как → Кодировка", None),
    ("Сохранить в UTF-8", "Сохранить как → Кодировка", None),
    ("Сохранить в UTF-8 с BOM", "Сохранить как → Кодировка", None),
    ("Сохранить в UTF-16 LE", "Сохранить как → Кодировка", None),
    ("Сохранить в UTF-16 BE", "Сохранить как → Кодировка", None),
    ("Создать вкладку Markdown", "Файл", "new_markdown"),
    ("Переключить вид Markdown", "Просмотр / строка состояния", "markdown_view"),
    ("Заголовки и основной текст", "Панель Markdown", None),
    ("Списки, включая вложенные", "Панель Markdown", None),
    ("Жирный текст", "Панель Markdown", None),
    ("Курсив", "Панель Markdown", None),
    ("Зачёркивание", "Панель Markdown", None),
    ("Вставить ссылку", "Панель Markdown", None),
    ("Вставить таблицу и менять строки / столбцы", "Панель Markdown", None),
    ("Очистить форматирование", "Панель Markdown", None),
    ("Сохранить всё", "Файл", "save_all"),
    ("Настроить параметры страницы", "Файл", "page_surface"),
    ("Удалить выделение", "Изменить", "delete"),
    ("Что нового", "Файл", None),
    ("Поиск через Bing", "Изменить", None),
    ("Выйти из Блокнота", "Файл", "exit_app"),
]

MORE = [
    ("Показать варианты исправления слова", "Контекстное меню / Shift+F10", None),
    ("Пропустить слово в документе", "Контекстное меню проверки правописания", None),
    ("Добавить слово в словарь", "Контекстное меню проверки правописания", None),
    ("Включить проверку правописания", "Параметры", None),
    ("Выбрать типы файлов для проверки правописания", "Параметры", None),
    ("Включить или выключить автозамену опечаток", "Параметры", None),
    ("Включить или выключить поддержку форматирования", "Параметры", None),
    ("Увеличить отступ элемента списка", "Панель Markdown", None),
    ("Искать с учётом регистра", "Параметры панели поиска, если доступны", "find_case"),
    ("Продолжать поиск с начала или конца документа", "Параметры панели поиска, если доступны", "find_wrap"),
]


def manifest_results(entries):
    results = []
    for number, (name, shortcut, operation) in enumerate(CORE + EXTRA + MORE, 1):
        match = next((e for e in entries if e["operation"] == operation), None) if operation else None
        recovery = (match or {}).get("recovery") or {}
        used_profile = recovery.get("profile") if recovery.get("status") == "verified" else None
        via_shortcut = bool(operation and operation.startswith("key_"))
        if used_profile == "open_shortcut":
            via_shortcut = True
        results.append({
            "number": number, "name": name, "shortcut_or_route": shortcut,
            "status": match["status"] if match else "not_run",
            "outcome": COMMAND_OUTCOMES.get(match["status"]) if match else None,
            "checked_via": match.get("checked_via", "shortcut" if via_shortcut else "interface")
                           if match and match["status"] not in {"pending", "not_run", "skipped", "blocked"} else None,
            "evidence": match.get("evidence") if match else None,
            "recovery": match.get("recovery") if match else None,
            "diagnosis": match.get("diagnosis") if match else None,
            "initial_failure": match.get("initial_failure") if match else None,
        })
    return results
