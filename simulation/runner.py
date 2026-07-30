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

DEFAULT_TIMEOUT = 120
# Маркер, после которого движок печатает JSON. Держим синхронно с print_report.
JSON_MARKER = "JSON ДЛЯ ОЦЕНКИ БАЛАНСА"


def extract_stats(stdout: str):
    """Достаёт список конфигураций из вывода скелета.

    Сначала пробуем то, что идёт после маркера, — это авторитетный блок. Если
    маркер потерялся (модель могла тронуть print_report вопреки запрету), берём
    последний JSON-массив в выводе.
    """
    text = stdout or ""
    tail = text.split(JSON_MARKER, 1)[1] if JSON_MARKER in text else text

    for candidate in (
        re.search(r"(\[[\s\S]*\])\s*$", tail),
        re.search(r"(\[[\s\S]*\])", tail),
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
        return {"ok": True, "stats": stats, "stdout": stdout}
