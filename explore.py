"""Visible menu inventory and controlled probes for Windows Notepad."""
import ctypes
from contextlib import contextmanager
from functools import partial
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

from comtypes import COMError
from pywintypes import error as Win32Error
from pywinauto import Desktop
from pywinauto.keyboard import send_keys
from pywinauto.uia_defines import NoPatternInterfaceError

from command_manifest import CORE, EXTRA, MORE, manifest_results
import trainer
import skill_engine
from skill_engine import CommandBlocked
from report_files import numbered_report_parts, reserve_report

REPORT = trainer.LOGS / "Notepad_Report.txt"
SKILL = trainer.SKILLS_DIR / "Notepad_Skill.json"
RETRY_REPORT = trainer.LOGS / "Notepad_Retry_Report.txt"
MANIFEST_OPERATIONS = {number: operation
                       for number, (_, _, operation) in enumerate(CORE + EXTRA + MORE, 1)}
USER32 = ctypes.windll.user32
RECOVERY_DB = trainer.MEMORY / "Notepad_Probe_Recovery.sqlite3"
STRATEGY_REVISION = "0.4.21-r1"
WINDOW_TESTS = {"save_all", "close_window", "exit_app"}
SEARCH_TESTS = {"find_text", "replace_text"}
KEYBOARD_TESTS = {"key_redo", "key_select_word", "key_ctrl_n"}
CLIPBOARD_PROBES = {
    "key_cut": ("^x", {"вырезать", "cut"}),
    "key_copy": ("^c", {"копировать", "copy"}),
    "key_paste": ("^v", {"вставить", "paste"}),
    "key_paste_plain": ("^+v", {"вставить как обычный текст", "вставить обычный текст", "paste as plain text"}),
}
SEARCH_OPTION_PROBES = {
    "find_case": {"учитывать регистр", "с учетом регистра", "учет регистра", "совпадение регистра",
                  "match case", "matchcase"},
    "find_wrap": {"по кругу", "искать по кругу", "циклический поиск", "вокруг", "wrap around", "wraparound"},
}
UNTITLED_TABS = {"без имени", "без названия", "untitled"}


def notepad_running():
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/FO", "CSV", "/NH"],
        check=True, capture_output=True, timeout=10,
    )
    return any(line.strip().lower().startswith(b'"notepad.exe",') for line in result.stdout.splitlines())


def require_closed_notepad():
    if not notepad_running():
        log("ИЗОЛЯЦИЯ | Перед запуском процессов notepad.exe нет.")
        return
    log("ИЗОЛЯЦИЯ | Блокнот ещё запущен. Принудительное завершение может потерять несохранённые правки.")
    try:
        confirmed = input("Если всё сохранено, введи ЗАВЕРШИТЬ для остановки всех notepad.exe: ").strip()
    except EOFError:
        confirmed = ""
    if confirmed != "ЗАВЕРШИТЬ":
        raise IsolationError("Остановлено. Сохрани документы, закрой Блокнот и запусти пункт 6 снова.")
    result = subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"],
                            capture_output=True, timeout=10)
    if not Lab.wait(lambda: not notepad_running(), 5, interval=0.3):
        raise IsolationError(f"После taskkill всё ещё есть процессы notepad.exe (код {result.returncode}).")
    log("ИЗОЛЯЦИЯ | После подтверждённого taskkill процессов notepad.exe нет.")

RETRY_PROFILES = {
    "open_file": ("open_shortcut", "Open the disposable file with Ctrl+O."),
    "key_doc_end": ("select_last_character", "Verify Ctrl+End by selecting the final line break."),
    "key_select_word": ("accept_word_separator", "Verify Ctrl+Shift+Right, permitting one trailing separator."),
    "key_redo": ("undo_via_menu", "If Ctrl+Z did not restore the sample during preparation, "
                 "prepare with Edit > Undo, then test the actual Ctrl+Y shortcut."),
    "key_goto_line": ("goto_via_menu", "Open the named Go To menu command in the disposable "
                      "document and verify the caret on line 2."),
    **{operation: ("clipboard_via_menu", "Use the explicitly named Edit menu command on the probe, "
                   "then verify clipboard text and/or the resulting document.")
       for operation in CLIPBOARD_PROBES},
    **{operation: ("find_via_menu", "Open Find with the named Edit menu command, then verify "
                   "the search option against the disposable document.")
       for operation in SEARCH_OPTION_PROBES},
}

# Explicit checks define which operations this sandbox can verify.
PROBES = {
    "сохранить как": "save_copy", "save as": "save_copy",
    "найти": "find_text", "find": "find_text",
    "заменить": "replace_text", "replace": "replace_text",
    "выбрать все": "select_all", "выделить все": "select_all", "select all": "select_all",
}

PROBES.update({
    "открыть": "open_file", "open": "open_file",
    "сохранить": "save_file", "save": "save_file",
    "сохранить все": "save_all", "save all": "save_all",
    "новая вкладка": "new_tab", "new tab": "new_tab",
    "новая вкладка markdown": "new_markdown", "new markdown tab": "new_markdown",
    "новое окно": "new_window", "new window": "new_window",
    "закрыть вкладку": "close_tab", "close tab": "close_tab",
    "закрыть окно": "close_window", "close window": "close_window",
    "выйти": "exit_app", "exit": "exit_app",
    "отменить": "undo", "undo": "undo",
    "удалить": "delete", "delete": "delete",
    "время и дата": "date", "time/date": "date",
    "перейти": "goto_surface", "go to": "goto_surface",
    "шрифт": "font_surface", "font": "font_surface",
    "масштаб": "surface", "zoom": "surface",
    "параметры страницы": "page_surface", "page setup": "page_surface",
    "строка состояния": "status_bar", "status bar": "status_bar",
    "перенос по словам": "word_wrap", "word wrap": "word_wrap",
    "markdown": "markdown_view",
    "найти далее": "find_next", "find next": "find_next",
    "найти ранее": "find_previous", "find previous": "find_previous",
})
SKIP = {
    "печать": "Может отправить задание на принтер",
    "print": "Может отправить задание на принтер",
    "определить с помощью bing": "Может передать выделенный текст в браузер",
    "что нового": "Может открыть внешний ресурс",
    "вырезать": "Меняет системный буфер обмена",
    "копировать": "Меняет системный буфер обмена",
    "вставить": "Может прочитать пользовательский буфер обмена",
    "очистить форматирование": "Для проверки нужен форматированный Markdown-документ",
}

ZOOM = {
    "увеличить": "zoom_in", "zoom in": "zoom_in",
    "уменьшить": "zoom_out", "zoom out": "zoom_out",
    "восстановить масштаб по умолчанию": "zoom_reset",
    "restore default zoom": "zoom_reset",
}


SURFACES = {"surface", "goto_surface", "font_surface", "page_surface"}
SELECTION_PROBES = {
    "key_line_start": [("^{HOME}{DOWN}{END}{HOME}+{RIGHT}", "a")],
    "key_line_end": [("^{HOME}{DOWN}{HOME}{END}+{LEFT}", "a")],
    "key_doc_start": [("^{END}^{HOME}+{RIGHT}", "D")],
    "key_doc_end": [("^{HOME}^{END}{UP}+{HOME}", "alpha beta alpha")],
    "key_select_chars_lines": [("^{HOME}+{RIGHT}", "D"), ("^{HOME}+{DOWN}", "Druzhishe probe")],
    "key_select_word": [("^{HOME}^+{RIGHT}", "Druzhishe")],
    "key_select_line_edges": [("^{HOME}{RIGHT}+{HOME}", "D"), ("^{HOME}{RIGHT}+{END}", "ruzhishe probe")],
    "key_select_doc_edges": [("^{HOME}{RIGHT}^+{HOME}", "D"),
                             ("^{HOME}{RIGHT}^+{END}", "ruzhishe probe\nalpha beta alpha")],
}
EDIT_PROBES = {
    "key_delete": ("^{HOME}{DELETE}", "ruzhishe probe\nalpha beta alpha"),
    "key_delete_left_word": ("^{HOME}{END}^{BACKSPACE}", "Druzhishe \nalpha beta alpha"),
    "key_delete_right_word": ("^{HOME}^{DELETE}", "probe\nalpha beta alpha"),
}

def norm(name):
    return name.split("\t")[0].strip().casefold().replace("…", "").rstrip(".").replace("ё", "е")


def control_id(control):
    return tuple(control.element_info.element.GetRuntimeId())


def search_field_role(control):
    automation_id = re.sub(r"[^a-z]", "", (control.element_info.automation_id or "").lower())
    name = norm(control.window_text().rstrip(":"))
    if name in {"заменить", "заменить на", "поле замены", "replace", "replace with"}:
        return "replace"
    if name in {"найти", "поиск", "найти текст", "что найти", "find", "find what", "search"}:
        return "find"
    automation_id = automation_id.replace("findandreplace", "").replace("findreplace", "")
    if "replace" in automation_id:
        return "replace"
    if any(word in automation_id for word in ("find", "search")):
        return "find"
    return None


def choose_search_fields(edits, editor_id, initial_edits, replace):
    candidates = {control_id(control): control for control in edits}
    candidates.pop(editor_id, None)
    roles = {key: search_field_role(control) for key, control in candidates.items()}
    expected = ("find", "replace") if replace else ("find",)
    selected = {}
    for role in expected:
        matching = [key for key, value in roles.items() if value == role]
        if len(matching) > 1:
            return None
        if matching:
            selected[role] = matching[0]
    missing = [role for role in expected if role not in selected]
    unnamed = [key for key in candidates if key not in initial_edits and roles[key] is None]
    if missing == ["find"] and "replace" in selected and not unnamed:
        def nearby_parents(control):
            parents = set()
            for _ in range(2):
                control = control.parent()
                if control is None or control.element_info.control_type not in {"Group", "Pane", "Custom", "ToolBar"}:
                    break
                parents.add(control_id(control))
            return parents

        replace_parents = nearby_parents(candidates[selected["replace"]])
        unnamed = [key for key, control in candidates.items() if roles[key] is None
                   and (control.element_info.automation_id or "").casefold() == "textbox"
                   and replace_parents & nearby_parents(control)]
    if missing:
        if len(unnamed) != len(missing):
            return None
        def position(key):
            bounds = candidates[key].rectangle()
            return bounds.top, bounds.left
        if len({position(key) for key in unnamed}) != len(unnamed):
            return None
        selected.update(zip(missing, sorted(unnamed, key=position)))
    return [candidates[selected[role]] for role in expected]


