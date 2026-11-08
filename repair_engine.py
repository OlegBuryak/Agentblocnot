"""Generate and verify a bounded source patch for a pure predicate."""
import ast
import difflib
import hashlib
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from skill_engine import DEFAULT_QWEN


class CaseMismatch(ValueError):
    def __init__(self, label, inputs, expected, actual):
        self.case = {"inputs": inputs, "expected": expected, "actual": actual}
        super().__init__(f"{label}: {json.dumps(self.case)}")


def checked_expression(source, parameters):
    """Compile a return expression that can only compare integer inputs."""
    if not isinstance(source, str) or len(source) > 300 or len(source.splitlines()) != 1:
        raise ValueError("Ожидалось короткое выражение Python.")
    tree = ast.parse(source.strip(), mode="eval")
    nodes = list(ast.walk(tree))
    if len(nodes) > 40:
        raise ValueError("Выражение слишком сложное.")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
               ast.Compare, ast.Eq, ast.NotEq, ast.Name, ast.Load, ast.Constant)
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError(f"Недопустимая конструкция: {type(node).__name__}.")
        if isinstance(node, ast.Name) and node.id not in parameters:
            raise ValueError(f"Неизвестный параметр: {node.id}.")
        if isinstance(node, ast.Constant) and (type(node.value) is not int or node.value != 0):
            raise ValueError("В выражении разрешена только числовая константа 0.")
    return compile(tree, "<repair predicate>", "eval")


def expression_value(code, parameters, inputs):
    result = eval(code, {"__builtins__": {}}, dict(zip(parameters, inputs)))
    if type(result) is not bool:
        raise ValueError("Выражение должно возвращать True или False.")
    return result


def shadow_check_patch(patch_candidate, test_data, *, baseline, parameters):
    """Run approved predicate syntax in a worker limited to five seconds."""
    data = {"candidate": patch_candidate, "baseline": baseline,
            "parameters": parameters, "cases": test_data}
    try:
        worker = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--shadow-worker"],
            input=json.dumps(data), capture_output=True, text=True,
            encoding="utf-8", timeout=5, check=True,
        )
        result = json.loads(worker.stdout)
    except subprocess.TimeoutExpired:
        return False, "Теневой процесс превысил лимит 5 секунд."
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        return False, f"Теневой процесс не завершил проверку ({type(error).__name__})."
    if "reason" in result:
        return False, result["reason"]
    base_ms, patch_ms = result["baseline_ms"], result["candidate_ms"]
    ratio = patch_ms / base_ms
    timings = f"база {base_ms:.2f} мс, кандидат {patch_ms:.2f} мс на серию"
    if patch_ms > base_ms * 1.2:
        return False, f"Кандидат медленнее на {(ratio - 1):.0%} | {timings}."
    return True, f"Теневые тесты пройдены | {timings}."


