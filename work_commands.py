"""Open, find or replace in an isolated copy using Notepad UI Automation."""
import json
from pathlib import Path
from uuid import uuid4

import explore
import trainer
from report_files import reserve_report

ROOT = Path(__file__).resolve().parent
REPORT = trainer.LOGS / "COMMAND_REPORT.txt"


def line(value, name, *, allow_empty=False):
    if (not isinstance(value, str) or (not value and not allow_empty)
            or len(value) > 200 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError(f"Поле {name} должно содержать одну строку до 200 символов.")
    return value


def run(command):
    action = command.get("action")
    if action not in ("open", "find", "replace"):
        raise ValueError("В COMMAND.json укажи action: open, find или replace.")
    source = ROOT / Path(command["file"])
    if not source.is_file() and command["file"] == "TASK.txt":
        source = trainer.task_path(trainer.NOTE_FILE)
    if source.suffix.lower() != ".txt":
        raise ValueError("Рабочие команды принимают текстовые файлы .txt в UTF-8.")
    sample = trainer.normalize_task_text(source.read_text(encoding="utf-8-sig"), source.name)
    needle = replacement = None
    if action != "open":
        needle = line(command.get("find"), "find")
        if needle not in sample:
            return {"status": "not_found", "action": action, "source": str(source)}
        if action == "replace":
            replacement = line(command.get("replace"), "replace", allow_empty=True)
            if replacement == needle:
                raise ValueError("Текст замены совпадает с текстом поиска.")
    lab = explore.Lab(sample)
    editor = lab.wait(lab.editor)
    if editor is None:
        raise RuntimeError("Не удалось подтвердить, что Блокнот открыл текст копии.")
    result = {"status": "verified", "action": action, "source": str(source), "copy": str(lab.source)}
    if action == "open":
        return result
    lab.initial_edits = {explore.control_id(edit) for edit in lab.visible_edits()}
    lab.edit_keys(editor, "^{HOME}^" + ("h" if action == "replace" else "f"))
    lab.find_or_replace(editor, needle, replacement)
    result["matches"] = sample.count(needle)
    if action == "replace":
        expected = sample.replace(needle, replacement)
        lab.check_active_file()
        lab.edit_keys(editor, "^s")
        if not lab.wait(lambda: lab.source.read_text(encoding="utf-8-sig") == expected, 7):
            raise RuntimeError("Запись в Блокноте не совпала с ожидаемым текстом.")
        result["file"] = str(trainer.publish_note(lab.source, expected, uuid4().hex[:10]))
    return result


if __name__ == "__main__":
    report_path = reserve_report(REPORT)
    try:
        command = json.loads(trainer.task_path(trainer.TASKS / "COMMAND.json").read_text(encoding="utf-8-sig"))
        result = run(command)
    except Exception as error:
        result = {"status": "failed", "error": str(error) or type(error).__name__}
    report = json.dumps(result, ensure_ascii=False, indent=2)
    report_path.write_text(report + "\n", encoding="utf-8")
    print(report)
    print("Отчёт:", report_path)
    raise SystemExit(0 if result["status"] == "verified" else 1)
