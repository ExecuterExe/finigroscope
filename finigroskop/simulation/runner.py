"""Запуск сгенерированного скелета-симулятора и получение STATS_JSON.

Это единственное место в сервисе, где выполняется код, написанный языковой
моделью, поэтому здесь важны не удобства, а границы:

  • отдельный процесс (не exec в процессе Flask) — падение или бесконечный цикл
    в сгенерированном коде не уносит сервис;
  • жёсткий таймаут с убийством дерева процессов;
  • пустой рабочий каталог во временной папке — скрипту нечего случайно
    перезаписать рядом с собой;
  • урезанное окружение (без переменных с ключами API);
  • ЗАПУСК ТОЛЬКО ПО ЯВНОМУ ДЕЙСТВИЮ АВТОРА, никогда не автоматически.

Чего эти меры НЕ дают, и это надо понимать честно: это не настоящая песочница.
Процесс запускается тем же интерпретатором и с правами текущего пользователя —
он может читать файлы пользователя и ходить в сеть. Для полной изоляции нужен
контейнер или отдельная виртуальная машина. Поэтому в интерфейсе основной путь —
прогнать скелет самому (хоть на online-python.com, как и написано в шапке
шаблона) и вставить готовый JSON, а локальный запуск — осознанная опция.

Формат разбирается по хвосту вывода: движок печатает человекочитаемый отчёт, а
затем строку-маркер и сам JSON-список конфигураций (см. print_report в
simulation/templates/game_skeleton.py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_TIMEOUT = 300
# Маркеры блоков в выводе скелета. Держим синхронно с print_report шаблона v4:
# движок печатает СНАЧАЛА STATS_JSON (его читает «Оценщик статистик»), затем
# DIAG_JSON (его читает агент-диагност).
STATS_MARKER = "STATS_JSON"
DIAG_MARKER = "DIAG_JSON"
# Маркер шаблона v3 — на случай, если запускают старый скелет.
LEGACY_MARKER = "JSON ДЛЯ ОЦЕНКИ БАЛАНСА"


def extract_stats(stdout: str):
    """Достаёт STATS_JSON (список конфигураций) из вывода скелета.

    Порядок важен: сначала отрезаем всё, что идёт после DIAG_JSON, иначе
    «последний массив в выводе» окажется куском диагностики.
    """
    text = stdout or ""
    head = text.split(DIAG_MARKER, 1)[0] if DIAG_MARKER in text else text
    for marker in (STATS_MARKER, LEGACY_MARKER):
        if marker in head:
            head = head.split(marker, 1)[1]
            break

    for candidate in (
        re.search(r"(\[[\s\S]*\])\s*$", head),
        re.search(r"(\[[\s\S]*\])", head),
        re.search(r"(\[[\s\S]*\])", text),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
    return None


def extract_diag(stdout: str):
    """Достаёт DIAG_JSON — диагностический блок для агента-диагноста.

    Его нет у скелетов v3, и это НЕ ошибка: числовая ветка работает и без него,
    просто тесты методички, которым он нужен, честно уйдут в n/a. Поэтому
    отсутствие возвращается как None, а не как пустой словарь: пустой словарь
    выглядел бы как «померили и ничего не нашли».
    """
    text = stdout or ""
    if DIAG_MARKER not in text:
        return None
    tail = text.split(DIAG_MARKER, 1)[1]
    match = re.search(r"(\{[\s\S]*\})", tail)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and data.get("runs") is not None else None


def parse_pasted_output(raw: str) -> dict:
    """Разбирает то, что автор вставил руками после внешнего прогона.

    Принимает ВЕСЬ вывод консоли целиком, а не один аккуратно вырезанный массив.
    Причина простая: в выводе скелета два блока — STATS_JSON и DIAG_JSON, и
    просьба «скопируйте нужный кусок» ровно на этом и ломалась. Автор копировал
    первый массив, DIAG терялся, а диагност потом честно ставил половине тестов
    n/a: no_data — при том что данные у автора на экране были.

    Возвращает {"stats": [...], "diag": {...}|None, "error": str|None}.
    Отдельно распознаётся случай, когда вставили ТОЛЬКО диагностику: молчать об
    этом нельзя, иначе автор получит «это не похоже на JSON» и не поймёт, что
    скопировал не тот блок.
    """
    text = (raw or "").strip()
    if not text:
        return {"stats": None, "diag": None, "error": "Пустая вставка."}

    has_markers = any(m in text for m in (STATS_MARKER, DIAG_MARKER, LEGACY_MARKER))
    if has_markers:
        stats = extract_stats(text)
        diag = extract_diag(text)
        if stats is None:
            if DIAG_MARKER in text and STATS_MARKER not in text:
                return {"stats": None, "diag": None,
                        "error": "В тексте есть только DIAG_JSON — блок для диагноста. "
                                 "Нужен и STATS_JSON: скопируйте вывод скелета целиком, "
                                 "с самого начала."}
            return {"stats": None, "diag": None,
                    "error": "Маркер STATS_JSON нашёлся, но JSON после него не разобрался — "
                             "похоже, вывод скопирован не полностью."}
        return {"stats": stats, "diag": diag, "error": None}

    # Маркеров нет: возможно, вставили только сам массив статистики.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        stats = extract_stats(text)
        if stats is not None:
            return {"stats": stats, "diag": None, "error": None}
        return {"stats": None, "diag": None,
                "error": "Это не похоже ни на вывод скелета, ни на JSON. Скопируйте всё, "
                         "что напечатала программа, целиком."}

    if isinstance(data, dict) and data.get("runs") is not None:
        return {"stats": None, "diag": None,
                "error": "Вставлен блок DIAG_JSON (диагностика), а нужен STATS_JSON — "
                         "список конфигураций. Проще всего скопировать весь вывод целиком."}
    if not looks_like_stats(data):
        return {"stats": None, "diag": None,
                "error": "JSON разобран, но это не STATS_JSON базового прогона — нужен "
                         "список конфигураций с полями num_players и win_rate_by_seat."}
    return {"stats": data, "diag": None, "error": None}


def looks_like_stats(data) -> bool:
    """Похоже ли на STATS_JSON базового прогона (а не на что-то ещё)."""
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    return isinstance(first, dict) and "num_players" in first and "win_rate_by_seat" in first


def add_run_plan_entries(code: str, entries: list) -> tuple:
    """Дописывает прогоны в RUN_PLAN уже сгенерированного скелета.

    К Симуляционисту повторно не обращаемся: код готов, доп. прогон — это запись
    в план и перезапуск файла. Базовую запись (`id: "base"`) НЕ трогаем и не
    переставляем: на ней держится STATS_JSON для «Оценщика статистик».

    Возвращает (новый_код, добавленные_записи). Записи с уже занятым id
    пропускаются — иначе результаты двух разных прогонов слились бы под одним
    именем, и диагност сопоставил бы данные не с тем тестом.
    """
    if not (code or "").strip():
        raise ValueError("Пустой код скелета — некуда дописывать RUN_PLAN.")

    match = re.search(r"^RUN_PLAN\s*=\s*\[", code, re.M)
    if not match:
        raise ValueError("В скелете нет RUN_PLAN — вероятно, это шаблон v3 без "
                         "поддержки дополнительных прогонов.")

    # Ищем закрывающую скобку списка, считая вложенность.
    start = match.end() - 1
    depth = 0
    end = None
    for i in range(start, len(code)):
        if code[i] == "[":
            depth += 1
        elif code[i] == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise ValueError("RUN_PLAN не закрыт — код скелета повреждён.")

    body = code[start:end + 1]
    # Кавычки обоих видов: шаблон написан с двойными, а дописанные записи —
    # через repr() с одинарными. Искать только двойные значило бы не увидеть
    # собственную прошлую запись и добавить прогон повторно; тогда два разных
    # прогона слились бы под одним run_id, и диагност сопоставил бы данные
    # не с тем тестом.
    existing = set(re.findall(r"""["']id["']\s*:\s*["']([^"']+)["']""", body))

    added, lines = [], []
    for e in entries or []:
        rid = e.get("id")
        if not rid or rid in existing or rid == "base":
            continue
        existing.add(rid)
        added.append(e)
        # repr, а НЕ json.dumps: json пишет null/true/false, а это не Python.
        # Коварство в том, что `null` — синтаксически корректное имя, поэтому
        # ast.parse такой файл пропускает, и ошибка вылезает только при запуске.
        lines.append("    " + repr({
            "id": rid,
            "player_counts": e.get("player_counts") or None,
            "config": e.get("config") or {},
        }) + ",")

    if not added:
        return code, []

    marker = "\n    # --- дописано диагностом (доп. прогоны) ---\n"
    patched = code[:end] + marker + "\n".join(lines) + "\n" + code[end:]
    return patched, added


def run_plan_entry(request: dict) -> dict:
    """Переводит run_request диагноста в запись RUN_PLAN.

    Соответствие типов конфигу задано в ТЗ; `config` из заказа берём как есть,
    но для известных типов гарантируем обязательный ключ — модель может его
    забыть, и тогда прогон молча повторил бы базовый.
    """
    rtype = request.get("request_type")
    config = dict(request.get("config") or {})
    if rtype == "catchup_off":
        config.setdefault("disable_catchup", True)
    elif rtype == "persona_targeted":
        config.setdefault("persona", "expert")
    return {
        "id": request.get("run_id"),
        "player_counts": request.get("player_counts") or None,
        "config": config,
    }


def run_extra_runs(code: str, requests: list, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Исполняет заказанные диагностом прогоны: план → перезапуск → сбор DIAG_JSON.

    Возвращает {ok, diag, runs, added, stdout} либо {ok: False, error}.
    `runs` — только заказанные прогоны, отфильтрованные по run_id: диагносту
    нужны именно они, а весь DIAG_JSON он уже видел на проходе 1.
    """
    entries = [run_plan_entry(r) for r in (requests or []) if r.get("run_id")]
    if not entries:
        return {"ok": True, "diag": None, "runs": {}, "added": []}

    try:
        patched, added = add_run_plan_entries(code, entries)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not added:
        return {"ok": False, "error": "Ни один заказанный прогон не удалось добавить в "
                                      "RUN_PLAN (идентификаторы уже заняты)."}

    outcome = run_skeleton(patched, timeout=timeout)
    if not outcome.get("ok"):
        return outcome

    diag = outcome.get("diag")
    wanted = {e["id"] for e in added}
    runs = {}
    for r in ((diag or {}).get("runs") or []):
        if r.get("run_id") in wanted:
            runs.setdefault(r["run_id"], []).append(r)
    # id прогона обязан совпасть: заказ -> RUN_PLAN -> результат. Иначе на
    # проходе 2 агент не сопоставит данные с тестом.
    lost = sorted(wanted - set(runs))
    return {
        "ok": True, "diag": diag, "runs": runs, "added": added,
        "missing_runs": lost, "stdout": outcome.get("stdout"),
    }


def run_skeleton(code: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Выполняет код скелета в отдельном процессе и возвращает STATS_JSON.

    Возвращает {ok: True, stats, stdout} либо {ok: False, error, stdout}.
    Исключений наружу не бросает — вызывающему коду нужен результат, а не разбор
    падений чужого кода.
    """
    if not (code or "").strip():
        return {"ok": False, "error": "Код скелета пуст."}

    with tempfile.TemporaryDirectory(prefix="finigro-sim-") as workdir:
        script = os.path.join(workdir, "skeleton.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)

        # Окружение без секретов: сгенерированному коду незачем видеть ключи API.
        env = {k: v for k, v in os.environ.items()
               if not any(mark in k.upper() for mark in ("API_KEY", "TOKEN", "SECRET"))}
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            proc = subprocess.run(
                [sys.executable, "-I", script],   # -I: без sitecustomize и PYTHON*-путей
                cwd=workdir, env=env, timeout=timeout,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "error": f"Скелет не завершился за {timeout} с — вероятно, партия не сходится. "
                             "Уменьшите GAMES_PER_CONFIG или проверьте условие окончания."}
        except OSError as exc:
            return {"ok": False, "error": f"Не удалось запустить скелет: {exc}"}

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        if proc.returncode != 0:
            tail = stderr.strip().splitlines()[-6:]
            return {"ok": False,
                    "error": "Скелет упал с ошибкой:\n" + "\n".join(tail),
                    "stdout": stdout}

        stats = extract_stats(stdout)
        if stats is None:
            return {"ok": False,
                    "error": "Скелет отработал, но JSON со статистикой в выводе не найден.",
                    "stdout": stdout}
        if not looks_like_stats(stats):
            return {"ok": False,
                    "error": "В выводе найден JSON, но он не похож на STATS_JSON базового прогона "
                             "(нет полей num_players / win_rate_by_seat).",
                    "stdout": stdout}
        # diag может быть None — у скелетов v3 его нет вовсе. Это не ошибка:
        # числовая ветка работает, а тесты диагноста уйдут в честный n/a.
        return {"ok": True, "stats": stats, "diag": extract_diag(stdout), "stdout": stdout}