def shadow_worker(data):
    parameters = data["parameters"]
    cases = data["cases"]
    if not 1 <= len(cases) <= 100:
        raise ValueError("Нужно от 1 до 100 контрольных случаев.")
    baseline = checked_expression(data["baseline"], parameters)
    candidate = checked_expression(data["candidate"], parameters)
    inputs = []
    for case in cases:
        values, expected = case["inputs"], case["expected"]
        if (len(values) != len(parameters) or not all(type(value) is int for value in values)
                or type(expected) is not bool):
            raise ValueError("Некорректные контрольные данные.")
        inputs.append(values)
        expression_value(baseline, parameters, values)
        actual = expression_value(candidate, parameters, values)
        if actual != expected:
            raise CaseMismatch("Теневой тест не пройден", dict(zip(parameters, values)),
                               expected, actual)

    def measure(code):
        started = time.perf_counter_ns()
        for _ in range(max(1, 9000 // len(inputs))):
            for values in inputs:
                expression_value(code, parameters, values)
        return (time.perf_counter_ns() - started) / 1e6

    base_times, patch_times = [], []
    for round_number in range(7):
        if round_number % 2:
            patch_times.append(measure(candidate))
            base_times.append(measure(baseline))
        else:
            base_times.append(measure(baseline))
            patch_times.append(measure(candidate))
    return {"baseline_ms": median(base_times), "candidate_ms": median(patch_times)}


def ask_patch(model, request):
    schema = {"type": "object", "additionalProperties": False,
              "required": ["expression"],
              "properties": {"expression": {"type": "string"}}}
    payload = {"model": model, "stream": False, "format": schema, "keep_alive": "2m",
               "options": {"temperature": 0, "num_predict": 256},
               "messages": [
                   {"role": "system", "content":
                    "Repair the Python return expression according to the contract. "
                    "Use only parameters, 0, ==, !=, and, or, not, and parentheses. "
                    "Use explicit comparisons so the result is always bool. "
                    "The verified_reproduction field shows a known baseline failure. "
                    "If rejections are present, correct the rejected expression. "
                    "Check your new expression against the expected result for each listed counterexample. "
                    "Return only JSON with expression."},
                   {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
               ]}
    http_request = Request("http://127.0.0.1:11434/api/chat",
                           data=json.dumps(payload).encode("utf-8"),
                           headers={"Content-Type": "application/json"})
    with urlopen(http_request, timeout=90) as response:
        answer = json.load(response)
    if answer.get("done") is False or answer.get("done_reason") == "length":
        raise ValueError("Ollama вернула незавершённый ответ.")
    return json.loads(answer["message"]["content"])


def validate_patch(expression, parameters, oracle, cases):
    """Check the candidate against the trusted cases, including the known failure."""
    candidate = checked_expression(expression, parameters)
    for case in cases:
        expected = oracle(*case)
        actual = expression_value(candidate, parameters, case)
        if actual != expected:
            raise CaseMismatch("Контрольный тест не пройден", dict(zip(parameters, case)),
                               expected, actual)
    return len(cases)


def run_repair(*, spec, oracle, cases, accepts_input, memory_dir, report,
               model=DEFAULT_QWEN, propose=ask_patch):
    """Try memory first, then give the model at most three proposals."""
    parameters = spec["parameters"]
    baseline = checked_expression(spec["baseline"], parameters)
    if any(len(case) != len(parameters) or not all(type(value) is int for value in case)
           or not accepts_input(*case) for case in cases):
        report("ОСТАНОВКА | Эталонный набор содержит недопустимый вход.")
        return False
    for case in cases:
        actual = expression_value(baseline, parameters, case)
        expected = oracle(*case)
        if actual != expected:
            reproduction = {"inputs": dict(zip(parameters, case)), "expected": expected, "actual": actual}
            break
    else:
        report("ОСТАНОВКА | Эталонный набор не воспроизводит исходный сбой.")
        return False
    report("ИСХОДНЫЙ СБОЙ | " + json.dumps(reproduction))
    signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()
    cache = memory_dir / (signature + ".json")

    def report_shadow(expression):
        test_data = [{"inputs": case, "expected": oracle(*case)} for case in cases]
        passed, detail = shadow_check_patch(expression, test_data, baseline=spec["baseline"],
                                            parameters=parameters)
        report(f"ТЕНЬ | {'ПРОЙДЕНО' if passed else 'ОТКЛОНЕНО'} | {detail}")

    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        count = validate_patch(saved["expression"], parameters, oracle, cases)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, SyntaxError, RecursionError, TypeError, KeyError) as error:
        report(f"ПАМЯТЬ | Кандидат отклонён при повторной проверке: {error}")
    else:
        report(f"ПАМЯТЬ | Правка повторно прошла {count} контрольных тестов, вызов Qwen пропущен.")
        report_shadow(saved["expression"])
        return True

    feedback = []
    for attempt in range(1, 4):
        report(f"QWEN | Попытка {attempt}/3 | {model}")
        started = time.monotonic()
        rejection = {"attempt": attempt}
        try:
            proposal = propose(model, {**spec, "verified_reproduction": reproduction,
                                       "rejections": feedback})
        except OSError as error:
            report(f"QWEN | Локальная модель недоступна: {error}")
            return False
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            reason = f"Ответ модели не разобран: {error}"
        else:
            try:
                if not isinstance(proposal, dict) or proposal.keys() != {"expression"}:
                    raise ValueError("Ответ должен содержать только expression.")
                count = validate_patch(proposal["expression"], parameters, oracle, cases)
            except (ValueError, SyntaxError, RecursionError) as error:
                reason = str(error)
                if isinstance(proposal, dict) and isinstance(proposal.get("expression"), str):
                    rejection["expression"] = proposal["expression"][:300]
                    report("ПРЕДЛОЖЕНИЕ | " + json.dumps(rejection["expression"]))
                if isinstance(error, CaseMismatch):
                    rejection["counterexample"] = error.case
            else:
                original = spec["baseline"]
                updated = proposal["expression"].strip()
                diff = "".join(difflib.unified_diff(
                    [f"    return {original}\n"], [f"    return {updated}\n"],
                    fromfile="baseline", tofile="candidate"))
                report(f"ПРОВЕРЕНО | {count} контрольных тестов, включая воспроизведённый сбой, "
                       f"за {time.monotonic() - started:.1f} с.")
                report(diff.rstrip())
                memory_dir.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix(f".{uuid4().hex}.tmp")
                try:
                    saved = {**proposal, "reproduce": reproduction["inputs"]}
                    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
                    temporary.replace(cache)
                finally:
                    temporary.unlink(missing_ok=True)
                report("ПАМЯТЬ | Проверенная правка сохранена для повторных запусков стенда.")
                report_shadow(updated)
                return True
        report(f"ОТКЛОНЕНО | {time.monotonic() - started:.1f} с | {reason}")
        feedback.append({**rejection, "reason": reason[:500]})
    report("ОСТАНОВКА | Три предложения не прошли проверку.")
    return False


if __name__ == "__main__" and sys.argv[1:] == ["--shadow-worker"]:
    try:
        outcome = shadow_worker(json.load(sys.stdin))
    except (ValueError, TypeError, SyntaxError, KeyError, RecursionError) as error:
        outcome = {"reason": str(error)}
    print(json.dumps(outcome))
