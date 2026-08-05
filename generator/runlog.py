# -*- coding: utf-8 -*-
"""Журнал вызовов модели.

Одна строка JSON на вызов в logs/llm_calls.jsonl плюс короткая строка в
консоль. Файл закрыт .gitignore: в нём видно параметры прогонов, а иногда и
куски пользовательских ответов.

JSONL, а не обычный лог: строки разбираются любым скриптом без парсинга
текста, а дописывание в конец не требует перечитывать файл.
"""

import json
import os
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "llm_calls.jsonl"

# на всякий случай: журнал не должен незаметно съесть диск
MAX_BYTES = 20 * 1024 * 1024

_enabled = os.environ.get("LLM_LOG", "1") != "0"


def new_run_id():
    """Идентификатор прогона: по нему собираются все проходы одной генерации."""
    return "run-%d" % int(time.time() * 1000)


def record(event, console=True):
    """Пишет событие в журнал. Никогда не роняет вызывающий код: журнал —
    вспомогательная вещь, из-за него терять результат генерации нельзя."""
    entry = dict(event)
    entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))

    if console:
        print(_line(entry))

    if not _enabled:
        return entry

    try:
        LOG_DIR.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            LOG_FILE.rename(LOG_FILE.with_suffix(".jsonl.old"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print("журнал недоступен (%s), продолжаю без записи" % e)

    return entry


def _line(entry):
    """Человекочитаемая однострочная сводка события."""
    parts = ["[%s]" % entry.get("agent", "llm")]
    for key in ("run_id", "phase", "attempt"):
        if entry.get(key) is not None:
            parts.append("%s=%s" % (key, entry[key]))
    if entry.get("duration") is not None:
        parts.append("%.1fs" % entry["duration"])
    if entry.get("tokens_in") is not None:
        parts.append("токены %s/%s" % (entry.get("tokens_in"), entry.get("tokens_out")))
    if entry.get("passed") is not None:
        parts.append("passed=%s" % entry["passed"])
    if entry.get("violations") is not None:
        parts.append("violation=%s concern=%s"
                     % (entry.get("violations"), entry.get("concerns")))
    if entry.get("error"):
        parts.append("ошибка: %s" % entry["error"])
    return " ".join(parts)
