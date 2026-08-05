# -*- coding: utf-8 -*-
"""Перезапуск сервера при правке кода — только для разработки.

Зачем. У ФинИгроСкопа автоперезагрузка есть (Flask), у генератора не было, и это
стоило дороже, чем выглядит. Правишь маршрут, обновляешь страницу — а в процессе
живёт код десятиминутной давности. Страница при этом НЕ пустая: она уже новая,
потому что браузер перечитал app.js. Получается пара «новая страница + старый
сервер», и её признак — ответ «неизвестный адрес» на маршрут, который в коде
есть. Тратится на такую загадку не минута.

Как. Родительский процесс ничего не обслуживает: он запускает сам себя дочерним
процессом и следит за временем правки .py-файлов. Изменился хоть один — дочерний
процесс останавливается и запускается заново. Ровно та же схема, что у Werkzeug,
только без зависимости: проект намеренно живёт на стандартной библиотеке.

Следим ТОЛЬКО за .py. Правка app.js, index.html или styles.css сервер не
перезапускает: он их не импортирует, а отдаёт с диска при каждом запросе — там
достаточно обновить страницу (и заголовки Cache-Control об этом позаботятся).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Признак дочернего процесса. Родитель ставит его в окружение, ребёнок по нему
# понимает, что следить не надо, а надо работать.
CHILD_MARK = "GENERATOR_RELOADER_CHILD"

# Как часто сверять время правки. Секунда: чаще незачем, реже — заметно.
POLL_SECONDS = 1.0

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", "logs", "tests"}


def is_child():
    return os.environ.get(CHILD_MARK) == "1"


def watched_files():
    """Все .py проекта. tests/ пропускаем: сервер их не импортирует."""
    for path in BASE_DIR.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(BASE_DIR).parts):
            continue
        yield path


def fingerprint():
    """Слепок состояния кода: путь -> время последней правки.

    Сравниваем словарь целиком, а не сумму: так замечается и новый файл, и
    удалённый, а не только правка существующего.
    """
    marks = {}
    for path in watched_files():
        try:
            marks[str(path)] = path.stat().st_mtime
        except OSError:
            continue                      # файл исчез между обходом и stat
    return marks


def _describe(before, after):
    """Что именно изменилось — чтобы в консоли было видно причину перезапуска."""
    changed = [p for p, m in after.items() if before.get(p) != m]
    added = [p for p in after if p not in before]
    gone = [p for p in before if p not in after]
    names = [Path(p).name for p in (changed or added or gone)]
    return ", ".join(sorted(set(names))[:4]) or "код"


def supervise(command=None, watch=fingerprint, announce=True):
    """Следит за кодом и перезапускает дочерний процесс. Возвращает код выхода.

    `command` и `watch` вынесены в аргументы ради проверок: сторож, который
    нельзя прогнать в тесте, сам становится следующей необнаруженной поломкой —
    а узнают о ней ровно так же, как о прошлой: по непонятному поведению уже
    исправленного кода.
    """
    env = dict(os.environ, **{CHILD_MARK: "1"})
    command = command or [sys.executable] + sys.argv
    fingerprint = watch

    if announce:
        print("Автоперезагрузка включена: слежу за .py в %s" % BASE_DIR, flush=True)
        print("Выключить: GENERATOR_AUTORELOAD=0 в .env\n", flush=True)

    while True:
        marks = fingerprint()
        child = subprocess.Popen(command, env=env)

        try:
            while True:
                code = child.poll()
                if code is not None:
                    # Ребёнок умер сам. Если это ошибка в коде — не крутим
                    # перезапуск вхолостую, а ждём правки и пробуем снова.
                    if code == 0:
                        return 0
                    if announce:
                        print("\nСервер остановился с кодом %d. Жду правки кода..."
                              % code, flush=True)
                    while fingerprint() == marks:
                        time.sleep(POLL_SECONDS)
                    break

                fresh = fingerprint()
                if fresh != marks:
                    if announce:
                        print("\n[перезапуск] изменилось: %s"
                              % _describe(marks, fresh), flush=True)
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                    break

                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
            return 0
