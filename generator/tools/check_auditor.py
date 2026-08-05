# -*- coding: utf-8 -*-
"""Дымовой прогон аудитора на живой модели.

    python tools/check_auditor.py            # проход mechanics
    python tools/check_auditor.py story       # проход story
    python tools/check_auditor.py features    # проход features

Тратит деньги: один вызов модели на прогон. Логика кода проверяется тестами
без сети (python -m pytest tests/), этот скрипт нужен для другого — убедиться,
что промпт понятен модели и она отвечает по схеме.

Модуль подаётся заведомо дефектный: аудитор обязан найти конкретное нарушение,
а не просто вернуть валидный JSON.
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from agents import module_auditor as auditor   # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# какое нарушение аудитор обязан найти на каждом проходе
CASES = {
    "mechanics": ("mechanics_elimination.json", "elimination_respected"),
    "story": ("story_age_content.json", "age_content_safe"),
    "features": ("features_elimination_conflict.json", "elimination_catchup_consistent"),
}

STATUS_MARK = {"ok": "+", "concern": "~", "violation": "!", "n/a": "-"}


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "mechanics"
    if phase not in CASES:
        sys.exit("Известные проходы: %s" % ", ".join(CASES))

    filename, must_find = CASES[phase]
    fixture = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))

    print("Проход: %s" % phase)
    print("Модуль: %s" % fixture["name"])
    print("Аудитор обязан найти нарушение по пункту: %s\n" % must_find)

    try:
        result = auditor.audit_module(
            phase=phase,
            module=fixture["module"],
            params=fixture["params"],
            previous_modules=fixture.get("previous_modules"))
    except auditor.AuditError as error:
        print("Аудит не удался: %s" % error)
        return 1

    print("\nКарта проверки:")
    for row in result.map:
        print("  %s %-32s %s" % (STATUS_MARK.get(row["status"], "?"),
                                 row["item"], (row.get("note") or "")[:70]))

    if result.consistency.get("applicable"):
        print("\nСлой 2, конфликтов: %d" % len(result.consistency["conflicts"]))
        for conflict in result.consistency["conflicts"]:
            print("  с %s: %s" % (conflict["previous_module"],
                                  conflict["conflict"][:110]))

    print("\nНаходки:")
    for issue in result.issues:
        print("  [%s] %s — %s" % (issue["severity"], issue["checklist_item"],
                                  issue["explanation"][:100]))

    if result.anomalies:
        print("\nАномалии промпта:")
        for item in result.anomalies:
            print("  - %s" % item)

    print("\npassed=%s, попыток=%s, модель=%s, %.1f с"
          % (result.passed, result.attempts, result.model, result.duration))
    print("summary: %s" % result.summary)

    found = [row["item"] for row in result.map if row["status"] == "violation"]
    ok = must_find in found and result.passed is False
    print("\n%s аудитор %s нарушение «%s»"
          % ("OK  " if ok else "ПЛОХО", "нашёл" if ok else "НЕ нашёл", must_find))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
