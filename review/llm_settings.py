"""Выбор LLM во время работы сервиса — то, что пользователь нажал в интерфейсе.

Зачем отдельный слой поверх .env: `.env` — это НАСТРОЙКА УСТАНОВКИ (её правит
тот, кто разворачивает сервис, и для применения нужен перезапуск). А выбрать,
какой моделью считать прямо сейчас, хочется из интерфейса и без перезапуска —
особенно когда у одного вендора кончилась квота и надо быстро переключиться на
другого. Поэтому выбор хранится в маленьком JSON-файле рядом с базой.

Приоритеты разрешения — в `llm_provider.resolve_provider_name`. Здесь только
хранилище: ключей API тут НЕТ (они остаются в .env и в файл не попадают),
хранится лишь имя провайдера и, если пользователь его закрепил, имя модели.
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(__file__)
_DEFAULT_PATH = os.path.join(os.path.dirname(_HERE), "instance", "llm_settings.json")


def path() -> str:
    """Где лежит файл выбора. LLM_SETTINGS_PATH позволяет увести его в тесты."""
    return os.environ.get("LLM_SETTINGS_PATH") or _DEFAULT_PATH


def load() -> dict:
    """Сохранённый выбор: {"provider": ..., "model": ...} или {}.

    Любая проблема с файлом (нет, побился, нет прав) — это не повод падать:
    сервис просто вернётся к значению из .env.
    """
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(provider: str, model: str = None) -> dict:
    """Запоминает выбор. Пустая модель = «не закреплять» (разрешить каскад)."""
    data = {"provider": (provider or "").lower().strip() or None,
            "model": (model or "").strip() or None}
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def clear() -> None:
    """Убирает выбор из интерфейса — сервис возвращается к .env."""
    try:
        os.remove(path())
    except OSError:
        pass


def status() -> dict:
    """Полная картина для страницы настроек: кто активен, откуда и настроен ли.

    Собирается здесь, а не в шаблоне, чтобы страница не занималась логикой
    приоритетов — она только рисует то, что ей дали.
    """
    from review import llm_provider

    name, source = llm_provider.resolve_provider_name()
    cls = llm_provider.provider_class(name)
    saved = load()
    # Провайдер создаём, чтобы показать модель, которая реально пойдёт в запрос
    # (учитывая .env, выбор в интерфейсе и значение по умолчанию).
    try:
        active_model = llm_provider.get_provider().model
    except Exception:
        active_model = None
    return {
        "provider": name,
        "title": cls.TITLE,
        "source": source,
        "configured": cls.is_configured(),
        "requires": cls.REQUIRES or cls.KEY_ENV,
        "known": name in llm_provider.available_providers(),
        "model": active_model,
        "pinned_model": saved.get("model"),
        "saved_provider": saved.get("provider"),
        "settings_path": path(),
        "forced": bool(os.environ.get("LLM_PROVIDER_FORCE")),
        "env_provider": os.environ.get("LLM_PROVIDER"),
    }
