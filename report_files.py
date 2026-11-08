"""Allocate unique, chronologically numbered user-facing reports."""
from contextlib import closing
from datetime import datetime
import re
import sqlite3

REPORT_NAME = re.compile(r"([0-9]+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+\.txt)")


def numbered_report_parts(name):
    match = REPORT_NAME.fullmatch(name)
    return (int(match[1]), match[2]) if match else None


def reserve_report(base):
    """Create a distinct report before the run starts and return its path."""
    folder = base.parent
    folder.mkdir(parents=True, exist_ok=True)
    memory = folder.parent / "Память"
    memory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(memory / "report_numbers.sqlite3", timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS report_sequence (id INTEGER PRIMARY KEY, last_number INTEGER)")
        row = db.execute("SELECT last_number FROM report_sequence WHERE id = 1").fetchone()
        existing = max((parts[0] for path in folder.iterdir()
                        if (parts := numbered_report_parts(path.name))), default=0)
        number = max(row[0] if row else 0, existing) + 1
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = folder / f"{number}_{stamp}_{base.name}"
        path.touch(exist_ok=False)
        db.execute("INSERT OR REPLACE INTO report_sequence VALUES (1, ?)", (number,))
    return path
