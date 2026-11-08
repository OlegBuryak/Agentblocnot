"""Replay the historical GetClipboardOwner fault without touching the clipboard."""
import os
from pathlib import Path

from repair_engine import run_repair
from report_files import reserve_report
from skill_engine import DEFAULT_QWEN

ROOT = Path(__file__).resolve().parent / "Данные"
SPEC = {
    "id": "notepad.get_clipboard_owner.historical.v1",
    "parameters": ["owner", "error"],
    "baseline": "owner == 0",
    "failure": "(0, 'GetClipboardOwner', 'No error message is available')",
    "contract": ("Inputs are integers. owner is zero when there is no clipboard owner. "
                 "error is the last error code, zero for no error. Return True only "
                 "when the owner is zero and the error code is nonzero. A zero owner "
                 "with zero error is valid, and a nonzero owner is always valid."),
}
# Cover every equality pattern among owner, error and zero. Include wide handles.
CASES = ((0, 0), (0, 5), (0, 1400), (0, 0xFFFFFFFF),
         (1, 0), (1, 5), (5, 5), (0x100000001, 0), (0x100000001, 1400))


def oracle(owner, error):
    if owner:
        return False
    return bool(error)


def accepts_input(owner, error):
    return 0 <= owner < 2**64 and 0 <= error < 2**32


def main():
    path = reserve_report(ROOT / "Журналы" / "Notepad_Repair_Lab.txt")
    with path.open("w", encoding="utf-8-sig") as output:
        def report(line):
            print(line, flush=True)
            print(line, file=output, flush=True)

        report("СТЕНД САМОВОССТАНОВЛЕНИЯ | Исторический сбой GetClipboardOwner")
        report("РЕЖИМ | Симуляция Win32. Рабочий Блокнот и буфер не используются.")
        report(f"ОТЧЁТ | {path}")
        try:
            verified = run_repair(
                spec=SPEC, oracle=oracle, cases=CASES, accepts_input=accepts_input,
                memory_dir=ROOT / "Память" / "Стенд_исправлений", report=report,
                model=os.environ.get("NOTEPAD_QWEN_MODEL") or DEFAULT_QWEN,
            )
        except OSError as error:
            report(f"ОСТАНОВКА | Ошибка файлов стенда: {error}")
            verified = False
        report("ИТОГ | " + ("verified_simulation" if verified else "unverified"))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
