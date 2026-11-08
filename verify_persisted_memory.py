"""Two-process acceptance check using real disposable Notepad documents."""
import ctypes
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import skill_engine
import trainer
from report_files import reserve_report

TEXT = "Дружище проверяет память. Ёж, № 123."
REPORT_NAME = "MEMORY_ACCEPTANCE_REPORT.txt"
EXPECTED_MISMATCH = trainer.mismatch_metrics(TEXT, TEXT + "!")


def append_report(report, message):
    with report.open("a", encoding="utf-8") as output:
        output.write(message + "\n")
    print(message, flush=True)


def run_phase(phase, folder):
    report = folder / REPORT_NAME
    trainer.DATA = folder
    trainer.EVENTS = folder / "notepad_events.jsonl"
    trainer.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    console = trainer.find_console_window(folder.name)
    model_calls = 0

    def log(stage, status, **details):
        if stage in {"baseline", "memory", "diagnosis", "recovery"}:
            message = details.get("detail") or details.get("profile") or ""
            append_report(report, f"{phase} | {stage} | {status} | {message}")

    def choose(model, prompt, options, report_event):
        nonlocal model_calls
        if phase == "recall":
            raise RuntimeError("Попытка вызова Qwen во второй фазе заблокирована.")
        model_calls += 1
        return skill_engine.ask_profile(model, prompt, options, report_event)

    def action(profile):
        return trainer.run_case(
            TEXT, profile or "base", folder.name, 1 if phase == "learn" else 2,
            console_hwnd=console, minimum_save_wait=trainer.MEMORY_TEST_SAVE_WAIT,
        )

    def allowed(error):
        if isinstance(error, trainer.VerificationMismatch) and error.metrics == EXPECTED_MISMATCH:
            return {"longer_save_wait": "Repeat the safe Notepad write using the longer save wait."}
        return {}

    result = skill_engine.run_skill(
        agent="notepad_acceptance", skill="write_then_verify",
        action=action,
        verify=lambda record: Path(record["file"]).read_text(encoding="utf-8-sig") == TEXT,
        profiles_for_error=allowed, memory_path=folder / "isolated_memory.sqlite3",
        model=trainer.QWEN, report=log, choose=choose,
    )
    selected_by = (result.recovery or {}).get("source")
    expected_source = trainer.QWEN if phase == "learn" else "memory"
    expected_calls = 1 if phase == "learn" else 0
    passed = result.status == "verified" and selected_by == expected_source and model_calls == expected_calls
    if result.failure:
        append_report(report, f"{phase} | причина | {result.failure}")
    append_report(report, f"{phase} | итог | {'PASS' if passed else 'FAIL'} | "
                          f"source={selected_by} | model_calls={model_calls}")
    return 0 if passed else 1


def main():
    if sys.platform != "win32":
        print("Проверка Блокнота запускается на Windows.", flush=True)
        return 1
    latest = reserve_report(trainer.LOGS / REPORT_NAME)
    folder = Path(tempfile.mkdtemp(prefix="Memory_Check_", dir=trainer.LOGS))
    report = folder / REPORT_NAME
    append_report(report, "ДРУЖИЩЕ — ЧТЕНИЕ ПАМЯТИ БЛОКНОТА ПОСЛЕ ПЕРЕЗАПУСКА")
    exit_code = 0
    try:
        for phase in ("learn", "recall"):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), phase, str(folder)])
            if completed.returncode:
                exit_code = completed.returncode
                break
    except (OSError, KeyboardInterrupt) as error:
        append_report(report, f"Проверка прервана | {error or type(error).__name__}")
        exit_code = 1
    append_report(report, f"ОБЩИЙ ИТОГ | {'PASS' if exit_code == 0 else 'FAIL'}")
    temporary = folder / "latest-report.tmp"
    shutil.copyfile(report, temporary)
    temporary.replace(latest)
    print("Отчёт:", latest, flush=True)
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in {"learn", "recall"}:
        folder = Path(sys.argv[2])
        try:
            sys.exit(run_phase(sys.argv[1], folder))
        except Exception as error:
            append_report(folder / REPORT_NAME, f"{sys.argv[1]} | ошибка | {error or type(error).__name__}")
            sys.exit(1)
    sys.exit(main())
