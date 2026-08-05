# -*- coding: utf-8 -*-
"""Сторож перезапуска: следит за кодом и поднимает сервер заново.

Проверяется настоящими процессами, а не подменой: смысл сторожа в том, что
дочерний процесс ДЕЙСТВИТЕЛЬНО перезапускается, и подтвердить это можно только
запустив его.

Зачем сторож вообще нужен. Без него правка маршрута не доезжала до работающего
сервера, а страницу браузер перечитывал сам — получалась пара «новая страница +
старый сервер», и она отвечала «неизвестный адрес» на маршрут, который в
исходниках есть.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import reloader  # noqa: E402


def test_слепок_видит_правку_файла(tmp_path, monkeypatch):
    target = tmp_path / "модуль.py"
    target.write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(reloader, "BASE_DIR", tmp_path)

    before = reloader.fingerprint()
    time.sleep(0.01)
    target.write_text("x = 2", encoding="utf-8")
    # mtime на Windows бывает грубым, поэтому сдвигаем время явно
    import os
    os.utime(target, (time.time() + 5, time.time() + 5))

    assert reloader.fingerprint() != before


def test_слепок_видит_новый_и_удалённый_файл(tmp_path, monkeypatch):
    (tmp_path / "первый.py").write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(reloader, "BASE_DIR", tmp_path)
    before = reloader.fingerprint()

    second = tmp_path / "второй.py"
    second.write_text("y = 2", encoding="utf-8")
    with_new = reloader.fingerprint()
    assert with_new != before, "новый файл не замечен"

    second.unlink()
    assert reloader.fingerprint() != with_new, "удалённый файл не замечен"


def test_тесты_из_слежки_исключены(tmp_path, monkeypatch):
    """Сервер их не импортирует — перезапускаться из-за них незачем."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "проверка.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "мусор.py").write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(reloader, "BASE_DIR", tmp_path)

    assert reloader.fingerprint() == {}


def test_дочерний_процесс_узнаёт_себя(monkeypatch):
    monkeypatch.delenv(reloader.CHILD_MARK, raising=False)
    assert reloader.is_child() is False
    monkeypatch.setenv(reloader.CHILD_MARK, "1")
    assert reloader.is_child() is True


def test_правка_кода_перезапускает_процесс(tmp_path, monkeypatch):
    """Главная проверка: дочерний процесс поднимается заново после правки."""
    monkeypatch.setattr(reloader, "POLL_SECONDS", 0.05)

    counter = tmp_path / "запусков.txt"
    child = tmp_path / "ребёнок.py"
    child.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "p = Path(%r)\n"
        "p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')\n"
        "time.sleep(30)\n" % str(counter),
        encoding="utf-8")
    counter.write_text("0", encoding="utf-8")

    watched = tmp_path / "следим.py"
    watched.write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(reloader, "BASE_DIR", tmp_path)

    result = {}

    def run():
        result["code"] = reloader.supervise(
            command=[sys.executable, str(child)], announce=False)

    supervisor = threading.Thread(target=run, daemon=True)
    supervisor.start()

    # ждём первого запуска
    for _ in range(100):
        if counter.read_text().strip() == "1":
            break
        time.sleep(0.05)
    assert counter.read_text().strip() == "1", "дочерний процесс не запустился"

    # правим код — сторож обязан перезапустить
    import os
    watched.write_text("x = 2", encoding="utf-8")
    os.utime(watched, (time.time() + 5, time.time() + 5))

    for _ in range(200):
        if counter.read_text().strip() == "2":
            break
        time.sleep(0.05)

    assert counter.read_text().strip() == "2", "перезапуска не произошло"
