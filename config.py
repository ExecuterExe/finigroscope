"""Где сервис хранит данные и в каком режиме он запущен.

Вынесено из app.py отдельным модулем по одной практической причине: путь к базе
раньше был константой в коде, а очистка хранилища срабатывала как ПОБОЧНЫЙ
ЭФФЕКТ ИМПОРТА `app`. Из-за этого любой запуск тестов (а они импортируют app)
стирал базу работающего рядом дев-сервера: открытая вкладка получала 403 на
собственные документы, и данные приходилось пересоздавать вручную.

Теперь путь берётся из окружения, а очистка требует явного разрешения.

Переменные окружения:
  FINIGROSKOP_DB       — путь к файлу SQLite (по умолчанию instance/finigroskop.db)
  FINIGROSKOP_UPLOADS  — папка загруженных .docx
  FINIGROSKOP_RESET    — «1» разрешает очистку хранилища при старте (только dev)
  FINIGROSKOP_SECRET   — ключ подписи сессий
  FINIGROSKOP_JOBS_SYNC — «1» выполняет фоновые задачи синхронно (тесты)
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _entry_is_test_script() -> bool:
    """Запущен ли процесс скриптом из папки tests/."""
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0 or argv0.startswith("-"):     # python -c, python -m, интерпретатор
        return False
    return os.path.dirname(os.path.abspath(argv0)) == os.path.join(_HERE, "tests")


def is_test_run() -> bool:
    """Идёт ли прогон тестов.

    Признаков ДВА, и они срабатывают по «или»:

    1. Запускающий скрипт лежит в `tests/`. Это главный признак: он не зависит
       ни от какого окружения и работает для любого будущего набора проверок.
    2. Задана `LLM_PROVIDER_FORCE` — переменная, которая в .env.example прямо
       описана как «только для тестов/CI, в обычной работе не задавайте».

    Одного второго признака было мало, и это выяснилось на живом прогоне:
    llm_settings_flow_check намеренно СНИМАЕТ эту переменную (весь смысл того
    набора — что выбор в интерфейсе не игнорируется), и потому единственный из
    всех уходил работать в боевую базу. Стирания не происходило, но гостевые
    записи в рабочую базу он добавлял. Признак по пути запуска закрывает этот
    случай, не требуя правки самих наборов.

    Любой признак перебивается явным `FINIGROSKOP_DB`.
    """
    return _entry_is_test_script() or bool(os.environ.get("LLM_PROVIDER_FORCE"))


def instance_dir() -> str:
    path = os.path.join(_HERE, "instance")
    os.makedirs(path, exist_ok=True)
    return path


def database_path() -> str:
    """Файл базы. У тестового прогона он СВОЙ и никогда не совпадает с боевым."""
    explicit = os.environ.get("FINIGROSKOP_DB")
    if explicit:
        return os.path.abspath(explicit)
    name = "finigroskop-test.db" if is_test_run() else "finigroskop.db"
    return os.path.join(instance_dir(), name)


def database_uri() -> str:
    return "sqlite:///" + database_path().replace("\\", "/")


def upload_dir() -> str:
    explicit = os.environ.get("FINIGROSKOP_UPLOADS")
    path = os.path.abspath(explicit) if explicit else os.path.join(
        _HERE, "uploads-test" if is_test_run() else "uploads")
    os.makedirs(path, exist_ok=True)
    return path


def reset_allowed() -> bool:
    """Разрешено ли стирать хранилище при старте.

    По умолчанию НЕТ — ни при импорте, ни при запуске. Дев-режим включает это
    явно (см. блок `__main__` в app.py), тестовый прогон — своей переменной.
    Так очистка перестала быть невидимым побочным эффектом.
    """
    if os.environ.get("FINIGROSKOP_RESET") is not None:
        return _flag("FINIGROSKOP_RESET")
    return is_test_run()


def jobs_sync() -> bool:
    """Выполнять фоновые задачи прямо в обработчике запроса, а не в пуле.

    Нужно тестам: сквозные проверки написаны как «нажали кнопку — сразу смотрим
    результат в базе», и городить в каждой ожидание потока было бы и медленнее,
    и хрупче. Явная переменная сильнее эвристики — асинхронные проверки ставят
    FINIGROSKOP_JOBS_SYNC=0 и получают настоящий пул.
    """
    if os.environ.get("FINIGROSKOP_JOBS_SYNC") is not None:
        return _flag("FINIGROSKOP_JOBS_SYNC")
    return is_test_run()


def secret_key() -> str:
    return os.environ.get("FINIGROSKOP_SECRET") or "finigroskop-dev-secret-change-me"


def max_workers() -> int:
    """Сколько задач считается одновременно. Начинаем с двух — см. jobs.py."""
    try:
        return max(1, int(os.environ.get("FINIGROSKOP_WORKERS") or 2))
    except ValueError:
        return 2
