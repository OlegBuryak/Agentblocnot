"""Reusable verification, recovery and profile memory for Druzhishe skills."""
from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import time
import urllib.request
from uuid import uuid4

DEFAULT_QWEN = "qwen2.5:14b"
MAX_RANKED_PROFILES = 3


def quiet_report(stage, status, **details):
    pass


def recovery_memory(path, signature, *, verified=None, rejected_id=None, report):
    if verified is None and not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS recovery ('
                       'signature TEXT PRIMARY KEY, profile TEXT NOT NULL, '
                       'verification_id TEXT NOT NULL)')
            if verified is not None:
                db.execute('INSERT OR REPLACE INTO recovery VALUES (?, ?, ?)',
                           (signature, verified['profile'], verified['id']))
                return True
            if rejected_id is not None:
                db.execute('DELETE FROM recovery WHERE signature = ? AND verification_id = ?',
                           (signature, rejected_id))
                return None
            return db.execute('SELECT profile, verification_id FROM recovery WHERE signature = ?',
                              (signature,)).fetchone()
    except sqlite3.Error as exc:
        report('memory', 'unavailable', detail=str(exc))
        return None


def valid_profile_ranking(plan, options):
    return (isinstance(plan, list) and len(plan) <= MAX_RANKED_PROFILES
            and all(isinstance(profile, str) and profile in options for profile in plan)
            and len(plan) == len(set(plan)))