def normalize_copy_text(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\r\n".join(line.rstrip(" \t") for line in lines)


def read_probe_copy(path):
    data = path.read_bytes()
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    return data.decode(encoding)


def menu_toggle_state(item):
    try:
        return "toggle", item.iface_toggle.CurrentToggleState
    except (AttributeError, COMError, NoPatternInterfaceError):
        pass
    try:
        return "legacy", bool(item.iface_legacy_iaccessible.CurrentState & 0x10)
    except (AttributeError, COMError, NoPatternInterfaceError):
        pass
    try:
        return "selection", bool(item.iface_selection_item.CurrentIsSelected)
    except (AttributeError, COMError, NoPatternInterfaceError):
        raise RuntimeError("Windows не предоставила проверяемое состояние пункта меню.") from None


class DialogNotFound(RuntimeError):
    """The file dialog was not recognized before confirmation."""


class IsolationError(RuntimeError):
    """A probe would affect documents outside the current test context."""


@contextmanager
def probe_clipboard(notepad_pid, *, verify_nonempty=False):
    from ctypes import wintypes

    import pywintypes
    import win32api
    import win32clipboard as clipboard
    import win32gui
    import win32process

    # An ownerless clipboard can still contain data. Use the native return value
    # because the pywin32 wrapper raises on a null handle even without an error.
    get_owner = ctypes.WinDLL("user32", use_last_error=True).GetClipboardOwner
    get_owner.restype = wintypes.HWND

    def clipboard_owner():
        ctypes.set_last_error(0)
        owner = get_owner()
        if not owner and (error := ctypes.get_last_error()):
            raise IsolationError(f"Windows не предоставила владельца буфера (код {error}).")
        return owner

    # A window owned by this process is needed when emptying and writing the clipboard.
    hwnd = win32gui.CreateWindow("Static", "", 0, 0, 0, 0, 0, 0, 0, win32api.GetModuleHandle(None), None)
    sequence = USER32.GetClipboardSequenceNumber

    @contextmanager
    def opened(block_error=IsolationError):
        for attempt in range(10):
            try:
                clipboard.OpenClipboard(hwnd)
                break
            except pywintypes.error as error:
                if attempt == 9:
                    raise block_error("Буфер обмена занят другим приложением.") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            clipboard.CloseClipboard()

    def read():
        return clipboard.GetClipboardData(13) if clipboard.IsClipboardFormatAvailable(13) else None

    def stamp():
        return sequence(), clipboard_owner()

    def write(value):
        clipboard.EmptyClipboard()
        if value is not None:
            clipboard.SetClipboardText(value, 13)  # CF_UNICODETEXT

    byte_formats = {1, 7}  # CF_TEXT, CF_OEMTEXT

    def read_format(fmt):
        value = clipboard.GetClipboardData(fmt)
        if fmt in byte_formats:
            return value.rstrip(b"\0")
        return value

    def restore(snapshot):
        write(None)
        for fmt, value in snapshot.items():
            if fmt == 13:
                clipboard.SetClipboardText(value, fmt)
            else:
                data = value + b"\0" if fmt in byte_formats else value
                clipboard.SetClipboardData(fmt, data)
        matches = (all(read_format(fmt) == value for fmt, value in snapshot.items())
                   if snapshot else clipboard.EnumClipboardFormats(0) == 0)
        if not matches:
            raise IsolationError("Содержимое буфера отличается от резервной копии.")

    try:
        with opened(CommandBlocked):
            formats = []
            item = clipboard.EnumClipboardFormats(0)
            while item:
                formats.append(item)
                item = clipboard.EnumClipboardFormats(item)
            if set(formats) - (byte_formats | {13, 16}) or (formats and 13 not in formats):
                format_ids = ", ".join(map(str, sorted(formats)))
                raise CommandBlocked(f"Обнаружены невосстановимые данные в буфере (форматы Win32: {format_ids}).")
            backup = {fmt: read_format(fmt) for fmt in formats}
            if formats and not isinstance(backup.get(13), str):
                raise CommandBlocked("Windows не предоставила исходный текст для восстановления.")
            current = stamp()
            if not current[0]:
                raise CommandBlocked("Windows не предоставила номер версии буфера обмена.")

        fixture = (f"Исходный текст пользователя — {uuid4().hex[:8]}"
                   if verify_nonempty and not backup else None)
        seeded_text = None

        def has_seed():
            return (seeded_text is not None and clipboard_owner() == hwnd
                    and read() == seeded_text)

        def seed(value):
            nonlocal current, seeded_text
            with opened(CommandBlocked):
                if stamp() != current:
                    raise CommandBlocked("Буфер обмена сменило другое приложение.")
                seeded_text = value
                try:
                    if fixture is not None:
                        write(fixture)
                        if read() != fixture:
                            raise IsolationError("Проверочный непустой буфер не подтверждён.")
                    write(value)
                finally:
                    current = stamp()

        def observe_copy(value):
            nonlocal current
            with opened():
                owner = clipboard_owner()
                if not owner or owner == hwnd:
                    return False
                if win32process.GetWindowThreadProcessId(owner)[1] != notepad_pid:
                    raise IsolationError("Владельцем буфера стало другое приложение.")
                if read() == value:
                    current = stamp()
                    return True
                return False

        def check_current():
            nonlocal current
            # Windows can finalize clipboard formats after the write. Recheck the owner
            # and the unique probe text under the clipboard lock before sending input.
            with opened(CommandBlocked):
                if not has_seed():
                    raise CommandBlocked("Перед командой не подтверждены владелец и пробный текст буфера.")
                confirmed = stamp()
                if confirmed != current:
                    log("БУФЕР | Версия изменилась; владелец и пробный текст совпадают.")
                current = confirmed

        completed = False
        try:
            yield seed, observe_copy, check_current
            completed = True
        finally:
            if seeded_text is not None:
                try:
                    with opened():
                        can_restore = stamp() == current or has_seed()
                        if can_restore:
                            try:
                                if fixture is not None:
                                    restore({13: fixture})
                            finally:
                                restore(backup)
                    if can_restore:
                        if fixture is not None:
                            log("БУФЕР | Восстановление непустого буфера проверено на тестовой строке.")
                        kind = "текстовый" if backup else "пустой"
                        log(f"БУФЕР | Исходный {kind} буфер восстановлен и сверен.")
                    else:
                        log("БУФЕР | Появилось новое содержимое; оно оставлено в буфере.")
                        if completed:
                            raise IsolationError("Буфер изменился до сверки восстановления.")
                except (pywintypes.error, IsolationError) as error:
                    raise IsolationError(f"Восстановление буфера не подтверждено: {error}") from error
    finally:
        win32gui.DestroyWindow(hwnd)


def retry_options(entry, error):
    if isinstance(error, (IsolationError, CommandBlocked)):
        return {}
    operation = entry["operation"]
    if operation in {"print_dialog", "key_ctrl_n", "key_reopen_tab"}:
        return {}
    if operation == "open_file":
        # Never try an alternate submission after the dialog was recognized.
        if not isinstance(error, DialogNotFound):
            return {}
        key, description = RETRY_PROFILES[operation]
        return {key: description}
    options = {"retry_on_probe": "Reopen a fresh disposable document, then retry the original action."}
    if operation in RETRY_PROFILES:
        key, description = RETRY_PROFILES[operation]
        options[key] = description
    return options


def retry_probe(lab, entry, options):
    metrics = {"version": 2, "operation": entry["operation"], "failure_type": entry["failure_type"]}
    fingerprint = {**metrics, "reason": entry["initial_failure"]}

    def report(stage, status, **details):
        if stage == "diagnosis":
            entry["diagnosis"] = {"status": status, **details}
            profiles = details.get("profiles") or [details.get("profile", "stop")]
            text = details.get("detail") or f"{' → '.join(profiles)} за {details.get('seconds')} с"
            log(f"QWEN | {entry['name']} | {status} | {text}")
        elif stage == "memory":
            text = details.get("detail") or details.get("profile", "")
            log(f"ПАМЯТЬ | {entry['name']} | {status} | {text}")
        elif stage == "recovery":
            if status == "started":
                log(f"ПОВТОР | {entry['name']} | {details['source']} → {details['profile']}")
            elif status == "blocked":
                log(f"ПОВТОР ЗАБЛОКИРОВАН | {entry['name']} | {details['detail']}")
            elif status == "unverified":
                log(f"ПОВТОР НЕ ПРОШЁЛ | {entry['name']} | {details['profile']} | {details['detail']}")

    def retry(strategy):
        lab.recover()
        if strategy == "retry_on_probe":
            lab.fresh_probe()
        evidence = lab.check(entry, strategy)
        return verified_status(entry), evidence

    context = {**metrics, "detail": entry["initial_failure"]}
    if entry["operation"] in {"key_doc_end", "key_select_word"}:
        context["observation"] = "The shortcut ran but the strict UIA selection check did not match."
    result = skill_engine.recover_skill(
        memory_path=RECOVERY_DB, fingerprint=fingerprint, context=context,
        profiles=lab.strategy_journal.allowed(entry["operation"], options),
        retry=retry, model=trainer.QWEN, report=report,
        rank=partial(skill_engine.ask_profile, keep_alive="2m", ranked=True),
        max_attempts=3,
        profiles_after_error=lambda error: retry_options(entry, error),
    )
    if result.recovery:
        entry["recovery"] = result.recovery
    if result.attempts:
        entry["recovery_attempts"] = result.attempts
    if result.failure:
        entry["evidence"] = result.failure
    return None if result.status == "blocked" else result.evidence


def verified_status(entry):
    return "panel_verified" if entry["operation"] in SURFACES else "verified"


def strategy_build_id(pid):
    """Read the installed Windows build and the probe window's executable version."""
    from ctypes import wintypes
    import win32api
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        names = ("CurrentMajorVersionNumber", "CurrentMinorVersionNumber", "CurrentBuildNumber", "UBR")
        windows = ".".join(str(int(winreg.QueryValueEx(key, name)[0])) for name in names)

    query_image = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
    query_image.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                            ctypes.POINTER(wintypes.DWORD))
    query_image.restype = wintypes.BOOL
    path = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(path))
    handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    try:
        if not query_image(int(handle), 0, path, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        handle.Close()

    version = win32api.GetFileVersionInfo(path.value, "\\")
    high, low = version["FileVersionMS"], version["FileVersionLS"]
    app = ".".join(map(str, (high >> 16, high & 0xFFFF, low >> 16, low & 0xFFFF)))
    return f"win-{windows}/notepad-{app}/agent-{STRATEGY_REVISION}"


def valid_pdf(path):
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")


def log(message):
    print(message, flush=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as report:
        report.write(message + "\n")


def write_skill(items):
    SKILL.parent.mkdir(parents=True, exist_ok=True)
    temporary = SKILL.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "name": "Блокнот", "schema_version": 1, "app": "notepad.exe", "test_version": "0.4.21",
        "agent": {"id": "notepad", "name": "Агент Блокнот", "group": "Агенты навыков"},
        "functions": items,
        "command_manifest": manifest_results(items),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SKILL)


def completed_report_queue(path):
    """Return failed command numbers, or None if this is not a complete report."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None
    if not any(line.startswith("ИТОГ |") for line in lines) or not any(
            line.startswith("Отчёт: ") for line in lines):
        return None
    rows = [match.groups() for line in lines if (match := re.fullmatch(
        r"МАНИФЕСТ \| (\d+) \| (.+) \| (verified|unverified|blocked|panel_verified|not_run|skipped|discovered)", line))]
    expected = [(number, name) for number, (name, _, _) in enumerate(CORE + EXTRA + MORE, 1)]
    if [(int(number), name) for number, name, _ in rows] != expected:
        return None
    carry = [line.removeprefix("ОЧЕРЕДЬ ПОВТОРА | ") for line in lines
             if line.startswith("ОЧЕРЕДЬ ПОВТОРА | ")]
    status_by_number = {int(number): status for number, _, status in rows}
    if carry:
        if len(carry) != 1 or not re.fullmatch(r"(?:\d+(?:,\d+)*)?", carry[0]):
            return None
        remaining = {int(number) for number in carry[0].split(",") if number}
        if not remaining <= MANIFEST_OPERATIONS.keys() or any(
                status_by_number[number] in {"verified", "panel_verified"} for number in remaining):
            return None
    else:
        remaining = {number for number, status in status_by_number.items() if status == "unverified"}
    return {number for number in remaining if MANIFEST_OPERATIONS[number] is not None}


def latest_retry_source(folder):
    report_name = re.compile(r"Notepad(?:_(?:Window|PDF|Search|Keyboard|Clipboard|Retry))?_Report"
                             r"(?:\s*\(\d+\)|\.last)?\.txt")
    candidates = []
    for path in folder.glob("*Notepad*Report*.txt"):
        numbered = numbered_report_parts(path.name)
        if numbered and report_name.fullmatch(numbered[1]):
            candidates.append((1, numbered[0], path))
        elif report_name.fullmatch(path.name):
            try:
                candidates.append((0, path.stat().st_mtime_ns, path))
            except FileNotFoundError:
                continue
    for _, _, path in sorted(candidates, key=lambda item: item[:2], reverse=True):
        queue = completed_report_queue(path)
        if queue is not None:
            return path, queue
    return None, set()


def arrange_test_console():
    """Place the launch console on the left and return a placer for test windows."""
    from ctypes import wintypes

    try:
        import win32api
        import win32con
        import win32gui

        get_console = ctypes.windll.kernel32.GetConsoleWindow
        get_console.restype = wintypes.HWND
        console = get_console()
        if not console or not win32gui.IsWindowVisible(console):
            marker = os.environ.get("DRUZHISHE_TEST_CONSOLE_TITLE", "")
            terminals = []

            def find_terminal(hwnd, _):
                if (win32gui.IsWindowVisible(hwnd)
                        and win32gui.GetClassName(hwnd) == "CASCADIA_HOSTING_WINDOW_CLASS"
                        and marker in win32gui.GetWindowText(hwnd)):
                    terminals.append(hwnd)
                return True

            if marker:
                for attempt in range(5):
                    win32gui.EnumWindows(find_terminal, None)
                    if terminals or attempt == 4:
                        break
                    time.sleep(0.2)
            if len(terminals) != 1:
                log("ОКНА | Не удалось однозначно найти окно журнала. Проверки продолжатся.")
                return None
            console = terminals[0]

        monitor = win32api.MonitorFromWindow(console, win32con.MONITOR_DEFAULTTONEAREST)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        middle = left + (right - left) // 2

        def place(hwnd, x, width):
            if win32gui.IsIconic(hwnd) or win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, 0, x, top, width, bottom - top,
                                  win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)

        place(console, left, middle - left)
        log("ОКНА | Журнал слева. Окна тестового Блокнота будут открываться справа.")
    except Exception as error:
        log(f"ОКНА | Не удалось расположить журнал ({error}). Проверки продолжатся.")
        return None

    def place_test_window(hwnd):
        try:
            place(hwnd, middle, right - middle)
        except Exception as error:
            log(f"ОКНА | Не удалось расположить тестовый Блокнот ({error}). Проверки продолжатся.")

    return place_test_window


class Lab:
    def __init__(self, sample="Druzhishe probe\nalpha beta alpha\n", *, clean_windows=False,
                 place_test_window=None, verify_nonempty_clipboard=False):
        self.desktop = Desktop(backend="uia")
        self.place_test_window = place_test_window
        self.verify_nonempty_clipboard = verify_nonempty_clipboard
        self.strategy_journal = None
        trainer.DATA.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="druzhishe-notepad-scan-", dir=trainer.DATA))
        self.source = folder / ("probe-" + uuid4().hex[:8] + ".txt")
        self.sample = sample
        self.probe_stems = {self.source.stem}
        self.probe_tab_ids = set()
        self.probe_window_classes = {"Notepad"}
        self.clean_windows = clean_windows
        self.clean_saved_text = None
        self.active_file = self.source
        try:
            if clean_windows:
                self.start_clean_probe()
                self.source = self.active_file
            else:
                self.source.write_text(self.sample, encoding="utf-8")
                subprocess.Popen(["notepad.exe", str(self.source)])
                self.attach(self.source)
        except BaseException:
            self.cleanup()
            raise

    def show_test_window(self, window):
        if self.place_test_window is not None:
            self.place_test_window(window.handle)

    def notepad_windows(self):
        return [window for window in self.desktop.windows()
                if window.is_visible() and window.class_name() in self.probe_window_classes]

    def require_single_window_tab(self, stem=None):
        handle = self.window.handle

        def snapshot():
            windows = self.notepad_windows()
            tabs = self.tabs()
            log(f"ИЗОЛЯЦИЯ | окон: {len(windows)} | TabItem: {len(tabs)}")
            if len(windows) != 1 or windows[0].handle != handle or len(tabs) != 1:
                raise IsolationError("Нужно ровно одно окно Блокнота с одной вкладкой. "
                                     "Если вернулись старые вкладки, выбери «Начать новый сеанс» в настройках Блокнота.")
            if USER32.GetForegroundWindow() != handle or not tabs[0].iface_selection_item.CurrentIsSelected:
                raise IsolationError("Единственная вкладка не подтверждена как активная.")
            if stem:
                if self.probe_stem(tabs[0].window_text()) != stem or self.probe_stem(self.window.window_text()) != stem:
                    raise IsolationError("Единственная вкладка не принадлежит текущему прогону.")
            elif norm(tabs[0].window_text()) not in UNTITLED_TABS:
                title = re.split(r"\s+[—–-]\s+", self.window.window_text(), maxsplit=1)[0]
                if norm(title) not in UNTITLED_TABS:
                    raise IsolationError("Вкладка нового окна уже имеет имя. Нужен пустой сеанс.")
            return control_id(tabs[0])

        first = snapshot()
        time.sleep(0.4)
        if snapshot() != first:
            raise IsolationError("Вкладка сменилась во время проверки изоляции.")
        return first

    def start_clean_probe(self, *, resume_saved=False):
        if self.notepad_windows():
            raise IsolationError("Перед запуском пробного окна обнаружено другое окно Блокнота.")
        subprocess.Popen(["notepad.exe"])
        windows = self.wait(self.notepad_windows, 12)
        if not windows or len(windows) != 1:
            raise IsolationError("Не появилось ровно одно новое окно Блокнота.")
        self.window = windows[0]
        self.show_test_window(self.window)
        self.window.set_focus()
        if not self.wait(self.tabs, 3):
            raise IsolationError("UIA не показал вкладку нового окна.")
        if (resume_saved and self.clean_saved_text is not None
                and self.probe_stem(self.window.window_text()) == self.active_file.stem):
            self.require_single_window_tab(self.active_file.stem)
            try:
                disk_text = read_probe_copy(self.active_file).replace("\r\n", "\n").replace("\r", "\n")
            except (OSError, UnicodeError) as error:
                raise IsolationError("Не удалось сверить сохранённый пробный файл.") from error
            if disk_text != self.clean_saved_text:
                raise IsolationError("Текст восстановленного файла на диске отличается от ожидаемого.")
            if self.wait(lambda: self.editor(self.clean_saved_text), 3) is None:
                raise IsolationError("Редактор не подтвердил текст восстановленной пробной вкладки.")
            self.require_single_window_tab(self.active_file.stem)
            log("ИЗОЛЯЦИЯ | Восстановлена собственная пробная вкладка; текст редактора и файла сверен.")
            return
        tab_id = self.require_single_window_tab()
        empty_editor = self.wait(lambda: self.editor(""), 3)
        if empty_editor is None:
            raise IsolationError("Редактор нового окна не подтверждён как пустой.")
        self.probe_tab_ids.add((self.window.handle, tab_id))
        empty_editor.click_input()
        self.focus_check()
        if not empty_editor.element_info.element.CurrentHasKeyboardFocus:
            raise IsolationError("Пустой редактор не получил фокус.")
        text = self.sample.rstrip("\n")
        send_keys(text.replace("\n", "{ENTER}") + "{ENTER}", with_spaces=True)
        if self.wait(self.editor, 3) is None:
            raise IsolationError("Пробный текст не подтверждён после ввода.")
        file_menu = next((name for name in self.menu_items() if norm(name) in {"файл", "file"}), None)
        if file_menu is None:
            raise IsolationError("Меню «Файл» не найдено.")
        self.open_menu(file_menu)
        save_as = next((item for name, item in self.menu_items().items()
                        if norm(name) in {"сохранить как", "save as"} and item.is_enabled()), None)
        if save_as is None:
            raise IsolationError("Команда «Сохранить как» недоступна.")
        self.focus_check()
        save_as.click_input()
        self.file_dialog()
        self.require_single_window_tab(self.active_file.stem)
        self.clean_saved_text = self.sample
        log("ИЗОЛЯЦИЯ | Одна пробная вкладка подготовлена и сохранена.")

    def attach(self, path):
        self.window = self.wait(lambda: next(
            (w for w in self.desktop.windows() if path.stem in w.window_text()), None), 12)
        if self.window is None:
            raise RuntimeError("Не найдено окно пробного файла.")
        self.probe_window_classes.add(self.window.class_name())
        self.show_test_window(self.window)
        self.window.set_focus()
        self.initial_edits = {tuple(e.element_info.element.GetRuntimeId()) for e in self.visible_edits()}

    @staticmethod
    def wait(check, seconds=5, interval=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = check()
            if result:
                return result
            time.sleep(interval)
        return None

    def owned(self, hwnd):
        return bool(hwnd and USER32.GetAncestor(hwnd, 3) == USER32.GetAncestor(self.window.handle, 3))

    def focus_check(self):
        if not self.owned(USER32.GetForegroundWindow()):
            raise RuntimeError("Пробный Блокнот потерял фокус. Проверка остановлена.")

    def roots(self):
        return [w for w in self.desktop.windows(process=self.window.process_id())
                if w.is_visible() and self.owned(w.handle)]

    def foreground_dialog(self, titles, match_controls=None, allow_foreground=None):
        hwnd = USER32.GetForegroundWindow()
        if not self.owned(hwnd) and not (allow_foreground and allow_foreground(hwnd)):
            return None
        root = self.desktop.window(handle=hwnd).wrapper_object()
        candidates = [root] if hwnd != self.window.handle else []
        candidates.extend(node for kind in ("Window", "Pane", "Group")
                          for node in root.descendants(control_type=kind) if node.is_visible())
        named = [item for item in candidates if norm(item.window_text()) in titles]
        if named:
            return named[0]
        if match_controls is not None:
            matched = {control_id(item): item for item in candidates if match_controls(item)}
            # Prefer the innermost surface when the same controls are exposed by its ancestors.
            innermost = [item for item in matched.values() if not any(
                control_id(child) in matched for child in item.descendants())]
            if len(innermost) == 1:
                return innermost[0]
        return None

    def menu_items(self):
        items = {}
        roots = [self.window]
        roots.extend(root for root in self.desktop.windows(process=self.window.process_id())
                     if root.element_info.control_type == "Menu" and root.is_visible())
        for root in roots:
            for item in root.descendants(control_type="MenuItem"):
                name = item.window_text().strip()
                if name and item.is_visible():
                    items.setdefault(name, item)
        return items

    def open_menu(self, name):
        self.focus_check()
        self.menu_items()[name].click_input()
        time.sleep(0.5)
        self.focus_check()

    def scan(self):
        menus = list(self.menu_items())
        if not menus:
            raise RuntimeError("Windows не показала доступные пункты меню.")
        self.main_menus = set(menus)
        entries = []
        for menu in menus:
            self.open_menu(menu)
            for name, item in self.menu_items().items():
                if name in menus:
                    continue
                probe = PROBES.get(norm(name))
                enabled = item.is_enabled()
                status = "pending" if probe else "skipped"
                entry = dict(menu=menu, name=name, enabled=enabled,
                             operation=probe, status=status)
                if status == "skipped":
                    entry["evidence"] = SKIP.get(norm(name), "Для этого пункта нет сценария проверки")
                entries.append(entry)
                log(f"НАЙДЕНО | {menu} → {name} | {entry.get('evidence', 'будет проверено')}")
            self.focus_check()
            send_keys("{ESC}")
            time.sleep(0.2)
        if not entries:
            raise RuntimeError("Меню открылись, но Windows не показала их содержимое.")
        zoom = next((entry for entry in entries if norm(entry["name"]) in {"масштаб", "zoom"}), None)
        if zoom:
            try:
                self.command(zoom)
                children = self.wait(lambda: {name: item for name, item in self.menu_items().items()
                                               if norm(name) in ZOOM}, 3) or {}
                for name, item in children.items():
                    entries.append(dict(menu=zoom["menu"], submenu=zoom["name"], name=name,
                                        enabled=item.is_enabled(), operation=ZOOM[norm(name)], status="pending"))
                    log(f"НАЙДЕНО | {zoom['menu']} → {zoom['name']} → {name} | будет проверено")
            except Exception as error:
                log(f"ОБХОД ПОДМЕНЮ | {error or type(error).__name__}")
            finally:
                self.focus_check()
                send_keys("{ESC}{ESC}")
        return entries

    def visible_edits(self):
        return [e for e in self.window.descendants(control_type="Edit") if e.is_visible()]

    def file_dialog(self, opening=False):
        titles = ({"open", "открыть", "открытие", "открытие файла", "open file"}
                  if opening else {"save as", "сохранение", "сохранить как", "сохранение файла"})

        def dialog():
            candidate = self.foreground_dialog(titles)
            if candidate is None:
                return None
            edits = [e for e in candidate.descendants(control_type="Edit") if e.is_visible()
                     and (e.element_info.automation_id == "1001"
                          or norm(e.window_text().rstrip(":")) in {"имя файла", "file name"})]
            return (candidate, edits[0]) if len(edits) == 1 else None

        found = self.wait(dialog)
        if found is None:
            raise DialogNotFound("Файловый диалог или поле имени не обнаружены.")
        self.focus_check()
        candidate, edit = found
        target = self.source.with_name(f"{'open' if opening else 'verified-copy'}-{uuid4().hex[:8]}.txt")
        self.probe_stems.add(target.stem)
        if opening:
            target.write_text(self.sample, encoding="utf-8")
        path = str(target)
        field = edit.iface_value
        edit.click_input()
        self.focus_check()
        send_keys("{HOME}+{END}{BACKSPACE}")
        if field.CurrentValue:
            raise RuntimeError("Не удалось очистить поле имени файла.")
        escaped = "".join("{" + char + "}" if char in "+^%~{}()" else char for char in path)
        send_keys(escaped, with_spaces=True, vk_packet=True)
        if field.CurrentValue != path:
            raise RuntimeError("Поле имени файла не приняло точный путь. Сохранение отменено.")
        log(f"ПУТЬ ПРОВЕРЕН | {field.CurrentValue}")
        buttons = [button for button in candidate.descendants(control_type="Button")
                   if norm(button.window_text().replace("&", "")) in ({"open", "открыть"} if opening else {"save", "сохранить"})
                   and button.is_visible() and button.is_enabled()]
        self.focus_check()
        if len(buttons) == 1:
            buttons[0].click_input()
        elif opening and edit.element_info.element.CurrentHasKeyboardFocus and field.CurrentValue == path:
            log("ОТКРЫТИЕ | Enter из проверенного поля имени файла")
            send_keys("{ENTER}")
        else:
            raise RuntimeError("Не найдена единственная активная кнопка подтверждения.")
        if opening:
            self.attach(target)
            self.active_file = target
            if self.wait(self.editor, 3) is None:
                raise RuntimeError("Пробный файл открыт, но текст в редакторе не подтверждён.")
            return "Открыт уникальный пробный файл, текст редактора сверен"
        expected = normalize_copy_text(self.sample)

        def matching_copy():
            try:
                actual = read_probe_copy(target)
            except (OSError, UnicodeError):
                return None
            return actual == self.sample or normalize_copy_text(actual) == expected

        if not self.wait(matching_copy, 10) and not target.is_file():
            titles = ", ".join(root.window_text() for root in self.roots() if root.window_text())
            raise RuntimeError(f"Копия не появилась по пути {path}. Открытые окна: {titles}")
        if not matching_copy():
            raise RuntimeError("Содержимое пробной копии отличается.")
        self.active_file = target
        return "Новый файл создан, текст совпадает после сверки переносов и пробелов в конце строк"

    @staticmethod
    def clean_text(value):
        return value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    @staticmethod
    def read_editor(control):
        try:
            value = control.iface_text.DocumentRange.GetText(-1)
            if value is not None:
                return value
        except (AttributeError, RuntimeError, OSError, COMError, NoPatternInterfaceError):
            pass
        return control.iface_value.CurrentValue

    def editor(self, expected_text=None):
        expected = self.clean_text(self.sample if expected_text is None else expected_text)
        for kind in ("Document", "Edit"):
            for control in self.window.descendants(control_type=kind):
                if not control.is_visible():
                    continue
                try:
                    if self.clean_text(self.read_editor(control)) == expected:
                        return control
                except (AttributeError, RuntimeError, OSError, COMError, NoPatternInterfaceError):
                    continue
        return None

    def focus_probe_editor(self):
        self.check_active_file()
        editor = self.wait(self.editor, 2)
        if editor is None:
            raise RuntimeError("Не найден главный редактор с пробным текстом.")
        editor.click_input()
        self.check_active_file()
        if not self.wait(lambda: editor.element_info.element.CurrentHasKeyboardFocus, 1):
            raise RuntimeError("Главное поле редактора не получило фокус.")
        return editor

    def selection_is(self, editor, expected):
        ranges = editor.iface_text.GetSelection()
        return ranges.Length == 1 and self.clean_text(ranges.GetElement(0).GetText(-1)) == expected

    def check_active_file(self):
        self.focus_check()
        if self.active_file.stem not in self.window.window_text():
            raise RuntimeError("Активная вкладка не соответствует подготовленной копии.")

    def find_or_replace(self, editor, needle, replacement=None, *, expected_matches=(), close_search=True):
        replace = replacement is not None

        def panel_fields():
            try:
                return choose_search_fields([e for e in self.visible_edits() if e.is_enabled()],
                                            control_id(editor), self.initial_edits, replace)
            except COMError:
                return None

        fields = self.wait(panel_fields)
        if not fields:
            self.check_active_file()
            controls = [e for e in self.visible_edits() if control_id(e) != control_id(editor)]
            log("UIA | Поля панели | " + repr([
                (e.element_info.automation_id, search_field_role(e), e.is_enabled()) for e in controls[:12]
            ]))
            raise RuntimeError("Не удалось однозначно распознать поля поиска и замены.")
        def current_field(index, check):
            fields_now = panel_fields()
            if not fields_now:
                return None
            field = fields_now[index]
            try:
                return field if check(field) else None
            except COMError:
                return None

        def has_focus(field):
            return field.element_info.element.CurrentHasKeyboardFocus

        for index, value in enumerate((needle, replacement) if replace else (needle,)):
            self.check_active_file()
            field = self.wait(lambda: current_field(index, lambda _: True))
            if field is None:
                raise RuntimeError("Поле поиска или замены исчезло.")
            if not has_focus(field):
                field.click_input()
            focused = self.wait(lambda: current_field(index, has_focus), 1, interval=0.1)
            if focused is None:
                self.check_active_file()
                field = current_field(index, lambda _: True)
                if field is not None:
                    field.set_focus()
                    focused = self.wait(lambda: current_field(index, has_focus), 1, interval=0.1)
            self.check_active_file()
            if focused is None or not has_focus(focused):
                raise RuntimeError("Поле поиска или замены не получило фокус.")
            escaped = "".join("{" + c + "}" if c in "+^%~{}()" else c for c in value)
            send_keys("{HOME}+{END}{BACKSPACE}" + escaped, with_spaces=True, vk_packet=True)
            if not self.wait(lambda: current_field(index, lambda field: field.iface_value.CurrentValue == value),
                             1, interval=0.1):
                raise RuntimeError("Введённое слово не совпало со значением поля.")
        self.check_active_file()
        if not replace:
            if not current_field(0, has_focus):
                raise RuntimeError("Поле поиска потеряло фокус перед подтверждением.")
            send_keys("{ENTER}")
            expected = expected_matches or (needle,)
            if not self.wait(lambda: any(self.selection_is(editor, value) for value in expected)):
                raise RuntimeError("Не подтверждено выделение найденного текста.")
            self.check_active_file()
            if close_search:
                send_keys("{ESC}")
            return

        buttons = [b for b in self.window.descendants(control_type="Button")
                   if b.is_visible() and b.is_enabled()
                   and norm(b.window_text()) in {"заменить все", "replace all"}]
        if len(buttons) != 1:
            raise RuntimeError("Не удалось однозначно найти кнопку «Заменить все».")
        if not all(current_field(index, lambda field: field.iface_value.CurrentValue == value)
                   for index, value in enumerate((needle, replacement))):
            raise RuntimeError("Значения полей изменились перед заменой.")
        self.check_active_file()
        buttons[0].click_input()
        expected = self.clean_text(self.sample.replace(needle, replacement))
        if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == expected):
            raise RuntimeError("Текст после замены не совпал с ожидаемым.")
        self.check_active_file()
        send_keys("{ESC}")

    def check_text(self, action, editor):
        if action == "find_text":
            self.find_or_replace(editor, "alpha")
            return "В пробном документе найдено и выделено слово alpha"
        self.find_or_replace(editor, "alpha", "omega")
        return "Оба alpha заменены на omega, весь текст сверен; " + self.reset_after_verified(editor)

    def menu_command(self, entry, *, required=True):
        self.open_menu(entry["menu"])
        if entry.get("submenu"):
            parent = self.menu_items().get(entry["submenu"])
            if parent is None:
                raise RuntimeError("Подменю не найдено.")
            self.focus_check()
            parent.click_input()
        item = self.wait(lambda: self.menu_items().get(entry["name"]), 2)
        if item is None or not item.is_enabled():
            if not required:
                return None
            raise RuntimeError("Пункт недоступен в подготовленном состоянии.")
        return item

    def command(self, entry):
        self.invoke_menu_item(self.menu_command(entry))

    def invoke_menu_item(self, item):
        self.focus_check()
        if self.clean_windows:
            self.check_active_file()
        try:
            item.invoke()
        except (AttributeError, NotImplementedError, NoPatternInterfaceError):
            self.focus_check()
            if not item.is_visible() or not item.is_enabled():
                raise RuntimeError("Пункт меню недоступен для клика.")
            item.click_input()

    def edit_keys(self, editor, keys):
        self.focus_check()
        editor.click_input()
        self.focus_check()
        send_keys(keys)

    def return_from_settings(self):
        self.focus_check()
        buttons = [b for b in self.window.descendants(control_type="Button")
                   if b.is_visible() and b.is_enabled()
                   and norm(b.window_text()) in {"назад", "back"}]
        if len(buttons) != 1:
            return False
        buttons[0].click_input()
        return bool(self.wait(lambda: self.menu_items().keys() >= self.main_menus, 3))

    def fresh_probe(self, suffix=".txt", text=None):
        self.focus_check()
        target = self.source.with_name(f"recovery-{uuid4().hex[:8]}{suffix}")
        self.probe_stems.add(target.stem)
        target.write_text(self.sample if text is None else text, encoding="utf-8")
        subprocess.Popen(["notepad.exe", str(target)])
        self.attach(target)
        self.active_file = target
        log(f"ВОССТАНОВЛЕНИЕ | Открыт новый пробный файл {target}")

    def tabs(self):
        return self.window.descendants(control_type="TabItem")

    def probe_stem(self, label):
        return next((stem for stem in self.probe_stems
                     if re.search(rf"(?<![\w-]){re.escape(stem)}(?![\w-])", label, re.I)), None)

    def close_test_tab(self, tab_id, stem, *, confirm_empty=None):
        handle = self.window.handle

        def current():
            if not USER32.IsWindow(handle):
                return None
            return next((t for t in self.tabs() if control_id(t) == tab_id), None)

        tab = current()
        if tab is None or not tab.is_visible():
            return False
        if (stem and self.probe_stem(tab.window_text()) != stem
                or not stem and confirm_empty is None and (
                    (handle, tab_id) not in self.probe_tab_ids
                    or norm(tab.window_text()) not in UNTITLED_TABS)):
            return False
        self.window.set_focus()
        if USER32.GetForegroundWindow() != handle:
            return False
        tab.click_input()
        if not self.wait(lambda: tab.iface_selection_item.CurrentIsSelected, 2):
            return False
        if USER32.GetForegroundWindow() != handle:
            return False
        if stem and self.probe_stem(self.window.window_text()) != stem:
            return False
        if confirm_empty is not None and not self.wait(confirm_empty, 1):
            return False
        buttons = [b for b in tab.descendants(control_type="Button") if b.is_visible() and b.is_enabled()
                   and norm(b.window_text()) in {"закрыть", "закрыть вкладку", "close", "close tab"}]
        if confirm_empty is not None:
            tab = current()
            if (tab is None or USER32.GetForegroundWindow() != handle
                    or not tab.iface_selection_item.CurrentIsSelected or not confirm_empty()):
                return False
        if len(buttons) == 1:
            buttons[0].click_input()
        else:
            send_keys("^w")

        def closed():
            return not USER32.IsWindow(handle) or (current() is None and (
                not stem or self.probe_stem(self.window.window_text()) != stem
                and all(self.probe_stem(t.window_text()) != stem for t in self.tabs())))

        if self.wait(closed, 1):
            self.probe_tab_ids.discard((handle, tab_id))
            return True
        if stem:
            dialog = self.foreground_dialog(set(), lambda item: any(
                self.probe_stem(node.window_text()) == stem and node.is_visible()
                for node in item.descendants(control_type="Text")))
            if dialog is not None:
                discard = [b for b in dialog.descendants(control_type="Button")
                           if b.is_visible() and b.is_enabled()
                           and norm(b.window_text()) in {"не сохранять", "don't save", "do not save"}]
                if len(discard) == 1 and self.owned(USER32.GetForegroundWindow()):
                    discard[0].click_input()
                    if self.wait(closed, 2):
                        self.probe_tab_ids.discard((handle, tab_id))
                        return True
        return False

    def cleanup(self):
        try:
            count = 0
            for window in self.desktop.windows():
                if window.class_name() not in self.probe_window_classes or not window.is_visible():
                    continue
                self.window = window
                attempted = set()
                while USER32.IsWindow(window.handle):
                    candidates = [(control_id(tab), self.probe_stem(tab.window_text()))
                                  for tab in self.tabs()]
                    target = next(((tid, stem) for tid, stem in candidates if tid not in attempted
                                   and (stem or (window.handle, tid) in self.probe_tab_ids)), None)
                    if target is None:
                        if self.probe_stem(window.window_text()):
                            log(f"УБОРКА | HWND {window.handle}: пробная вкладка не найдена в UIA.")
                        break
                    tab_id, stem = target
                    attempted.add(tab_id)
                    try:
                        done = self.close_test_tab(tab_id, stem)
                    except Exception as error:
                        log(f"УБОРКА | HWND {window.handle} | {error or type(error).__name__}")
                        done = False
                    if done:
                        count += 1
                    else:
                        log(f"УБОРКА | Оставлена пробная вкладка {stem or tab_id}: закрытие не подтверждено.")
                        break
            log(f"УБОРКА | Подтверждено закрытий пробных вкладок: {count}")
        except Exception as error:
            log(f"УБОРКА | Не завершена: {error or type(error).__name__}")

    def remember_created_tab(self, before):
        added = [tab for tab in self.tabs() if control_id(tab) not in before]
        if len(added) == 1:
            self.probe_tab_ids.add((self.window.handle, control_id(added[0])))

    def wait_new_selected_tab(self, before):
        def selected_new():
            if (USER32.GetForegroundWindow() != self.window.handle
                    or self.active_file.stem in self.window.window_text()):
                return None
            try:
                selected = [tab for tab in self.tabs() if tab.iface_selection_item.CurrentIsSelected]
                return selected[0] if len(selected) == 1 and control_id(selected[0]) not in before else None
            except (COMError, NoPatternInterfaceError):
                return None
        return self.wait(selected_new, 3)

    def check_tab(self, entry):
        operation = entry["operation"]
        self.check_active_file()
        if operation == "close_tab":
            previous, window = self.active_file, self.window
            opener = next((e for e in self.entries if e["operation"] == "open_file"), None)
            if opener is None:
                raise RuntimeError("Не найдено меню открытия второй пробной вкладки.")
            self.command(opener)
            self.file_dialog(opening=True)
            target = self.active_file
            if self.window.handle != window.handle or not any(previous.stem in tab.window_text() for tab in self.tabs()):
                raise RuntimeError("Отдельная пробная вкладка в исходном окне не подтверждена.")
            self.check_active_file()
            self.command(entry)
            if not self.wait(lambda: target.stem not in self.window.window_text()
                             and all(target.stem not in tab.window_text() for tab in self.tabs()), 3):
                raise RuntimeError("Команда отправлена, пробная вкладка ещё видна.")
            self.active_file = previous
            if not self.activate_probe():
                raise RuntimeError("Пробная вкладка закрылась, возврат к исходному файлу не подтверждён.")
            return "Именованная пробная вкладка исчезла, исходная пробная вкладка активна"

        before = {control_id(tab) for tab in self.tabs()}
        markdown = operation == "new_markdown"
        before_markdown = {self.control_key(c) for c in self.markdown_controls()} if markdown else set()
        self.command(entry)
        log(f"ВКЛАДКИ | {entry['name']} | команда отправлена")
        created = self.wait_new_selected_tab(before)
        if created is None:
            self.activate_probe()
            raise RuntimeError("После команды UIA не показала новую выбранную вкладку.")
        created_id = control_id(created)
        after = {control_id(tab) for tab in self.tabs()}
        if markdown:
            self.scan_markdown(before_markdown)
        # Close an anonymous tab only when the inventory proves exactly one addition.
        if after == before | {created_id}:
            self.probe_tab_ids.add((self.window.handle, created_id))
            buttons = [b for b in created.descendants(control_type="Button")
                       if b.is_visible() and b.is_enabled()
                       and norm(b.window_text()) in {"закрыть", "закрыть вкладку", "close", "close tab"}]
            if len(buttons) == 1 and created.iface_selection_item.CurrentIsSelected:
                self.focus_check()
                buttons[0].click_input()
                if self.wait(lambda: all(control_id(tab) != created_id for tab in self.tabs()), 2):
                    self.probe_tab_ids.discard((self.window.handle, created_id))
        else:
            log("УБОРКА | Вкладка без имени оставлена: состав вкладок изменился неоднозначно.")
        if not self.activate_probe():
            raise RuntimeError("Новая вкладка опознана, возврат к пробному файлу не подтверждён.")
        return f"UIA подтвердила новую активную вкладку; число доступных TabItem {len(before)} → {len(after)}"

    def prepare_single_tab(self, action, entry):
        self.check_active_file()
        handle = self.window.handle
        other_windows = [w for w in self.desktop.windows(process=self.window.process_id())
                         if w.handle != handle and w.is_visible() and w.class_name() == self.window.class_name()]
        if other_windows and action in {"save_all", "exit_app"}:
            raise IsolationError("Открыто другое окно того же процесса Блокнота. "
                                 "Для проверки нужен отдельный сеанс без пользовательских окон.")
        tabs = self.tabs()
        original = [tab for tab in tabs if self.active_file.stem in tab.window_text()]
        if len(original) != 1:
            raise IsolationError("Пробная вкладка не опознана однозначно.")
        original_id = control_id(original[0])
        extras = [tab for tab in tabs if control_id(tab) != original_id]
        if any((handle, control_id(tab)) not in self.probe_tab_ids
               and not self.probe_stem(tab.window_text()) for tab in extras):
            if action == "close_window":
                return self.check_isolated_close(entry)
            raise IsolationError("Есть вкладки вне текущего прогона. "
                                 "Для проверки нужен отдельный сеанс без пользовательских вкладок.")
        for extra in extras:
            tab_id = control_id(extra)
            current = next((tab for tab in self.tabs() if control_id(tab) == tab_id), None)
            if current is None or not self.close_test_tab(tab_id, self.probe_stem(current.window_text())):
                raise RuntimeError("Пробная вкладка не закрылась или требует подтверждения.")
        if len(self.tabs()) != 1 or not self.activate_probe():
            raise RuntimeError("Единственная пробная вкладка не подтверждена.")
        log(f"ИЗОЛЯЦИЯ | {action} | одна пробная вкладка")

    def check_isolated_close(self, entry):
        previous = self.window
        windows = {w.handle for w in self.desktop.windows()}
        new_window = next((e for e in self.entries if e["operation"] == "new_window"), None)
        if new_window is None:
            raise IsolationError("Команда создания отдельного окна недоступна.")
        try:
            self.command(new_window)
            dedicated = self.wait(lambda: next((
                w for w in self.desktop.windows(process=previous.process_id())
                if w.handle not in windows and w.handle == USER32.GetForegroundWindow()
                and w.class_name() == previous.class_name()), None), 4)
            if dedicated is None:
                raise IsolationError("Не подтверждено создание отдельного окна.")
            self.window = dedicated
            self.show_test_window(dedicated)
            tabs = self.wait(lambda: self.tabs(), 2)
            if (not tabs or len(tabs) != 1
                    or norm(tabs[0].window_text()) not in UNTITLED_TABS):
                raise IsolationError("В отдельном окне не подтверждена одна вкладка без имени.")
            tab_id = control_id(tabs[0])
            self.probe_tab_ids.add((dedicated.handle, tab_id))
            self.command(entry)
            if not self.wait(lambda: not USER32.IsWindow(dedicated.handle), 3):
                raise IsolationError("Закрытие отдельного пробного окна не подтверждено.")
            self.probe_tab_ids.discard((dedicated.handle, tab_id))
        except Exception as error:
            raise IsolationError(f"Отдельное пробное окно не подготовлено: {error}") from error
        finally:
            self.window = previous
            self.window.set_focus()
        return "Отдельное пробное окно с одной вкладкой закрыто"

    def check_toggle(self, entry):
        def toggle(item, state):
            self.focus_check()
            if not item.is_visible() or not item.is_enabled():
                raise RuntimeError("Переключатель недоступен для клика.")
            if state is not None and state[0] == "toggle":
                try:
                    item.iface_toggle.Toggle()
                except (NoPatternInterfaceError, NotImplementedError):
                    self.focus_check()
                    item.click_input()
            else:
                item.click_input()
            self.focus_check()
            if entry["name"] in self.menu_items():
                send_keys("{ESC}")

        status_bar = entry["operation"] == "status_bar"
        bars = lambda: sum(b.is_visible() for b in self.window.descendants(control_type="StatusBar"))
        self.check_active_file()
        before_bars = bars() if status_bar else None
        try:
            if entry["operation"] in {"word_wrap", "status_bar"}:
                self.focus_probe_editor()
            item = self.menu_command(entry)
            try:
                before = menu_toggle_state(item)
            except RuntimeError:
                if not status_bar or not before_bars:
                    raise
                before = None
            log(f"UIA | {entry['name']} | исходное состояние: {before}; StatusBar: {before_bars}")
            toggle(item, before)
            log(f"UIA | {entry['name']} | переключение отправлено; повторно считываю состояние")
            item = self.menu_command(entry)
            if before is not None:
                changed = self.wait(lambda: (state := menu_toggle_state(item)) != before
                                    and state[0] == before[0], 1.5, interval=0.1)
            else:
                changed = self.wait(lambda: bars() != before_bars, 1.5, interval=0.1)
            if not changed:
                raise RuntimeError("Состояние переключателя не изменилось или не подтверждено.")
            toggle(item, before)
            if before is not None:
                item = self.menu_command(entry)
                restored = self.wait(lambda: menu_toggle_state(item) == before, 2, interval=0.1)
            else:
                restored = self.wait(lambda: bars() == before_bars, 2)
            if not restored:
                raise RuntimeError("Возврат переключателя в исходное состояние не подтверждён.")
            return "Переключение и восстановление исходного состояния подтверждены"
        finally:
            if self.owned(USER32.GetForegroundWindow()):
                send_keys("{ESC}")

    def check_markdown_view(self, entry):
        previous_file, previous_window = self.active_file, self.window
        self.fresh_probe(suffix=".md", text="# Druzhishe probe\n\n**alpha beta**\n")
        try:
            return self.check_toggle(entry)
        finally:
            self.focus_check()
            self.active_file, self.window = previous_file, previous_window
            self.window.set_focus()
            if not self.activate_probe():
                raise RuntimeError("Возврат из пробной Markdown-вкладки не подтверждён.")

    def reset_after_verified(self, editor, saved=False):
        try:
            self.edit_keys(editor, "^z")
            restored = self.wait(self.editor, 2)
            if restored and not saved:
                return "исходный текст восстановлен"
            if restored:
                self.edit_keys(restored, "^s")
                if self.wait(lambda: self.active_file.read_text(encoding="utf-8-sig") == self.sample, 2):
                    return "исходный текст восстановлен в редакторе и на диске"
        except Exception as error:
            log(f"ВОССТАНОВЛЕНИЕ | {error or type(error).__name__}")
        try:
            self.fresh_probe()
            return "для следующих проверок открыт свежий пробный файл"
        except Exception as error:
            return f"подготовка следующего пробного файла не удалась ({error or type(error).__name__})"

    def recover(self):
        if not self.owned(USER32.GetForegroundWindow()):
            return
        send_keys("{ESC}")
        if self.recovery_editor is not None and not self.wait(self.editor, 1):
            self.fresh_probe()

    def activate_probe(self):
        self.focus_check()
        if self.active_file.stem in self.window.window_text():
            return True
        matches = [tab for tab in self.window.descendants(control_type="TabItem")
                   if tab.is_visible() and self.active_file.stem in tab.window_text()]
        if len(matches) == 1:
            matches[0].click_input()
            return bool(self.wait(lambda: self.active_file.stem in self.window.window_text(), 2))
        return False

    def zoom_level(self):
        values = {int(match.group(1)) for control in self.window.descendants(control_type="Text")
                  if control.is_visible()
                  if (match := re.fullmatch(r"(\d+)\s*%", control.window_text().strip()))}
        return next(iter(values)) if len(values) == 1 else None

    def check_zoom(self, entry):
        original = self.zoom_level()
        if original != 100:
            raise RuntimeError("Для обратимой проверки нужен видимый исходный масштаб 100%.")
        by_operation = {e["operation"]: e for e in self.entries if e.get("submenu") == entry["submenu"]}
        reset = by_operation.get("zoom_reset")
        increase = by_operation.get("zoom_in")
        if not reset or (entry["operation"] == "zoom_reset" and not increase):
            raise RuntimeError("В подменю нет команды для подготовки или восстановления масштаба.")
        action = entry["operation"]
        changed = None
        try:
            if action == "zoom_reset":
                self.command(increase)
                if not self.wait(lambda: (self.zoom_level() or 0) > original, 3):
                    raise RuntimeError("Не удалось увеличить масштаб перед проверкой сброса.")
            self.command(entry)
            def expected_level():
                level = self.zoom_level()
                if level is None:
                    return None
                if ((action == "zoom_in" and level > original)
                        or (action == "zoom_out" and level < original)
                        or (action == "zoom_reset" and level == original)):
                    return level
                return None
            changed = self.wait(expected_level, 3)
            if changed is None:
                raise RuntimeError("Индикатор не подтвердил ожидаемый масштаб.")
        finally:
            if self.zoom_level() != original:
                self.focus_check()
                send_keys("{ESC}{ESC}")
                self.command(reset)
                if not self.wait(lambda: self.zoom_level() == original, 3):
                    raise RuntimeError("Не подтверждено восстановление масштаба 100%.")
        return f"Индикатор показал {changed}%; исходный масштаб 100% восстановлен"

    def markdown_controls(self):
        kinds = {"Button", "SplitButton", "ToggleButton", "MenuItem", "ListItem", "ComboBox",
                 "RadioButton", "CheckBox", "Custom", "DataItem"}
        return [control for root in self.roots() for control in root.descendants()
                if control.element_info.control_type in kinds and control.is_visible()
                and (control.window_text().strip() or control.element_info.automation_id)]

    @staticmethod
    def control_key(control):
        info = control.element_info
        return info.control_type, control.window_text().strip(), info.automation_id

    def scan_markdown(self, before):
        groups = {
            "стили заголовков": r"heading|заголов|стиль текста|text\s*style|paragraph\s*style|\bh[1-6]\b",
            "списки": r"\blists?\b|list(button|menu|dropdown)|спис[ок]|bullets|numbering",
            "таблицы": r"\btables?\b|table(button|menu|dropdown)|таблиц",
        }
        def metadata(control):
            try:
                help_text = control.element_info.element.CurrentHelpText
            except (AttributeError, COMError):
                help_text = ""
            return " ".join(part for part in (*self.control_key(control)[1:], help_text) if part)

        initial = self.wait(lambda: [c for c in self.markdown_controls() if self.control_key(c) not in before], 3)
        if not initial:
            log("MARKDOWN | Панель не обнаружена.")
            return
        seen = {(e.get("submenu"), e["name"], e.get("automation_id")) for e in self.entries
                if e["menu"] == "Панель Markdown"}

        def record(control, parent=None):
            kind, name, automation_id = self.control_key(control)
            name = name or automation_id
            key = parent, name, automation_id
            if key not in seen:
                self.entries.append(dict(menu="Панель Markdown", submenu=parent, name=name,
                                         automation_id=automation_id, control_type=kind,
                                         enabled=control.is_enabled(), operation=None, status="discovered",
                                         evidence="Элемент обнаружен, действие не проверялось."))
                seen.add(key)
                log(f"MARKDOWN | {parent + ' → ' if parent else ''}{name}")

        # Toolbar controls are above the editor, including controls with no visible text.
        top = self.window.rectangle().top
        toolbar = [c for c in initial if c.rectangle().top <= top + 180]
        for control in toolbar:
            record(control)
        for group, pattern in groups.items():
            try:
                current = self.markdown_controls()
                candidates = [c for c in current if self.control_key(c) not in before
                              and c.rectangle().top <= top + 180 and c.is_enabled()
                              and re.search(pattern, metadata(c), re.IGNORECASE)]
                dropdowns = [c for c in candidates
                             if c.element_info.control_type in {"SplitButton", "ComboBox"}]
                if len(dropdowns) == 1:
                    candidates = dropdowns
                if len(candidates) != 1:
                    log(f"MARKDOWN | {group}: кандидатов {len(candidates)}; "
                        f"панель: {[self.control_key(c) for c in toolbar][:18]}")
                    continue
                previous = {self.control_key(c) for c in current}
                self.focus_check()
                trigger = candidates[0]
                try:
                    trigger.iface_expand_collapse.Expand()
                except (AttributeError, NoPatternInterfaceError):
                    if trigger.element_info.control_type == "SplitButton":
                        # Main SplitButton click can apply formatting instead of opening the menu.
                        arrows = [c for c in trigger.descendants(control_type="Button")
                                  if c.is_visible() and c.is_enabled()
                                  and re.search(r"expand|dropdown|arrow|раскры|подменю", metadata(c), re.I)]
                        if len(arrows) != 1:
                            raise RuntimeError("Стрелка раскрытия SplitButton не подтверждена.")
                        trigger = arrows[0]
                    self.focus_check()
                    trigger.click_input()
                added = self.wait(lambda: [c for c in self.markdown_controls()
                                            if self.control_key(c) not in previous], 2) or []
                for control in added:
                    record(control, group)
                if not added:
                    log(f"MARKDOWN | {group}: элементы подменю не обнаружены")
            except Exception as error:
                log(f"MARKDOWN | {group}: {error or type(error).__name__}")
            finally:
                self.focus_check()
                send_keys("{ESC}")

    def check_pdf_print(self, allow_confirmation=False):
        printer = "microsoft print to pdf"
        original = self.window.handle
        previous_windows = {w.handle for w in self.desktop.windows()}
        hosted_print = set()

        def confirm_window(hwnd, message):
            if USER32.GetForegroundWindow() != hwnd:
                raise RuntimeError("Системное окно потеряло фокус до подтверждения.")
            log(f"PDF | Ожидаю подтверждения системного окна HWND {hwnd}.")
            # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND.
            response = USER32.MessageBoxW(ctypes.c_void_p(hwnd), ctypes.c_wchar_p(message),
                                          ctypes.c_wchar_p("Дружище — пробная печать в PDF"), 0x10124)
            if response != 6:
                raise RuntimeError("Продолжение проверки PDF отменено пользователем.")
            if not self.wait(lambda: USER32.IsWindow(hwnd) and USER32.GetForegroundWindow() == hwnd, 2):
                raise RuntimeError("Подтверждённое системное окно не вернулось на передний план.")
            log(f"PDF | Пользователь подтвердил системное окно HWND {hwnd}.")

        def same_dialog(dialog, expected_hwnd):
            hwnd = USER32.GetForegroundWindow()
            if (hwnd != expected_hwnd or not (self.owned(hwnd) or hwnd in hosted_print)
                    or (dialog.handle and hwnd != USER32.GetAncestor(dialog.handle, 2))):
                raise RuntimeError("Диалог потерял фокус.")

        def selected_pdf(dialog):
            for combo in dialog.descendants(control_type="ComboBox"):
                if not combo.is_visible():
                    continue
                try:
                    if norm(combo.selected_text() or "") == printer:
                        return True
                except (NoPatternInterfaceError, NotImplementedError, COMError):
                    continue
            for option in dialog.descendants(control_type="ListItem"):
                if norm(option.window_text()) != printer or not option.is_visible():
                    continue
                try:
                    if option.iface_selection_item.CurrentIsSelected:
                        return True
                except (NoPatternInterfaceError, COMError):
                    continue
            return False

        def print_buttons(dialog):
            return [e for e in dialog.descendants(control_type="Button")
                    if e.is_visible() and e.is_enabled() and norm(e.window_text()) in {"печать", "print"}]

        def print_controls(dialog):
            if len(print_buttons(dialog)) != 1:
                return False
            if selected_pdf(dialog):
                return True
            return any(norm(e.window_text()) == printer and e.is_visible()
                       for e in dialog.descendants(control_type="ListItem"))

        def recognize_host(hwnd):
            if not hwnd or hwnd in previous_windows:
                return False
            cls = ctypes.create_unicode_buffer(256)
            USER32.GetClassNameW(hwnd, cls, len(cls))
            if cls.value != "ApplicationFrameWindow":
                return False
            root = self.desktop.window(handle=hwnd).wrapper_object()
            # The random probe name distinguishes this dialog from another application's print job.
            labels = [root.window_text(), *(item.window_text() for item in root.descendants(control_type="Text")
                                           if item.is_visible())]
            linked = any(self.probe_stem(label) == self.active_file.stem for label in labels)
            if linked and print_controls(root):
                hosted_print.add(hwnd)
                return True
            return False

        self.focus_check()
        if USER32.GetForegroundWindow() != original:
            raise RuntimeError("Редактор Блокнота не на переднем плане.")
        if self.active_file.stem not in self.window.window_text():
            raise RuntimeError("Активная вкладка не является пробным файлом.")
        menu_print = next((item for item in self.entries
                           if norm(item["name"]) in {"печать", "print"} and norm(item["menu"]) in {"файл", "file"}), None)
        item = self.menu_command(menu_print, required=False) if menu_print is not None else None
        self.check_active_file()
        if item is not None:
            route = "interface"
            log("PDF | Маршрут: Файл → Печать.")
            self.invoke_menu_item(item)
        else:
            route = "shortcut"
            if menu_print is not None:
                send_keys("{ESC}")
            self.focus_probe_editor()
            log("PDF | Пункт меню недоступен. Резервный маршрут: Ctrl+P.")
            send_keys("^p")
        dialog = self.wait(lambda: self.foreground_dialog(
            {"печать", "print", "параметры печати", "print settings"}, print_controls, recognize_host),
            6 if route == "interface" else 3, interval=0.1)
        if dialog is None:
            hwnd = USER32.GetForegroundWindow()
            focus = ("пробный редактор" if hwnd == original else
                     "связанное окно" if self.owned(hwnd) else "постороннее окно")
            window_class = ctypes.create_unicode_buffer(256)
            USER32.GetClassNameW(hwnd, window_class, len(window_class))
            log(f"PDF | Диалог не распознан | Фокус: {focus}; HWND: {hwnd}; класс: {window_class.value}")
            confirm_candidate = False
            if window_class.value == "ApplicationFrameWindow":
                try:
                    root = self.desktop.window(handle=hwnd).wrapper_object()
                    print_labels = {"печать", "print", "параметры печати", "print settings"}
                    visible = [item for item in root.descendants() if item.is_visible()]
                    counts = {kind: sum(item.element_info.control_type == kind for item in visible)
                              for kind in ("Button", "ComboBox", "ListItem", "Text")}
                    title_matches = norm(root.window_text()) in print_labels
                    probe_matches = self.probe_stem(root.window_text()) == self.active_file.stem or any(
                        self.probe_stem(item.window_text()) == self.active_file.stem
                        for item in visible if item.element_info.control_type == "Text")
                    pdf_visible = any(norm(item.window_text()) == printer for item in visible
                                      if item.element_info.control_type in {"ListItem", "Text"})
                    confirm_candidate = (hwnd not in previous_windows
                                         and len(print_buttons(root)) == 1
                                         and (pdf_visible or selected_pdf(root)))
                    log(f"PDF | UIA кадра | новое окно: {hwnd not in previous_windows}; "
                        f"заголовок печати: {title_matches}; имя пробы: {probe_matches}; "
                        f"PDF в видимых элементах: {pdf_visible}; кнопок печати: {len(print_buttons(root))}; "
                        f"элементы: {counts}")
                except Exception as error:
                    log(f"PDF | UIA кадра недоступен: {type(error).__name__}")
            if allow_confirmation and confirm_candidate:
                confirm_window(hwnd, f"Это окно печати пробного документа {self.active_file.name}, "
                                     "которое только что открыл Блокнот?\n\n"
                                     "Нажми «Да», чтобы продолжить тест с Microsoft Print to PDF.")
                hosted_print.add(hwnd)
                dialog = self.desktop.window(handle=hwnd).wrapper_object()
                if len(print_buttons(dialog)) != 1:
                    raise RuntimeError("Элементы окна изменились после подтверждения. Задание не отправлено.")
                same_dialog(dialog, hwnd)
            else:
                raise RuntimeError("Диалог печати не распознан в безопасном контексте. Задание не отправлено.")
        print_hwnd = USER32.GetForegroundWindow()
        if not selected_pdf(dialog):
            for combo in dialog.descendants(control_type="ComboBox"):
                if combo.is_visible() and combo.is_enabled():
                    try:
                        if any(norm(value) == printer for value in combo.texts()):
                            same_dialog(dialog, print_hwnd)
                            combo.select("Microsoft Print to PDF")
                            break
                    except (NoPatternInterfaceError, NotImplementedError, COMError):
                        continue
            if not selected_pdf(dialog):
                options = [e for e in dialog.descendants(control_type="ListItem")
                           if e.is_visible() and e.is_enabled() and norm(e.window_text()) == printer]
                if len(options) == 1:
                    same_dialog(dialog, print_hwnd)
                    options[0].click_input()
        if not self.wait(lambda: selected_pdf(dialog), 2):
            raise RuntimeError("Не удалось подтвердить Microsoft Print to PDF. Задание не отправлено.")
        buttons = print_buttons(dialog)
        if len(buttons) != 1:
            raise RuntimeError("Кнопка печати не определена однозначно. Задание не отправлено.")
        same_dialog(dialog, print_hwnd)
        if not selected_pdf(dialog):
            raise RuntimeError("Выбранный принтер изменился. Задание не отправлено.")
        windows_before_submit = {w.handle for w in self.desktop.windows()} if allow_confirmation else set()
        same_dialog(dialog, print_hwnd)
        buttons[0].click_input()

        def save_from_host(hwnd):
            if print_hwnd in hosted_print and hwnd and USER32.GetAncestor(hwnd, 3) == print_hwnd:
                hosted_print.add(hwnd)
                return True
            return False

        save_titles = {
            "сохранение результата печати", "сохранить результат печати как",
            "сохранение результата печати как", "сохранение результатов печати", "save print output as",
        }

        def save_fields(dialog):
            edits = [e for e in dialog.descendants(control_type="Edit")
                     if e.is_visible() and e.element_info.automation_id == "1001"]
            buttons = [e for e in dialog.descendants(control_type="Button")
                       if e.is_visible() and e.is_enabled()
                       and norm(e.window_text().replace("&", "")) in {"сохранить", "save"}]
            return edits, buttons

        save = self.wait(lambda: self.foreground_dialog(save_titles, allow_foreground=save_from_host), 8)
        save_hwnd = USER32.GetForegroundWindow()
        if save is None and allow_confirmation and save_hwnd and save_hwnd not in windows_before_submit:
            candidate = self.desktop.window(handle=save_hwnd).wrapper_object()
            if norm(candidate.window_text()) in save_titles and all(len(items) == 1 for items in save_fields(candidate)):
                confirm_window(save_hwnd, "Это окно сохранения результата только что запущенной пробной печати?\n\n"
                                         "Нажми «Да», чтобы сохранить тестовый PDF в папку «Данные\\Тесты».")
                save = self.desktop.window(handle=save_hwnd).wrapper_object()
                if norm(save.window_text()) not in save_titles:
                    raise RuntimeError("После подтверждения заголовок окна сохранения изменился.")
                hosted_print.add(save_hwnd)
        if save is None:
            raise RuntimeError("Диалог сохранения результата печати не распознан.")
        edits, buttons = save_fields(save)
        if len(edits) != 1 or len(buttons) != 1:
            raise RuntimeError("Не распознаны поле имени PDF и кнопка сохранения.")
        target = self.source.with_name(f"Notepad_Test_{uuid4().hex[:8]}.pdf")
        same_dialog(save, save_hwnd)
        edit = edits[0]
        edit.click_input()
        same_dialog(save, save_hwnd)
        if not edit.element_info.element.CurrentHasKeyboardFocus:
            raise RuntimeError("Поле имени PDF не получило фокус.")
        send_keys("{HOME}+{END}{BACKSPACE}")
        escaped = "".join("{" + c + "}" if c in "+^%~{}()" else c for c in str(target))
        send_keys(escaped, with_spaces=True, vk_packet=True)
        if edit.iface_value.CurrentValue != str(target):
            raise RuntimeError("Путь PDF не подтвердился в поле сохранения.")
        same_dialog(save, save_hwnd)
        buttons[0].click_input()

        if not self.wait(lambda: valid_pdf(target), 12):
            raise RuntimeError("Готовый PDF по указанному пути не подтверждён.")
        return f"Создан пробный PDF: {target}", route

    def edit_menu_action(self, names, *, before_invoke=None):
        self.check_active_file()
        menus = [name for name in self.menu_items() if norm(name) in {"правка", "изменить", "edit"}]
        if len(menus) != 1:
            raise RuntimeError("Меню «Правка» не распознано однозначно.")
        self.open_menu(menus[0])
        items = [item for name, item in self.menu_items().items()
                 if norm(name) in names and item.is_visible() and item.is_enabled()]
        if len(items) != 1:
            self.check_active_file()
            send_keys("{ESC}")
            raise RuntimeError("Команда в меню «Правка» не распознана однозначно.")
        self.check_active_file()
        if before_invoke is not None:
            before_invoke()
        self.invoke_menu_item(items[0])

    def check_clipboard(self, entry, strategy):
        operation = entry["operation"]
        keys, labels = CLIPBOARD_PROBES[operation]
        editor = self.focus_probe_editor()
        self.recovery_editor = editor
        is_paste = operation in {"key_paste", "key_paste_plain"}
        payload = "Проба-" + uuid4().hex[:8]
        selected = self.sample.splitlines()[0][0]
        initial = self.clean_text(self.sample)
        with probe_clipboard(self.window.process_id(),
                             verify_nonempty=self.verify_nonempty_clipboard) as (seed, copied, check_current):
            seed(payload)
            self.check_active_file()
            send_keys("^{HOME}" if is_paste else "^{HOME}+{RIGHT}")
            if not is_paste and not self.wait(lambda: self.selection_is(editor, selected), 2):
                raise RuntimeError("Пробное выделение перед копированием или вырезанием не подтверждено.")
            self.check_active_file()
            check_current()
            if strategy == "clipboard_via_menu":
                self.edit_menu_action(labels, before_invoke=check_current)
                entry["checked_via"] = "interface"
            else:
                send_keys(keys)
                entry["checked_via"] = "shortcut"
            if not is_paste:
                def copied_from_probe():
                    self.check_active_file()
                    return copied(selected)

                if not self.wait(copied_from_probe, 2):
                    raise RuntimeError("Буфер не подтвердил точный пробный символ.")
            expected = (payload + initial if is_paste else initial[1:] if operation == "key_cut" else initial)
            if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == expected, 2):
                raise RuntimeError("Текст документа после команды буфера не совпал с ожидаемым.")
        if is_paste or operation == "key_cut":
            return "Пробный текст сверен после команды; " + self.reset_after_verified(editor)
        return "Буфер подтвердил пробный символ, исходный текст редактора совпал."

    def check_reopen_tab(self):
        previous = self.active_file
        original = self.window
        self.fresh_probe()
        target = self.active_file
        if self.window.handle != original.handle or not any(
                self.probe_stem(tab.window_text()) == previous.stem for tab in self.tabs()):
            raise IsolationError("Две пробные вкладки в одном окне не подтверждены.")
        candidates = [tab for tab in self.tabs() if self.probe_stem(tab.window_text()) == target.stem]
        if len(candidates) != 1 or self.wait(self.editor, 2) is None:
            raise IsolationError("Вкладка для восстановления не опознана однозначно.")
        if not self.close_test_tab(control_id(candidates[0]), target.stem):
            raise IsolationError("Закрытие пробной вкладки не подтверждено.")
        self.active_file = previous
        if not self.activate_probe():
            raise IsolationError("Исходная пробная вкладка не активна после закрытия.")
        self.check_active_file()
        send_keys("^+t")
        if not self.wait(lambda: self.probe_stem(self.window.window_text()) == target.stem
                         and any(self.probe_stem(tab.window_text()) == target.stem
                                 and tab.iface_selection_item.CurrentIsSelected for tab in self.tabs())
                         and self.editor(), 4):
            raise IsolationError("Имя, активная вкладка или текст восстановленного файла не подтверждены.")
        if not self.activate_probe():
            raise IsolationError("Восстановление подтверждено, но возврат к исходной вкладке не удался.")
        return "Закрытая пробная вкладка снова открылась; её имя, выделение и текст сверены."

    def check_goto_line(self, entry, strategy):
        editor = self.focus_probe_editor()
        existing = {control_id(control) for control in self.visible_edits()} | {control_id(editor)}
        send_keys("^{HOME}")
        if strategy == "goto_via_menu":
            self.edit_menu_action({"перейти", "перейти к строке", "go to", "go to line"})
            entry["checked_via"] = "interface"
        else:
            self.check_active_file()
            send_keys("^g")
            entry["checked_via"] = "shortcut"

        def line_field():
            dialog = self.foreground_dialog({"переход к строке", "перейти к строке", "go to line", "go to"})
            fields = self.visible_edits()
            if dialog:
                fields += dialog.descendants(control_type="Edit")
            fresh = {control_id(field): field for field in fields
                     if field.is_visible() and field.is_enabled() and control_id(field) not in existing}
            return next(iter(fresh.values())) if len(fresh) == 1 else None

        field = self.wait(line_field, 3)
        if field is None:
            raise RuntimeError("Поле номера строки не распознано.")
        self.focus_check()
        field.click_input()
        self.focus_check()
        if not field.element_info.element.CurrentHasKeyboardFocus:
            raise RuntimeError("Поле номера строки не получило фокус.")
        send_keys("{HOME}+{END}2", vk_packet=True)
        self.focus_check()
        if not field.element_info.element.CurrentHasKeyboardFocus or field.iface_value.CurrentValue != "2":
            raise RuntimeError("Поле номера строки не подтвердило число 2.")
        send_keys("{ENTER}")
        if not self.wait(lambda: editor.element_info.element.CurrentHasKeyboardFocus, 2):
            raise RuntimeError("Фокус не вернулся в пробный редактор после перехода.")
        self.check_active_file()
        send_keys("+{END}")
        expected = self.sample.splitlines()[1]
        if not self.wait(lambda: self.selection_is(editor, expected), 2):
            raise RuntimeError("Не подтверждено начало второй строки после перехода.")
        return "Проверено положение курсора в начале строки 2 по точному выделению текста."

    def check_search_option(self, operation, strategy):
        previous, original = self.active_file, self.window
        case_sensitive = operation == "find_case"
        sample = "Alpha\nalpha\n" if case_sensitive else "alpha\nконец\n"
        self.fresh_probe(text=sample)
        editor = self.wait(lambda: self.editor(sample), 2)
        if editor is None:
            raise RuntimeError("Пробный редактор для параметров поиска не подтверждён.")
        self.recovery_editor = editor
        self.initial_edits = {control_id(field) for field in self.visible_edits()}
        labels = SEARCH_OPTION_PROBES[operation]

        def controls(names):
            candidates = list(self.menu_items().values())
            for kind in ("CheckBox", "Button"):
                candidates.extend(self.window.descendants(control_type=kind))
            return {control_id(control): control for control in candidates
                    if control.is_visible() and control.is_enabled()
                    and (norm(control.window_text()) in names
                         or norm(control.element_info.automation_id or "") in names)}

        def option():
            self.check_active_file()
            matches = controls(labels)
            if not matches:
                settings = controls({"параметры поиска", "find options", "search options",
                                     "searchoptions", "findoptions", "findoptionsbutton", "findmenubutton"})
                if len(settings) == 1:
                    next(iter(settings.values())).click_input()
                    matches = self.wait(lambda: controls(labels), 1) or {}
            if len(matches) != 1:
                raise RuntimeError("Переключатель поиска не распознан однозначно.")
            return next(iter(matches.values()))

        def checked(control):
            _, state = menu_toggle_state(control)
            if state not in (0, 1):
                raise RuntimeError("Состояние переключателя поиска неопределённое.")
            return bool(state)

        def set_option(value):
            control = option()
            if checked(control) != value:
                self.check_active_file()
                control.click_input()
                if checked(option()) != value:
                    raise RuntimeError("Переключатель поиска не подтвердил новое состояние.")

        def open_find():
            if strategy == "find_via_menu":
                self.edit_menu_action({"найти", "find"})
            else:
                self.check_active_file()
                send_keys("^f")
            if not self.wait(lambda: choose_search_fields(
                    self.visible_edits(), control_id(editor), self.initial_edits, False), 2):
                raise RuntimeError("Панель поиска не подтверждена.")

        saved = None
        try:
            self.edit_keys(editor, "^{HOME}" if case_sensitive else "^{END}")
            open_find()
            saved = checked(option())
            if case_sensitive:
                set_option(False)
                self.find_or_replace(editor, "ALPHA", expected_matches=("Alpha", "alpha"), close_search=False)
                self.edit_keys(editor, "^{HOME}")
            set_option(True)
            self.find_or_replace(editor, "alpha", close_search=False)
        finally:
            try:
                if saved is not None:
                    set_option(saved)
                if self.owned(USER32.GetForegroundWindow()):
                    self.check_active_file()
                    send_keys("{ESC}")
                    if original.handle != self.window.handle:
                        original.set_focus()
                    self.window, self.active_file = original, previous
                    if not self.activate_probe():
                        raise IsolationError("Возврат к исходному пробному файлу не подтверждён.")
                else:
                    raise IsolationError("Пробное окно потеряло фокус.")
            except Exception as error:
                raise IsolationError(f"Восстановление исходного состояния поиска не подтверждено: {error}") from error
        return ("Без учёта регистра ALPHA нашло слово другого регистра, с учётом регистра выделено alpha."
                if case_sensitive else "Поиск от конца файла с обходом выделил alpha в начале.")

    def check_ctrl_n(self):
        source_editor = self.focus_probe_editor()
        editor_kind, _, editor_id = self.control_key(source_editor)
        editor_class = source_editor.element_info.class_name
        original = self.window
        original_stem = self.active_file.stem
        initial_tabs = self.tabs()
        before_tabs = {control_id(tab) for tab in initial_tabs}
        original_ids = {control_id(tab) for tab in initial_tabs
                        if self.probe_stem(tab.window_text()) == original_stem}
        before_windows = {window.handle for window in self.desktop.windows()}
        original_class = original.class_name()
        observation = "состояние UIA ещё не считано"

        def observe():
            nonlocal observation
            foreground = USER32.GetForegroundWindow()
            if foreground == original.handle:
                target = original
            elif foreground and foreground not in before_windows:
                target = next((window for window in self.notepad_windows()
                               if window.handle == foreground and window.class_name() == original_class), None)
                if target is None:
                    observation = "фокус в новом окне, которое не опознано как Блокнот"
                    return None
            else:
                observation = "фокус вне исходного окна, нового окна Блокнота в фокусе нет"
                return None

            try:
                tabs = {control_id(tab): tab for tab in target.descendants(control_type="TabItem")}
                added = tabs.keys() - before_tabs if target is original else tabs.keys()
                selected = [tab_id for tab_id, tab in tabs.items()
                            if tab.iface_selection_item.CurrentIsSelected]
                selected_id = selected[0] if len(selected) == 1 else None
                untitled = (selected_id is not None
                            and norm(tabs[selected_id].window_text()) in UNTITLED_TABS)
                probe_in_title = self.probe_stem(target.window_text()) == original_stem
                where = "исходное окно" if target is original else "новое окно Блокнота"
                observation = (f"{where}; TabItem {len(before_tabs)} → {len(tabs)}, "
                               f"добавлено {len(added)}, выбрано {len(selected)}, "
                               f"добавленная вкладка активна: {selected_id in added}, "
                               f"безымянная активна: {untitled}, "
                               f"исходный файл в заголовке: {probe_in_title}")
                if target is not original and len(tabs) == 1 and untitled:
                    return target, selected_id, True, "отдельное окно с одной безымянной вкладкой"
                if target is original and selected_id is not None and not probe_in_title:
                    if added == {selected_id} and before_tabs <= tabs.keys():
                        return target, selected_id, untitled, "новая активная вкладка в исходном окне"
                    if untitled and original_ids == {selected_id} and before_tabs == tabs.keys():
                        observation += "; прежняя вкладка отображается как безымянная"
            except (COMError, NoPatternInterfaceError) as error:
                observation = f"UIA временно не вернула состояние вкладок ({type(error).__name__})"
            return None

        self.check_active_file()
        send_keys("^n")
        result = self.wait(observe, 5)
        log(f"CTRL+N | {observation}")
        if result is None:
            raise IsolationError("Ctrl+N отправлен, но новый документ не подтверждён. Повтор отменён.")

        target, tab_id, untitled, description = result
        if target is not original and target.process_id() == original.process_id():
            self.show_test_window(target)
        # For an unfamiliar label, only the newly observed tab with a proven empty editor is safe.
        if not untitled:
            def empty_editor():
                try:
                    controls = [control for control in original.descendants(control_type=editor_kind)
                                if control.is_visible() and control.is_enabled()
                                and control.element_info.class_name == editor_class
                                and control.element_info.automation_id == editor_id]
                    return len(controls) == 1 and self.read_editor(controls[0]) == ""
                except (AttributeError, RuntimeError, OSError, COMError, NoPatternInterfaceError):
                    return False

            try:
                closed = self.close_test_tab(tab_id, None, confirm_empty=empty_editor)
            except (COMError, NoPatternInterfaceError, RuntimeError) as error:
                closed = False
                log(f"УБОРКА | Ctrl+N | {error}")
            if closed:
                log("УБОРКА | Новая пустая вкладка Ctrl+N закрыта по идентификатору UIA.")
            else:
                log("УБОРКА | Пустая вкладка Ctrl+N не подтверждена или не закрылась.")
        elif target is original or target.process_id() == original.process_id():
            self.probe_tab_ids.add((target.handle, tab_id))
        else:
            log("УБОРКА | Отдельное окно другого процесса оставлено открытым.")
        if target is not original and USER32.GetForegroundWindow() == target.handle:
            original.set_focus()
        if not self.owned(USER32.GetForegroundWindow()) or not self.activate_probe():
            raise IsolationError(f"Ctrl+N подтверждён ({description}), но возврат к пробному файлу не подтверждён.")
        return f"Ctrl+N: {description}; исходный пробный файл снова активен."

    def check_keyboard(self, action, strategy=None):
        if action in {"key_tab_next", "key_tab_previous"}:
            def tabs():
                return self.window.descendants(control_type="TabItem")

            if len(tabs()) < 2:
                new_tab = next((entry for entry in self.entries if entry["operation"] == "new_tab"), None)
                if new_tab is None:
                    raise RuntimeError("Не найдено действие создания второй вкладки.")
                before_ids = {control_id(tab) for tab in tabs()}
                self.command(new_tab)
                if not self.wait(lambda: len(tabs()) >= 2, 3):
                    raise RuntimeError("Не удалось подготовить две вкладки.")
                self.remember_created_tab(before_ids)
                if not self.activate_probe():
                    raise RuntimeError("Не удалось вернуться к пробной вкладке.")

            current = tabs()
            selected = [i for i, tab in enumerate(current) if tab.iface_selection_item.CurrentIsSelected]
            if len(selected) != 1:
                raise RuntimeError("UIA не определил единственную активную вкладку.")
            step = 1 if action == "key_tab_next" else -1
            expected = current[(selected[0] + step) % len(current)]
            expected_id = tuple(expected.element_info.element.GetRuntimeId())
            try:
                self.focus_check()
                send_keys("^{TAB}" if step == 1 else "^+{TAB}")
                if not self.wait(lambda: any(
                    tuple(tab.element_info.element.GetRuntimeId()) == expected_id
                    and tab.iface_selection_item.CurrentIsSelected for tab in tabs()), 3):
                    raise RuntimeError("Ожидаемая соседняя вкладка не стала активной.")
            finally:
                self.activate_probe()
            return "UIA подтвердил переключение на ожидаемую соседнюю вкладку."
        if action == "key_ctrl_n":
            return self.check_ctrl_n()
        editor = self.wait(self.editor, 2)
        if editor is None:
            raise RuntimeError("Пробный редактор недоступен для сверки через UIA.")
        if strategy == "select_last_character":
            self.edit_keys(editor, "^{HOME}^{END}+{LEFT}")
            ranges = editor.iface_text.GetSelection()
            if ranges.Length != 1 or ranges.GetElement(0).GetText(-1) not in {"\n", "\r", "\r\n"}:
                raise RuntimeError("Последний перенос строки не выделен после Ctrl+End.")
            return "После Ctrl+End выделен последний перенос строки."
        if strategy == "accept_word_separator":
            editor = self.focus_probe_editor()
            send_keys("^{HOME}^+{RIGHT}")
            time.sleep(0.2)
            if not self.wait(lambda: self.selection_is(editor, "Druzhishe")
                             or self.selection_is(editor, "Druzhishe "), 2):
                raise RuntimeError("Выделение слова не подтверждено.")
            return "Подтверждено выделение слова с допустимым пробелом."
        if action in SELECTION_PROBES:
            for keys, selected in SELECTION_PROBES[action]:
                self.edit_keys(editor, keys)
                if action == "key_select_word":
                    time.sleep(0.2)
                if not self.wait(lambda: self.selection_is(editor, selected), 2):
                    raise RuntimeError(f"UIA не подтвердил ожидаемое выделение после {keys}.")
            return "Положение курсора или выделенный текст подтверждены через UIA."
        self.recovery_editor = editor
        if action == "key_redo":
            editor = self.focus_probe_editor()
            self.recovery_editor = editor
            send_keys("^{END}x")
            expected = self.clean_text(self.sample + "x")
            time.sleep(0.2)
            if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == expected, 2):
                raise RuntimeError("Пробная правка перед отменой не подтверждена.")
            self.check_active_file()
            if strategy == "undo_via_menu":
                undo = next((entry for entry in self.entries if entry["operation"] == "undo"), None)
                if undo is None:
                    raise RuntimeError("Пункт «Отменить» не найден для подготовки теста Ctrl+Y.")
                self.command(undo)
            else:
                send_keys("^z")
            time.sleep(0.2)
            if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == self.clean_text(self.sample), 2):
                raise RuntimeError("Не подтверждена отмена перед проверкой повтора.")
            editor = self.focus_probe_editor()
            send_keys("^y")
            time.sleep(0.2)
        else:
            keys, expected = EDIT_PROBES[action]
            self.edit_keys(editor, keys)
        if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == expected, 2):
            raise RuntimeError("Текст редактора не совпал с ожидаемым после команды.")
        return "Текст редактора сверен. " + self.reset_after_verified(editor)

    def check(self, entry, strategy=None):
        if self.strategy_journal is None or verified_status(entry) == "panel_verified":
            return self._check(entry, strategy)
        return self.strategy_journal.execute(
            entry["operation"], strategy or "baseline", partial(self._check, entry, strategy),
            candidate=strategy is not None and strategy != "confirm_pdf",
        )

    def _check(self, entry, strategy=None):
        self.recovery_editor = None
        action = entry["operation"]
        if not self.menu_items().keys() >= self.main_menus:
            self.return_from_settings()
        if not self.activate_probe():
            if self.clean_windows:
                raise IsolationError("Пробная вкладка не активна. Оконная команда отменена.")
            self.fresh_probe()
        if action == "print_dialog":
            evidence, route = self.check_pdf_print(allow_confirmation=strategy == "confirm_pdf")
            entry["checked_via"] = route
            return evidence
        if action in CLIPBOARD_PROBES:
            return self.check_clipboard(entry, strategy)
        if action == "key_reopen_tab":
            return self.check_reopen_tab()
        if action == "key_goto_line":
            return self.check_goto_line(entry, strategy)
        if action in SEARCH_OPTION_PROBES:
            return self.check_search_option(action, strategy)
        if action.startswith("key_"):
            return self.check_keyboard(action, strategy)
        if action == "open_file" and strategy == "open_shortcut":
            self.check_active_file()
            send_keys("^o")
            return self.file_dialog(opening=True)
        if action in ZOOM.values():
            return self.check_zoom(entry)
        if action in {"word_wrap", "status_bar"}:
            return self.check_toggle(entry)
        if action == "markdown_view":
            return self.check_markdown_view(entry)
        if action in {"new_tab", "new_markdown", "close_tab"}:
            return self.check_tab(entry)
        if action in {"close_window", "exit_app", "save_all"}:
            if self.clean_windows:
                self.require_single_window_tab(self.active_file.stem)
            else:
                isolated_result = self.prepare_single_tab(action, entry)
                if isolated_result:
                    return isolated_result
        needs_editor = action in {
            "find_text", "replace_text", "select_all", "find_next", "find_previous",
            "save_file", "save_all", "undo", "delete", "date",
        }
        editor = self.wait(self.editor, 1) if needs_editor else None
        if needs_editor and editor is None:
            raise RuntimeError("Windows не предоставила исходный пробный текст для этой проверки.")
        if self.active_file.stem not in self.window.window_text():
            raise RuntimeError("Активный документ не совпадает с пробным файлом.")
        if editor is not None:
            self.edit_keys(editor, "^{HOME}")
        before_windows = {w.handle for w in self.desktop.windows(process=self.window.process_id())}
        if action in {"save_file", "save_all", "undo", "delete", "date", "replace_text"}:
            self.recovery_editor = editor
        if action == "undo":
            self.edit_keys(editor, "^{END}x")
            expected = self.clean_text(self.sample + "x")
            if not self.wait(lambda: self.clean_text(self.read_editor(editor)) == expected):
                raise RuntimeError("Не подтверждена вставка одного символа для проверки отмены.")
        elif action in {"save_file", "save_all"}:
            self.edit_keys(editor, "^{END}probe")
        elif action == "delete":
            select = next((e for e in self.entries if e["operation"] == "select_all"), None)
            if select is None:
                raise RuntimeError("Не найден пункт выделения всего текста.")
            self.command(select)
            if not self.wait(lambda: self.selection_is(editor, self.clean_text(self.sample)), 2):
                raise RuntimeError("Не подтверждено выделение пробного текста перед удалением.")
        before_ui = {e.window_text() for e in self.window.descendants() if e.is_visible()} if action in SURFACES else set()
        if self.clean_windows and action == "save_all":
            self.require_single_window_tab(self.active_file.stem)
        if action in SEARCH_TESTS:
            self.initial_edits = {control_id(edit) for edit in self.visible_edits()}
        self.command(entry)
        if action in {"save_copy", "open_file"}:
            return self.file_dialog(opening=action == "open_file")
        if action in {"find_text", "replace_text"}:
            return self.check_text(action, editor)
        if action == "select_all":
            if not self.wait(lambda: self.selection_is(editor, self.clean_text(self.sample))):
                raise RuntimeError("Выделение всего текста не подтверждено.")
            return "Выделен весь пробный текст"
        if action in {"find_next", "find_previous"}:
            if not self.wait(lambda: self.selection_is(editor, "alpha")):
                raise RuntimeError("Переход не выделил alpha. Сначала нужна успешная проверка «Найти».")
            return "После перехода выделено alpha"
        if action in {"save_file", "save_all"}:
            if not self.wait(lambda: self.active_file.read_text(encoding="utf-8-sig") == self.sample + "probe"):
                raise RuntimeError("Содержимое файла на диске не совпало с ожидаемым после записи.")
            if self.clean_windows:
                self.clean_saved_text = self.sample + "probe"
                return "Изменение записано на диск, весь текст сверен"
            return "Изменение записано на диск, весь текст сверен; " + self.reset_after_verified(editor, saved=True)
        if action == "undo":
            if not self.wait(self.editor):
                raise RuntimeError("Отмена не восстановила исходный текст.")
            return "Отмена восстановила исходный текст"
        if action in {"delete", "date"}:
            value = lambda: self.clean_text(self.read_editor(editor))
            expected = self.clean_text(self.sample)
            if not self.wait(lambda: value() == "" if action == "delete" else value().endswith(expected) and len(re.findall(r"\d+", value()[:-len(expected)])) >= 3):
                raise RuntimeError("Ожидаемое изменение текста не подтверждено.")
            result = "Весь выделенный текст удалён" if action == "delete" else "В начало пробного текста добавлена строка даты и времени"
            return result + "; " + self.reset_after_verified(editor)
        if action == "new_window":
            opened = self.wait(lambda: next((w for w in self.desktop.windows(process=self.window.process_id())
                                              if w.handle not in before_windows and w.handle == USER32.GetForegroundWindow()
                                              and w.class_name() == self.window.class_name()), None))
            if opened is None:
                raise RuntimeError("Новое окно Блокнота не обнаружено.")
            previous = self.window
            try:
                self.window = opened
                self.show_test_window(opened)
                tabs = self.wait(self.tabs, 2)
                if (not tabs or len(tabs) != 1
                        or norm(tabs[0].window_text()) not in UNTITLED_TABS):
                    return "Появилось отдельное окно Блокнота; принадлежность вкладки не подтверждена"
                tab_id = control_id(tabs[0])
                self.probe_tab_ids.add((opened.handle, tab_id))
                self.focus_check()
                opened.close()
                if self.wait(lambda: not USER32.IsWindow(opened.handle), 2):
                    self.probe_tab_ids.discard((opened.handle, tab_id))
                    return "Появилось отдельное окно Блокнота, пробное окно закрыто"
                return "Появилось отдельное окно Блокнота; пробная вкладка оставлена до финальной уборки"
            finally:
                self.window = previous
                self.window.set_focus()
        if action in {"close_window", "exit_app"}:
            closed_handle = self.window.handle
            if not self.wait(lambda: not USER32.IsWindow(closed_handle)):
                raise RuntimeError("Закрытие пробного окна не подтверждено.")
            self.probe_tab_ids = {key for key in self.probe_tab_ids if key[0] != closed_handle}
            if self.clean_windows:
                if self.notepad_windows():
                    raise IsolationError("Пробное окно закрылось, но обнаружено другое окно Блокнота.")
                return "Закрытие единственного пробного окна подтверждено"
            subprocess.Popen(["notepad.exe", str(self.active_file)])
            self.attach(self.active_file)
            return "Пробное окно закрылось, пробный файл открыт заново"
        if action in SURFACES:
            added = self.wait(lambda: ({e.window_text() for e in self.window.descendants() if e.is_visible()}
                                        | {w.window_text() for w in self.roots() if w.handle != self.window.handle})
                                       - before_ui - {""})
            if not added:
                raise RuntimeError("Появление новых элементов не подтверждено.")
            entry["observed_controls"] = sorted(added)
            for label in entry["observed_controls"]:
                log(f"ЭЛЕМЕНТ | {entry['name']} → {' '.join(label.split())}")
            self.focus_check()
            if norm(entry["name"]) in {"шрифт", "font"}:
                if not self.return_from_settings():
                    raise RuntimeError("Настройки шрифта открылись, но возврат в редактор не подтверждён.")
            else:
                send_keys("{ESC}{ESC}" if norm(entry["name"]) in {"масштаб", "zoom"} else "{ESC}")
            return "Появились элементы интерфейса: " + ", ".join(sorted(added)[:12])
        raise RuntimeError(f"Неизвестный сценарий: {action}")


def main(window_tests=False, print_only=False, search_only=False, keyboard_only=False, *,
         clipboard_only=False, retry_only=False):
    global REPORT
    retry_numbers = None
    if retry_only:
        source, retry_numbers = latest_retry_source(trainer.LOGS)
        if source is None:
            print("Завершённый отчёт не найден. Сначала запусти нужную группу, например B для буфера.", flush=True)
            return
        if not retry_numbers:
            print(f"В отчёте {source.name} нет проблемных проверок для повтора.", flush=True)
            return
    base_name = numbered_report_parts(REPORT.name)
    REPORT = reserve_report(REPORT.with_name(base_name[1]) if base_name else REPORT)
    REPORT.write_text("", encoding="utf-8-sig")
    log(f"ФАЙЛ ОТЧЁТА | {REPORT}")
    RECOVERY_DB.parent.mkdir(parents=True, exist_ok=True)
    if retry_only:
        log(f"ИСТОЧНИК ПОВТОРА | {source.name}")
    if clipboard_only:
        started = time.monotonic()
        try:
            with probe_clipboard(None):
                pass
        except CommandBlocked as error:
            log(f"[Прогон B] Заблокировано: {error}")
            entries = [dict(menu="Клавиатура", name=name, shortcut=shortcut, enabled=True,
                            operation=operation, status="blocked", evidence=str(error))
                       for name, shortcut, operation in CORE + EXTRA + MORE
                       if operation in CLIPBOARD_PROBES]
            finish_checks(entries, started)
            return
        log("[Прогон B] Буфер готов. Исходные данные сохраняются перед каждой командой.")
    write_skill([])
    if window_tests:
        log("РЕЖИМ | Чистый запуск: Сохранить всё, Закрыть окно, Выйти")
        require_closed_notepad()
    lab = Lab(clean_windows=window_tests, place_test_window=arrange_test_console(),
              verify_nonempty_clipboard=clipboard_only)
    try:
        try:
            build_id = strategy_build_id(lab.window.process_id())
        except (OSError, Win32Error, RuntimeError, KeyError, ValueError, TypeError) as error:
            build_id = None
            log(f"ЖУРНАЛ СТРАТЕГИЙ | Сборка не определена, доступна базовая проверка: {error}")
        lab.strategy_journal = skill_engine.StrategyJournal(RECOVERY_DB, build_id, log)
        if lab.strategy_journal.ready:
            log(f"ЖУРНАЛ СТРАТЕГИЙ | {build_id}")
        run_checks(lab, window_tests, print_only, search_only, keyboard_only,
                   clipboard_only=clipboard_only, retry_numbers=retry_numbers)
    finally:
        lab.cleanup()


def run_checks(lab, window_tests, print_only=False, search_only=False, keyboard_only=False, *,
               clipboard_only=False, retry_numbers=None):
    started = time.monotonic()
    retry_operations = ({MANIFEST_OPERATIONS[number] for number in retry_numbers}
                        if retry_numbers is not None else None)
    log(f"ПРОБНЫЙ ФАЙЛ | {lab.source}")
    mode = ("повтор проблемных проверок" if retry_numbers is not None else
            "проверка печати в PDF" if print_only else
            "проверка поиска и замены" if search_only else
            "проверка клавиатуры с памятью и Qwen" if keyboard_only else
            "проверка буфера с памятью и Qwen" if clipboard_only else
            "оконные проверки" if window_tests else "полный прогон с памятью и Qwen")
    log(f"АГЕНТЫ НАВЫКОВ | Агент Блокнот 0.4.21 | {mode}")
    entries = lab.scan()
    for name, shortcut, operation in CORE + EXTRA + MORE:
        if operation and (operation.startswith("key_") or operation == "print_dialog"
                          or operation in SEARCH_OPTION_PROBES):
            menu = ("Тест PDF" if operation == "print_dialog" else
                    "Параметры поиска" if operation in SEARCH_OPTION_PROBES else "Клавиатура")
            entries.append(dict(menu=menu,
                                name=name, shortcut=shortcut,
                                enabled=True, operation=operation, status="pending"))
    lab.entries = entries
    write_skill(entries)
    pending = [e for e in entries if e["status"] == "pending" and e["operation"] != "print_dialog"]
    if window_tests:
        pending = [e for e in pending if e["operation"] in WINDOW_TESTS]
        missing = WINDOW_TESTS - {e["operation"] for e in pending}
        if missing:
            log(f"ЧИСТЫЙ ЗАПУСК | В меню не найдены: {', '.join(sorted(missing))}")
    if print_only:
        pending = []
        log("ПЛАН | Файл → Печать. Ctrl+P используется только при недоступности пункта меню.")
    elif search_only:
        pending = [entry for entry in pending if entry["operation"] in SEARCH_TESTS]
        log("ПЛАН | Найти и выделить alpha, затем заменить оба вхождения и сверить весь текст.")
        missing = SEARCH_TESTS - {entry["operation"] for entry in pending}
        if missing:
            log(f"ПОИСК | В меню не найдены: {', '.join(sorted(missing))}")
    elif keyboard_only:
        pending = [entry for entry in pending if entry["operation"] in KEYBOARD_TESTS]
        log("ПЛАН | Ctrl+Y, Ctrl+Shift+Стрелка вправо, Ctrl+N. При сбое: память, Qwen и сверка.")
    elif clipboard_only:
        pending = [entry for entry in pending if entry["operation"] in CLIPBOARD_PROBES]
        log("ПЛАН | Вырезать, копировать, вставить, вставить обычным текстом.")
    elif retry_operations is not None:
        pending = [entry for entry in pending if entry["operation"] in retry_operations]
        log(f"ПЛАН | Проблемных проверок из отчёта: {len(retry_numbers)}.")
    else:
        log("ПЛАН | Проверка изоляции перед каждой оконной командой." if window_tests else
            "ПЛАН | Проверка → память → Qwen при отсутствии подходящего профиля → повторная сверка.")
    # Check closing and Save All before creating extra tabs.
    order = {"save_all": -5, "close_window": -4, "exit_app": -3,
             "save_copy": -2, "find_text": -1, "find_next": 1, "find_previous": 1,
             "save_file": 2, "close_tab": 4, "new_tab": 4, "new_markdown": 4, "new_window": 4,
             "key_ctrl_n": 4, "key_tab_next": 4, "key_tab_previous": 4}
    pending.sort(key=lambda entry: order.get(entry["operation"], 0))
    observe_new = [e for e in pending if keyboard_only and e["operation"] == "key_ctrl_n"]
    pending = [e for e in pending if e not in observe_new]
    deferred = []
    for retry, queue in ((False, pending), (True, deferred), (False, observe_new)):
        if retry and queue:
            log("ПОВТОР | Остальные проверки завершены. Разбираю сбои с памятью и Qwen.")
        for entry, options in (queue if retry else ((item, None) for item in queue)):
            if not window_tests and not lab.owned(USER32.GetForegroundWindow()):
                log("ОСТАНОВКА | Пробный Блокнот потерял фокус.")
                break
            log(f"{'ПОВТОРНАЯ ПРОВЕРКА' if retry else 'ПРОВЕРКА'} | {entry['menu']} → {entry['name']}")
            try:
                if window_tests and not USER32.IsWindow(lab.window.handle):
                    lab.start_clean_probe(resume_saved=True)
                if retry:
                    evidence = retry_probe(lab, entry, options)
                    if evidence is None:
                        entry["status"] = "unverified"
                        log("НЕ ПОДТВЕРЖДЕНО | " + entry["evidence"])
                        continue
                    entry["evidence"] = evidence
                else:
                    entry["evidence"] = lab.check(entry)
                entry["status"] = verified_status(entry)
                log("ПОДТВЕРЖДЕНО | " + entry["evidence"])
            except CommandBlocked as error:
                entry["status"] = "blocked"
                entry["evidence"] = str(error)
                log("ЗАБЛОКИРОВАНО | " + entry["evidence"])
            except Exception as error:
                options = {} if retry or window_tests or search_only or entry in observe_new else retry_options(entry, error)
                entry["status"] = "deferred" if options else "unverified"
                entry["evidence"] = str(error) or type(error).__name__
                if options:
                    entry["failure_type"] = type(error).__name__
                    entry["initial_failure"] = entry["evidence"]
                    deferred.append((entry, options))
                log(("ОТЛОЖЕНО" if options else "НЕ ПОДТВЕРЖДЕНО") + " | " + entry["evidence"])
                if not window_tests:
                    try:
                        lab.recover()
                    except Exception as recovery_error:
                        log(f"ВОССТАНОВЛЕНИЕ | {recovery_error or type(recovery_error).__name__}")
            finally:
                write_skill(entries)
        else:
            continue
        break
    pdf_entry = next(e for e in entries if e["operation"] == "print_dialog")
    if (not (window_tests or search_only or keyboard_only or clipboard_only)
            and (retry_operations is None or "print_dialog" in retry_operations)
            and lab.owned(USER32.GetForegroundWindow())):
        log("ПРОВЕРКА | Печать в PDF")
        try:
            pdf_entry["evidence"] = lab.check(pdf_entry, "confirm_pdf" if print_only else None)
            pdf_entry["status"] = "verified"
            log("ПОДТВЕРЖДЕНО | " + pdf_entry["evidence"])
        except Exception as error:
            pdf_entry["status"] = "unverified"
            pdf_entry["evidence"] = str(error) or type(error).__name__
            log("НЕ ПОДТВЕРЖДЕНО | " + pdf_entry["evidence"])
    for entry in entries:
        if entry["status"] == "pending":
            entry["status"] = "not_run"
            entry["evidence"] = ("Действие вне выбранного прогона"
                                 if (window_tests and entry["operation"] not in WINDOW_TESTS
                                     or print_only and entry["operation"] != "print_dialog"
                                     or search_only and entry["operation"] not in SEARCH_TESTS
                                     or keyboard_only and entry["operation"] not in KEYBOARD_TESTS
                                     or clipboard_only and entry["operation"] not in CLIPBOARD_PROBES
                                     or retry_operations is not None and entry["operation"] not in retry_operations)
                                 else "Проверка прервана до этого пункта")
        elif entry["status"] == "deferred":
            entry["status"] = "unverified"
            entry["evidence"] += " (до повторной проверки агент остановился)"
    finish_checks(entries, started, retry_numbers)


def finish_checks(entries, started, retry_numbers=None):
    write_skill(entries)
    commands = manifest_results(entries)
    health = skill_engine.command_health(command["outcome"] for command in commands)
    rate = health["health_rate"]
    log(f"ЗДОРОВЬЕ | SUCCESS: {health['SUCCESS']} | FAILURE: {health['FAILURE']} | "
        f"BLOCKED: {health['BLOCKED']} | Health Rate: "
        + (f"{rate:.1%}" if rate is not None else "нет выполненных проверок"))
    if retry_numbers is not None:
        resolved = {command["number"] for command in commands
                    if command["status"] in {"verified", "panel_verified"}}
        remaining = retry_numbers - resolved
        log(f"ПОВТОР | Подтверждено: {len(retry_numbers) - len(remaining)} | В очереди: {len(remaining)}")
        log("ОЧЕРЕДЬ ПОВТОРА | " + ",".join(f"{number:02d}" for number in sorted(remaining)))
    log(f"ИТОГ | пунктов: {len(entries)} | действий проверено: "
        f"{sum(e['status'] == 'verified' for e in entries)} | интерфейс: "
        f"{sum(e['status'] == 'panel_verified' for e in entries)} | пропущено: "
        f"{sum(e['status'] == 'skipped' for e in entries)} | обнаружено без проверки: "
        f"{sum(e['status'] == 'discovered' for e in entries)} | время: {time.monotonic() - started:.1f} с")
    for command in commands:
        log(f"МАНИФЕСТ | {command['number']:02d} | {command['name']} | {command['status']}")
    log(f"Отчёт: {REPORT}\nНавык: {SKILL}")


if __name__ == "__main__":
    try:
        window_tests = sys.argv[1:] == ["--clean-windows"]
        print_only = sys.argv[1:] == ["--print-only"]
        search_only = sys.argv[1:] == ["--search-only"]
        keyboard_only = sys.argv[1:] == ["--keyboard-only"]
        clipboard_only = sys.argv[1:] == ["--clipboard-only"]
        retry_only = sys.argv[1:] == ["--retry-only"]
        if window_tests:
            REPORT = trainer.LOGS / "Notepad_Window_Report.txt"
            SKILL = trainer.SKILLS_DIR / "Notepad_Window_Skill.json"
        elif print_only:
            REPORT = trainer.LOGS / "Notepad_PDF_Report.txt"
            SKILL = trainer.SKILLS_DIR / "Notepad_PDF_Skill.json"
        elif search_only:
            REPORT = trainer.LOGS / "Notepad_Search_Report.txt"
            SKILL = trainer.SKILLS_DIR / "Notepad_Search_Skill.json"
        elif keyboard_only:
            REPORT = trainer.LOGS / "Notepad_Keyboard_Report.txt"
            SKILL = trainer.SKILLS_DIR / "Notepad_Keyboard_Skill.json"
        elif clipboard_only:
            REPORT = trainer.LOGS / "Notepad_Clipboard_Report.txt"
            SKILL = trainer.SKILLS_DIR / "Notepad_Clipboard_Skill.json"
        elif retry_only:
            REPORT = RETRY_REPORT
            SKILL = trainer.SKILLS_DIR / "Notepad_Retry_Skill.json"
        main(window_tests, print_only, search_only, keyboard_only,
             clipboard_only=clipboard_only, retry_only=retry_only)
    except Exception as error:
        message = "ОШИБКА | " + str(error)
        if numbered_report_parts(REPORT.name):
            log(message)
        else:
            print(message, flush=True)
        traceback.print_exc()
        raise SystemExit(1)
