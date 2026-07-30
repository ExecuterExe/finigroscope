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


def looks_like_stats(data) -> bool:
    """Похоже ли на STATS_JSON базового прогона (а не на что-то ещё)."""
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    return isinstance(first, dict) and "num_players" in first and "win_rate_by_seat" in first


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