def ask_profile(model, prompt, options, report, *, keep_alive=0, ranked=False):
    if ranked:
        key = 'profiles'
        field = {'type': 'array', 'maxItems': min(len(options), MAX_RANKED_PROFILES),
                 'items': {'type': 'string', 'enum': sorted(options)}}
        instruction = ('Rank up to three listed recovery profiles, most relevant first. '
                       'Include only distinct applicable and safe options. Return an empty list if none apply. '
                       'Return only an object with the key profiles.')
    else:
        key = 'profile'
        field = {'type': 'string', 'enum': sorted(options | {'stop'})}
        instruction = ('Choose one listed recovery profile using the failure and its preconditions. '
                       'When a listed alternate is safe and directly addresses the failure, choose it. '
                       'Choose stop if no listed option is applicable or safe. '
                       'Return only an object with the key profile.')
    schema = {'type': 'object', 'required': [key], 'additionalProperties': False,
              'properties': {key: field}}
    payload = {'model': model, 'messages': [
                   {'role': 'system', 'content': instruction},
                   {'role': 'user', 'content': prompt}],
               'stream': False, 'format': schema, 'keep_alive': keep_alive,
               'options': {'temperature': 0, 'num_predict': 256}}
    request = urllib.request.Request('http://127.0.0.1:11434/api/chat',
                                     data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    report('diagnosis', 'started', model=model, detail=f'Ожидаем ответ {model} до 90 секунд')
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            answer = json.load(response)
        done_reason = answer.get('done_reason')
        if answer.get('done') is False or done_reason == 'length':
            raise ValueError(f'Незавершённый ответ Ollama ({done_reason or "done=false"})')
        decision = json.loads(answer['message']['content'])
        if not isinstance(decision, dict) or decision.keys() != {key}:
            raise ValueError(f'Ответ модели должен содержать только ключ {key}')
        result = decision[key]
        if ranked:
            if not valid_profile_ranking(result, options):
                raise ValueError('Модель вернула недопустимый список способов')
            profile = result[0] if result else 'stop'
            extra = {'profiles': result}
        else:
            if not isinstance(result, str) or result not in options | {'stop'}:
                raise ValueError('Модель вернула вариант вне разрешённого списка')
            profile, extra = result, {}
        report('diagnosis', 'declined' if profile == 'stop' else 'received',
              model=model, profile=profile, done_reason=done_reason,
              seconds=round(time.monotonic() - start, 2), load_ns=answer.get('load_duration'), **extra)
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report('diagnosis', 'unavailable', model=model, error_type=type(exc).__name__,
              detail=str(exc), seconds=round(time.monotonic() - start, 2))
        return None



@dataclass
class Outcome:
    status: str = "unverified"
    evidence: object = None
    failure: str | None = None
    recovery: dict | None = None
    attempts: list = field(default_factory=list)


class CommandBlocked(RuntimeError):
    """A prerequisite prevented the command from being dispatched."""


COMMAND_OUTCOMES = {"verified": "SUCCESS", "unverified": "FAILURE", "blocked": "BLOCKED"}


def command_health(outcomes, *, executed_limit=None):
    """Summarize comparable outcomes supplied in chronological order."""
    if executed_limit is not None:
        outcomes = reversed(list(outcomes))
    counts = dict.fromkeys(COMMAND_OUTCOMES.values(), 0)
    for outcome in outcomes:
        if outcome in counts:
            counts[outcome] += 1
        if executed_limit is not None and counts["SUCCESS"] + counts["FAILURE"] == executed_limit:
            break
    executed = counts["SUCCESS"] + counts["FAILURE"]
    return {**counts, "health_rate": counts["SUCCESS"] / executed if executed else None}


def rollback_needed(comparable_outcomes):
    """Signal rollback only when five executed outcomes give a rate below 80%."""
    health = command_health(comparable_outcomes, executed_limit=5)
    return health["SUCCESS"] + health["FAILURE"] == 5 and health["health_rate"] < 0.8


class StrategyJournal:
    """Store attempt outcomes and quarantine recovery strategies by build."""

    def __init__(self, path, build_id, log):
        self.path = path
        self.build_id = build_id
        self.log = log
        self.ready = False
        if build_id is None:
            return
        try:
            with closing(sqlite3.connect(path, timeout=2)) as db, db:
                db.execute("""CREATE TABLE IF NOT EXISTS strategy_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    operation TEXT NOT NULL, build_id TEXT NOT NULL, strategy TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('SUCCESS', 'FAILURE', 'BLOCKED'))
                )""")
                db.execute("""CREATE INDEX IF NOT EXISTS strategy_history_scope
                    ON strategy_history(operation, build_id, strategy, id DESC)
                    WHERE status IN ('SUCCESS', 'FAILURE')""")
                db.execute("""CREATE TABLE IF NOT EXISTS strategy_quarantine (
                    operation TEXT NOT NULL, build_id TEXT NOT NULL, strategy TEXT NOT NULL,
                    PRIMARY KEY(operation, build_id, strategy)
                )""")
        except sqlite3.Error as error:
            self.log(f"ЖУРНАЛ СТРАТЕГИЙ | SQLite недоступна: {error}")
        else:
            self.ready = True

    def allowed(self, operation, profiles):
        if not profiles or not self.ready:
            return {}
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as db:
                disabled = {row[0] for row in db.execute(
                    "SELECT strategy FROM strategy_quarantine WHERE operation = ? AND build_id = ?",
                    (operation, self.build_id),
                )}
        except sqlite3.Error as error:
            self.log(f"ЖУРНАЛ СТРАТЕГИЙ | Не удалось проверить блокировки: {error}")
            return {}
        for strategy in profiles.keys() & disabled:
            self.log(f"ОТКАТ | {operation} | {strategy} пропущена.")
        return {name: value for name, value in profiles.items() if name not in disabled}

    def execute(self, operation, strategy, action, *, candidate):
        if candidate and not self.allowed(operation, {strategy: True}):
            raise CommandBlocked("Стратегия отключена или журнал блокировок недоступен.")
        status = None
        try:
            value = action()
            status = "SUCCESS"
            return value
        except CommandBlocked:
            status = "BLOCKED"
            raise
        except Exception:
            status = "FAILURE"
            raise
        finally:
            if status and self.ready:
                self.record(operation, strategy, status, candidate=candidate)

    def record(self, operation, strategy, status, *, candidate):
        scope = (operation, self.build_id, strategy)
        quarantined = False
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                db.execute("""INSERT INTO strategy_history(operation, build_id, strategy, status)
                              VALUES (?, ?, ?, ?)""", (*scope, status))
                if candidate and status != "BLOCKED":
                    rows = db.execute("""SELECT status FROM strategy_history
                        WHERE operation = ? AND build_id = ? AND strategy = ?
                        AND status IN ('SUCCESS', 'FAILURE')
                        ORDER BY id DESC LIMIT 5""", scope).fetchall()
                    if rollback_needed([row[0] for row in reversed(rows)]):
                        quarantined = bool(db.execute("""INSERT OR IGNORE INTO strategy_quarantine
                            (operation, build_id, strategy) VALUES (?, ?, ?)""", scope).rowcount)
        except sqlite3.Error as error:
            self.ready = False
            self.log(f"ЖУРНАЛ СТРАТЕГИЙ | Не удалось сохранить исход: {error}")
        else:
            if quarantined:
                successes = sum(row[0] == "SUCCESS" for row in rows)
                self.log(f"ОТКАТ | {operation} | {strategy} отключена ({successes}/5 успешно).")


class VerificationError(RuntimeError):
    """The action ran, but independent verification failed."""


def recover_skill(*, memory_path, fingerprint, context, profiles, retry,
                  model=DEFAULT_QWEN, report=quiet_report, choose=ask_profile,
                  profiles_after_error=None, rank=None, max_attempts=2):
    """Try distinct profiles, consulting memory first.

    retry(profile) must return (status, evidence). Only 'verified' is learned.
    rank(model, prompt, options, report) may return an ordered list, [] to
    stop, or None if unavailable. Without rank, choose returns one profile.
    Every subsequent attempt requires approval from profiles_after_error.
    """
    if not 1 <= max_attempts <= MAX_RANKED_PROFILES:
        raise ValueError("max_attempts must be between 1 and 3")
    available = {key: value for key, value in profiles.items() if key != "stop"}
    result = Outcome()
    if not available:
        return result
    signature = json.dumps(fingerprint, sort_keys=True)
    memory_path = Path(memory_path)
    saved = recovery_memory(memory_path, signature, report=report)
    if saved and saved[0] in available:
        profile, saved_id = saved
        source = "memory"
        report("memory", "hit", profile=profile)
    else:
        profile, saved_id = None, None
        report("memory", "miss")

    latest_failure = None
    ranked_queue = []
    while available:
        if saved_id is None:
            source = model
            if ranked_queue:
                profile = ranked_queue.pop(0)
            else:
                prompt = json.dumps({"failure": context, "previous_attempt": latest_failure,
                                     "allowed": available if rank else {**available, "stop": "Stop"}},
                                    ensure_ascii=False)
                if rank:
                    decision = rank(model, prompt, set(available), report)
                    if valid_profile_ranking(decision, available):
                        ranked_queue = decision.copy()
                        profile = ranked_queue.pop(0) if ranked_queue else 'stop'
                    else:
                        profile = None
                else:
                    prompt = ("Choose an allowed recovery profile for a skill. Return JSON with the key profile. "
                              "Return stop if the action should not be retried. " + prompt)
                    profile = choose(model, prompt, set(available), report)
            if not isinstance(profile, str) or profile not in available:
                stopped = profile == "stop"
                result.recovery = {"source": source, "profile": "stop" if stopped else None,
                                   "status": "declined" if stopped else "unavailable"}
                return result

        attempt = {"source": source, "profile": profile}
        result.attempts.append(attempt)
        result.recovery = attempt
        report("recovery", "started", **attempt)
        try:
            status, evidence = retry(profile)
            if status not in {"verified", "panel_verified"}:
                raise VerificationError(str(evidence) or "Результат не подтверждён.")
        except CommandBlocked as error:
            detail = str(error)
            result.status, result.evidence = "blocked", detail
            attempt.update(status="blocked", reason=detail)
            report("recovery", "blocked", source=source, profile=profile, detail=detail)
            return result
        except Exception as error:
            result.failure = str(error) or type(error).__name__
            attempt.update(status="unverified", failure=result.failure)
            report("recovery", "unverified", source=source, profile=profile, detail=result.failure)
            if saved_id is not None:
                recovery_memory(memory_path, signature, rejected_id=saved_id, report=report)
                report("memory", "rejected", profile=profile)
            del available[profile]
            if len(result.attempts) >= max_attempts or profiles_after_error is None or not available:
                return result
            safe = profiles_after_error(error)
            available = {name: safe[name] for name in available if name in safe}
            ranked_queue = [name for name in ranked_queue if name in available]
            latest_failure = {"profile": profile, "type": type(error).__name__,
                              "detail": result.failure}
            saved_id = None
        else:
            attempt["status"] = status
            result.status, result.evidence, result.failure = status, evidence, None
            report("recovery", status, source=source, profile=profile)
            if saved_id is None and status == "verified":
                if recovery_memory(memory_path, signature,
                                   verified={"profile": profile, "id": uuid4().hex}, report=report):
                    report("memory", "saved", profile=profile)
            return result
    return result


def run_skill(*, agent, skill, action, verify, profiles_for_error, memory_path,
              model=DEFAULT_QWEN, report=quiet_report, choose=ask_profile, rank=None, max_attempts=2):
    """Execute action(profile) and independently verify(result).

    action(None) is the baseline. profiles_for_error(error) returns permitted
    profile IDs and their descriptions. Return an empty mapping if retrying
    would risk duplicating a side effect. verify must return True on success.
    rank and max_attempts opt into ranked recovery as in recover_skill.
    """
    def checked(profile):
        value = action(profile)
        if verify(value) is not True:
            raise VerificationError("Результат не подтверждён.")
        return "verified", value

    try:
        status, value = checked(None)
    except CommandBlocked as error:
        detail = str(error)
        report("baseline", "blocked", detail=detail)
        return Outcome(status="blocked", evidence=detail)
    except Exception as error:
        failure = str(error) or type(error).__name__
        report("baseline", "unverified", detail=failure)
        context = {"agent": agent, "skill": skill, "failure_type": type(error).__name__}
        allowed = profiles_for_error(error)
        result = recover_skill(
            memory_path=memory_path, fingerprint={**context, "reason": failure, "version": 1},
            context={**context, "detail": failure}, profiles=allowed, retry=checked,
            model=model, report=report, choose=choose, profiles_after_error=profiles_for_error,
            rank=rank, max_attempts=max_attempts,
        )
        if result.status == "blocked":
            result.status, result.evidence = "unverified", None
        if result.status == "unverified" and result.failure is None:
            result.failure = failure
        return result
    report("baseline", status)
    return Outcome(status=status, evidence=value)
