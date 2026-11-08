# -*- coding: utf-8 -*-
"""Isolated Windows Notepad skill smoke test; Python standard library only."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime

import skill_engine
from skill_engine import ask_profile
from report_files import reserve_report

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'Данные'
DATA = STATE / 'Тесты'
LOGS = STATE / 'Журналы'
MEMORY = STATE / 'Память'
SKILLS_DIR = STATE / 'Навыки'
TASKS = STATE / 'Задания'
TASK_FILE = TASKS / 'TASK.json'
NOTE_FILE = TASKS / 'TASK.txt'
OUTPUT = STATE / 'Готово'
EVENTS = LOGS / 'events.jsonl'
REPORT = LOGS / 'REPORT.txt'
SKILLS = SKILLS_DIR / 'skills_registry.json'


def task_path(path):
    legacy = ROOT / path.name
    return legacy if not path.exists() and legacy.is_file() else path


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


if os.name == 'nt':
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None
    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetConsoleTitleW.restype = wintypes.BOOL
    user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL

MONITOR_DEFAULTTONEAREST = 2
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

VK_CONTROL = 0x11
VK_S = 0x53
VK_A = 0x41
VK_W = 0x57
VK_END = 0x23
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_TAB = 0x09
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

class INPUT(ctypes.Structure):
    pass
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]
class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]
class U(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]
INPUT._fields_ = [('type', wintypes.DWORD), ('union', U)]

def event(stage, status, **data):
    rec = {'time': datetime.now().astimezone().isoformat(timespec='seconds'),
           'stage': stage, 'status': status, **data}
    with EVENTS.open('a', encoding='utf-8') as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f'[{stage}] {status} ' + (str(data.get('detail', ''))), flush=True)


def window_title(hwnd):
    size = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(size + 1)
    user32.GetWindowTextW(hwnd, buf, size + 1)
    return buf.value


def find_window(title_fragment):
    hits = []
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            title = window_title(hwnd)
            if title_fragment.casefold() in title.casefold():
                hits.append((hwnd, title))
        return True
    callback = EnumWindowsProc(cb)
    user32.EnumWindows(callback, 0)
    return hits


def find_console_window(series):
    # A classic console has a visible HWND. Windows Terminal may host it via ConPTY.
    hwnd = kernel32.GetConsoleWindow()
    if hwnd and user32.IsWindowVisible(hwnd):
        return hwnd
    title = f'Druzhishe Notepad - {series}'
    if kernel32.SetConsoleTitleW(title):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            matches = find_window(title)
            if len(matches) == 1:
                return matches[0][0]
            time.sleep(0.1)
    event('layout', 'partial', detail='Окно журнала не распознано. Блокнот будет справа.')
    return None


def split_work_area(rect):
    height = rect.bottom - rect.top
    half_width = (rect.right - rect.left) // 2
    middle = rect.left + half_width
    return ((rect.left, rect.top, half_width, height),
            (middle, rect.top, rect.right - middle, height))


def arrange_windows(console_hwnd, notepad_hwnd):
    # Use the monitor containing the launcher and stay clear of the taskbar.
    console_visible = console_hwnd and user32.IsWindowVisible(console_hwnd)
    monitor = user32.MonitorFromWindow(
        console_hwnd if console_visible else notepad_hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        event('layout', 'unavailable', detail='Не удалось определить рабочую область экрана.')
        return
    left, right = split_work_area(info.rcWork)
    for hwnd, bounds, label in (
        (console_hwnd if console_visible else None, left, 'журнал'),
        (notepad_hwnd, right, 'Блокнот'),
    ):
        if not hwnd:
            continue
        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if not user32.SetWindowPos(hwnd, None, *bounds, SWP_NOZORDER | SWP_NOACTIVATE):
            event('layout', 'partial', detail=f'Windows не переместила окно «{label}».')


def send_key(vk, ctrl=False):
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

def send_unicode(string, hwnd, delay, target):
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    utf16 = string.encode('utf-16-le')
    for i in range(0, len(utf16), 2):
        code = int.from_bytes(utf16[i:i+2], 'little')
        down = INPUT(type=1, union=U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        up = INPUT(type=1, union=U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        pair = (INPUT * 2)(down, up)
        if not is_active_test_window(hwnd, target):
            raise RuntimeError(f'Фокус потерян при вводе на позиции {i // 2}; операция отменена')
        if code in (10, 9):
            send_key(VK_RETURN if code == 10 else VK_TAB)
        elif user32.SendInput(2, pair, ctypes.sizeof(INPUT)) != 2:
            raise RuntimeError(f'SendInput не отправил символ на позиции {i // 2}')
        # Give Notepad a chance to consume each Unicode key before the next one.
        time.sleep(delay)

PROFILES = {
    'base': (0.025, 1.2),
    'slow_input': (0.055, 1.5),
    'longer_save_wait': (0.025, 2.5),
}
QWEN = os.environ.get('NOTEPAD_QWEN_MODEL', skill_engine.DEFAULT_QWEN)
MEMORY_TEST_SAVE_WAIT = 2.0


def recovery_memory(path, signature, *, verified=None, rejected_id=None, report=event):
    return skill_engine.recovery_memory(path, signature, verified=verified,
                                        rejected_id=rejected_id, report=report)

class VerificationMismatch(Exception):
    def __init__(self, metrics):
        super().__init__('Текст на диске отличается от ожидаемого')
        self.metrics = metrics


def read_saved(target, expected):
    deadline = time.monotonic() + 7
    actual = None
    while time.monotonic() < deadline:
        try:
            actual = target.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError):
            actual = None
        if actual == expected:
            break
        time.sleep(0.2)
    return actual


def mismatch_metrics(expected, actual):
    prefix = 0
    if actual is not None:
        while prefix < min(len(actual), len(expected)) and actual[prefix] == expected[prefix]:
            prefix += 1
    metrics = {'expected_len': len(expected), 'actual_len': len(actual) if actual is not None else None,
               'matched_prefix': prefix}
    if actual is not None and prefix == len(expected) and len(actual) == prefix + 1:
        metrics['extra_codepoint'] = ord(actual[prefix])
    return metrics


def is_active_test_window(hwnd, target):
    if user32.GetForegroundWindow() != hwnd:
        return False
    return (target.name.casefold() in window_title(hwnd).casefold()
            and user32.GetForegroundWindow() == hwnd)


def run_case(expected, profile, series_id, case_number, inject_fault=False,
             *, console_hwnd=None, keep_open=False, minimum_save_wait=None, write_note=False):
    run_id = uuid.uuid4().hex[:10]
    input_delay, save_delay = PROFILES[profile]
    target = DATA / f'NOTEPAD_SKILL_{run_id}.txt'
    with target.open('x', encoding='utf-8') as draft:
        if not write_note:
            placeholder = 'ТЕСТ: этот текст должен быть заменён.'
            draft.write(placeholder if placeholder != expected else '')
    event('task', 'started', id=run_id, series=series_id, case=case_number, profile=profile, file=str(target))
    proc = subprocess.Popen(['notepad.exe', str(target)])
    event('process', 'started', pid=proc.pid)
    title = ''
    hwnd = 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        matches = find_window(target.name)
        if len(matches) == 1:
            hwnd, title = matches[0]
            break
        if len(matches) > 1:
            raise RuntimeError('Найдено несколько окон с тестовым файлом; остановлено')
        time.sleep(0.3)
    if not hwnd:
        raise TimeoutError('Не удалось обнаружить окно по уникальному имени файла за 15 секунд')
    event('window', 'found', hwnd=hwnd, title=title)
    try:
        arrange_windows(console_hwnd, hwnd)
        if not user32.SetForegroundWindow(hwnd):
            raise RuntimeError('Windows не разрешила активировать тестовое окно')
        time.sleep(0.35)
        if not is_active_test_window(hwnd, target):
            raise RuntimeError('Фокус не на нужной вкладке; ввод отменён')
        send_key(VK_A, ctrl=True)
        time.sleep(0.1)
        event('input', 'typing', detail=f'Вводим {len(expected)} символов с контролем фокуса')
        entered = expected
        if inject_fault or (minimum_save_wait is not None and save_delay < minimum_save_wait):
            entered += '!'
            event('fault', 'injected', id=run_id, extra_codepoint=ord('!'),
                  detail='В пробную запись добавлен лишний символ !')
        send_unicode(entered, hwnd, input_delay, target)
        # Win11 Notepad has an asynchronous editor; Ctrl+S must not race text input.
        time.sleep(save_delay)
        if not is_active_test_window(hwnd, target):
            raise RuntimeError('Фокус переключился на другое окно или вкладку; сохранение отменено')
        send_key(VK_S, ctrl=True)
        time.sleep(0.7)
        event('input', 'sent_only', detail='Unicode-ввод завершён, выдержана пауза, Ctrl+S отправлен; проверяем файл')
        actual = read_saved(target, expected)
        if actual == expected + '\n':
            event('recovery', 'trailing_newline', id=run_id,
                  detail='Обнаружен один лишний перенос в конце. Проверяем исправление.')
            for key, ctrl in ((VK_END, True), (VK_BACK, False), (VK_S, True)):
                if not is_active_test_window(hwnd, target):
                    raise RuntimeError('Тестовая вкладка потеряла фокус; исправление отменено')
                send_key(key, ctrl=ctrl)
                time.sleep(0.2)
            actual = read_saved(target, expected)
        if actual != expected:
            metrics = mismatch_metrics(expected, actual)
            event('verification', 'mismatch', id=run_id, **metrics)
            raise VerificationMismatch(metrics)
        event('verification', 'verified', id=run_id, detail='Текст на диске совпал полностью')
        return {'status': 'verified', 'id': run_id, 'file': str(target), 'profile': profile}

    finally:
        if not keep_open:
            try:
                if not is_active_test_window(hwnd, target):
                    user32.SetForegroundWindow(hwnd)
                if not is_active_test_window(hwnd, target):
                    event('cleanup', 'left_open', id=run_id, detail='Фокус пробной вкладки не подтверждён.')
                else:
                    send_key(VK_W, ctrl=True)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and target.name.casefold() in window_title(hwnd).casefold():
                        time.sleep(0.1)
                    status = ('left_open' if target.name.casefold() in window_title(hwnd).casefold()
                              else 'closed')
                    event('cleanup', status, id=run_id)
            except (OSError, RuntimeError) as error:
                event('cleanup', 'left_open', id=run_id, detail=str(error))


def publish_note(draft, expected, series):
    data = draft.read_bytes()
    actual = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    if actual != expected:
        raise RuntimeError('Черновик изменился после сверки; готовый файл не создан')
    OUTPUT.mkdir(exist_ok=True)
    name = datetime.now().strftime('Zametka_%Y-%m-%d_%H-%M-%S_') + series + '.txt'
    destination = OUTPUT / name
    fd, temporary_name = tempfile.mkstemp(dir=OUTPUT, prefix='.draft-', suffix='.tmp')
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename fails if the destination already exists.
        temporary.rename(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def diagnose(model, mismatch, options):
    # Модель получает только числовые признаки ошибки и идентификаторы профилей.
    prompt = (
        'Windows Notepad UI automation saved mismatching text. Pick one allowed recovery profile. '
        'Profile values below are seconds. If metrics include minimum_save_wait_seconds, '
        'choose an allowed profile whose save_wait_seconds meets that minimum. '
        'If you cannot choose, return stop. Return only a JSON object with key profile. '
        + json.dumps({'metrics': mismatch, 'allowed': sorted(options) + ['stop'],
                      'profiles': {name: {'per_character_seconds': PROFILES[name][0],
                                          'save_wait_seconds': PROFILES[name][1]}
                                   for name in sorted(options)}})
    )
    return ask_profile(model, prompt, options, event)


def make_cases(seed):
    samples = [
        seed, 'Дружище ё й № 123', 'ТЕСТ ЁЖ и май', 'English text 123',
        'Смешанный текст Mix 123', 'А', '  пробелы  ', 'Точка, тире — и вопрос?',
        'Цена: 123 ₽', 'Кавычки «текст» и "text"', 'Скобки (тест) [1] {2}',
        'Знаки + - = % ^ & #', 'Обратная косая \\ и прямая /', 'Ещё один день',
        'hello world 001', 'Разделы: А / Б / В', 'Текст с удвоенным  пробелом',
        'Проверка №0007', 'ЯЮЭЪЬ, ёжик', 'Тест № 000 999', 'abc ABC 0123',
        'Короткая строка 21', 'Слова: раз-два-три', 'Русские буквы: ёйъь',
        'Длина 80: ' + 'а' * 70, 'Длина 120: ' + 'Ж' * 109,
        'Юникод: 😀 🐾', 'Сумма €10 и $20', 'Маркер: • ① →\nВторая строка\tколонка',
        ('Дружище ё й № 123 ' * 16)[:250],
    ]
    return samples


def normalize_task_text(text, source='TASK.json'):
    if not isinstance(text, str) or not text:
        raise ValueError(f'{source}: нужен непустой текст')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if len(text) > 1000:
        raise ValueError(f'{source}: в тексте {len(text)} символов, предел — 1000')
    for index, char in enumerate(text, 1):
        if (ord(char) < 32 and char not in '\n\t') or 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError(f'{source}: недопустимый символ U+{ord(char):04X} на позиции {index}')
    return text


def main(fault_test=False, memory_test=False, write_note=False):
    if sys.platform != 'win32':
        raise RuntimeError('Этот тест запускается только на Windows 11')
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    DATA.mkdir(parents=True, exist_ok=True)
    MEMORY.mkdir(parents=True, exist_ok=True)
    series = uuid.uuid4().hex[:10]
    if write_note:
        cases = [normalize_task_text(task_path(NOTE_FILE).read_text(encoding='utf-8-sig'), NOTE_FILE.name)]
        memory_path = MEMORY / 'recovery_memory.sqlite3'
    elif memory_test:
        cases = ['Дружище: проверка памяти восстановления. Ёж, № 123.']
        memory_path = MEMORY / 'recovery_memory_test.sqlite3'
    elif fault_test:
        cases = ['Дружище: проба восстановления. Ёж, № 123.']
        memory_path = None
    else:
        task = json.loads(task_path(TASK_FILE).read_text(encoding='utf-8-sig'))
        cases = make_cases(normalize_task_text(task.get('text')))
        memory_path = MEMORY / 'recovery_memory.sqlite3'
    console_hwnd = find_console_window(series)
    results = []
    profile = 'base'
    for number, expected in enumerate(cases, 1):
        options = set(PROFILES) - {profile}
        attempts = []
        record = {'case': number, 'attempts': attempts, 'status': 'failed'}
        results.append(record)
        event('series', 'case', series=series, case=number, detail=f'{number}/{len(cases)}')
        candidate = profile
        selected_by = None
        signature = None
        remembered_id = None
        while candidate:
            inject_fault = fault_test and not attempts
            try:
                result = run_case(expected, candidate, series, number, inject_fault=inject_fault,
                                  console_hwnd=console_hwnd,
                                  minimum_save_wait=MEMORY_TEST_SAVE_WAIT if memory_test else None,
                                  write_note=write_note)
                if inject_fault or (memory_test and not attempts):
                    attempts.append({'status': 'injection_not_detected', 'profile': candidate})
                    break
                if selected_by:
                    result['selected_by'] = selected_by
                if write_note:
                    draft = Path(result['file'])
                    destination = publish_note(draft, expected, series)
                    result.update(draft_file=str(draft), file=str(destination))
                    event('output', 'published', file=str(destination), detail=str(destination))
                if signature is not None and selected_by and remembered_id is None:
                    record['memory_saved'] = bool(recovery_memory(memory_path, signature, verified=result))
                    if record['memory_saved']:
                        event('memory', 'saved', profile=candidate, verification_id=result['id'],
                              detail=f'Профиль сохранён: {candidate}')
                attempts.append(result)
                record['status'] = 'verified'
                profile = candidate
                break
            except VerificationMismatch as exc:
                attempts.append({'status': 'mismatch', 'profile': candidate, **exc.metrics})
                if selected_by:
                    attempts[-1]['selected_by'] = selected_by
                if selected_by and remembered_id is None:
                    break
                if (inject_fault or (memory_test and not selected_by)) and exc.metrics.get('extra_codepoint') != ord('!'):
                    event('fault', 'unexpected_mismatch', detail='Расхождение отличается от заданного сбоя')
                    break
                if remembered_id is not None:
                    recovery_memory(memory_path, signature, rejected_id=remembered_id)
                    event('memory', 'rejected', profile=candidate,
                          detail=f'Повтор с профилем {candidate} не прошёл сверку')
                    options.remove(candidate)
                    remembered_id = None
                elif memory_path is not None:
                    signature = json.dumps({'version': 1, 'profile': candidate, **exc.metrics},
                                           sort_keys=True)
                    remembered = recovery_memory(memory_path, signature)
                    if remembered and remembered[0] in options:
                        candidate, remembered_id = remembered
                        selected_by = 'memory'
                        event('memory', 'hit', profile=candidate, verification_id=remembered_id,
                              detail=f'Повтор с сохранённым профилем: {candidate}')
                        continue
                    event('memory', 'miss', detail='Для этого сбоя нет сохранённого профиля')
                if not options:
                    break
                metrics = dict(exc.metrics)
                if memory_test:
                    metrics['minimum_save_wait_seconds'] = MEMORY_TEST_SAVE_WAIT
                proposed = diagnose(QWEN, metrics, options)
                if proposed not in options:
                    break
                candidate = proposed
                selected_by = QWEN
            except (TimeoutError, OSError, RuntimeError) as exc:
                attempts.append({'status': 'stopped', 'profile': candidate,
                                 'error_type': type(exc).__name__, 'error': str(exc)})
                event('series', 'stopped', series=series, case=number, detail=str(exc))
                return summarize(series, cases, results, profile, interrupted=True)
    return summarize(series, cases, results, profile)


def summarize(series, cases, results, profile, interrupted=False):
    passed = sum(result['status'] == 'verified' for result in results)
    first_try = sum(row['status'] == 'verified' and len(row['attempts']) == 1 for row in results)
    return {'status': 'verified' if passed == len(cases) else 'failed',
            'series': series, 'planned': len(cases), 'tested': len(results), 'verified': passed,
            'first_try': first_try, 'recovered': passed - first_try, 'interrupted': interrupted,
            'last_verified_profile': profile if passed else None, 'cases': results}

def finish(fault_test=False, memory_test=False, write_note=False):
    global EVENTS
    report = LOGS / 'RECOVERY_REPORT.txt' if fault_test else REPORT
    mode = 'normal'
    if write_note:
        report = LOGS / 'WORK_REPORT.txt'
        EVENTS = LOGS / 'work_events.jsonl'
        mode = 'write_note'
    elif memory_test:
        report = LOGS / 'MEMORY_REPORT.txt'
        EVENTS = LOGS / 'memory_events.jsonl'
        mode = 'memory_test'
    elif fault_test:
        EVENTS = LOGS / 'recovery_events.jsonl'
        mode = 'fault_recovery'
    report = reserve_report(report)
    result = {}
    try:
        result = main(fault_test=fault_test, memory_test=memory_test, write_note=write_note)
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)}
        event('test', 'failed', detail=str(exc))
    result['mode'] = mode
    title = ('АГЕНТ «БЛОКНОТ» — ЗАПИСЬ ЗАМЕТКИ' if write_note else
             'АВТОМАТИЧЕСКОЕ ОБУЧЕНИЕ АГЕНТА «БЛОКНОТ» — ПРОБНЫЙ ЗАПУСК')
    report.write_text(f'{title}\n'
                      f'Дата: {datetime.now().astimezone().isoformat(timespec="seconds")}\n'
                      f'Результат: {result["status"]}\n'
                      + json.dumps(result, ensure_ascii=False, indent=2) + '\n'
                      f'Версия: 0.3.9. Модель диагностики: {QWEN}.\n'
                      + 'После попыток закрываются опознанные тестовые вкладки.\n'
                      + 'Файлы тестов сохраняются.\n', encoding='utf-8')
    if mode == 'normal':
        skills = {'schema_version': 1, 'agent': 'notepad', 'skill': 'replace_and_save_text',
                  'status': 'candidate' if result['status']=='verified' else 'unverified',
                  'last_test': result}
        SKILLS.parent.mkdir(exist_ok=True)
        SKILLS.write_text(json.dumps(skills, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\nОТЧЁТ:', report, flush=True)
    print('Проверка:', result['status'], flush=True)
    if write_note and result['status'] == 'verified':
        print('ГОТОВЫЙ ФАЙЛ:', result['cases'][0]['attempts'][-1]['file'], flush=True)
    return 0 if result['status'] == 'verified' else 1

if __name__ == '__main__':
    sys.exit(finish(fault_test='--fault-test' in sys.argv[1:],
                    memory_test='--memory-test' in sys.argv[1:],
                    write_note='--write-note' in sys.argv[1:]))
