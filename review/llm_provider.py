"""Абстракция LLM-провайдера (раздел 6.2 проектного документа).

Фундамент, на который позже наслаиваются конкретные провайдеры
(GigaChat / YandexGPT / Gemini / Groq / Ollama) — провайдер меняется строкой
конфига. Сейчас зарегистрирован только `NullProvider`: он не делает сетевых
вызовов и честно сообщает, что ИИ-рецензия недоступна. Это обеспечивает
изящную деградацию (сервис не ломается без ключей) и даёт интерфейс, под
который пишутся реальные провайдеры.

Кэширование по хэшу (раздел 6.2): повторная рецензия неизменённого ввода —
ноль токенов. Кэш абстрактный (по умолчанию — на диске JSON).
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
from dataclasses import dataclass


@dataclass
class LLMResponse:
    text: str
    provider: str
    cached: bool = False
    available: bool = True
    error: str | None = None


# --- кэш --------------------------------------------------------------------
class ResponseCache:
    """Простой файловый кэш ответов по ключу (хэш промпта + ввода)."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    @staticmethod
    def make_key(*parts: str) -> str:
        h = hashlib.sha256()
        for p in parts:
            h.update((p or "").encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def get(self, key: str):
        path = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def put(self, key: str, data: dict) -> None:
        path = os.path.join(self.cache_dir, key + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


# --- интерфейс провайдера ---------------------------------------------------
class LLMProvider(abc.ABC):
    """Базовый класс провайдера. Конкретные провайдеры реализуют `_complete`."""

    name = "base"

    def __init__(self, cache: ResponseCache | None = None, **options):
        self.cache = cache
        self.options = options

    @abc.abstractmethod
    def _complete(self, system: str, user: str, **opts) -> str:
        """Сырой вызов модели. Реализуется конкретным провайдером."""
        raise NotImplementedError

    def complete(self, system: str, user: str, use_cache: bool = True, **opts) -> LLMResponse:
        """Запрос с кэшем и обработкой ошибок (изящная деградация)."""
        key = None
        if self.cache and use_cache:
            key = ResponseCache.make_key(self.name, system, user)
            hit = self.cache.get(key)
            if hit is not None:
                return LLMResponse(text=hit["text"], provider=self.name, cached=True)
        try:
            text = self._complete(system, user, **opts)
        except Exception as exc:  # сеть/ключи/лимиты не должны ронять сервис
            return LLMResponse(text="", provider=self.name, available=False, error=str(exc))
        if self.cache and key:
            self.cache.put(key, {"text": text})
        return LLMResponse(text=text, provider=self.name)


class NullProvider(LLMProvider):
    """Провайдер-заглушка: ИИ недоступен. Используется по умолчанию."""

    name = "null"

    def _complete(self, system: str, user: str, **opts) -> str:
        raise RuntimeError("LLM-провайдер не настроен (NullProvider)")

    def complete(self, system: str, user: str, use_cache: bool = True, **opts) -> LLMResponse:
        return LLMResponse(
            text="", provider=self.name, available=False,
            error="ИИ-рецензия временно недоступна: провайдер не настроен.",
        )


# --- реестр и фабрика -------------------------------------------------------
# Конкретные провайдеры регистрируются здесь по мере реализации:
#   register("gigachat", GigaChatProvider)
_REGISTRY: dict[str, type] = {"null": NullProvider}


def register(name: str, cls: type) -> None:
    _REGISTRY[name] = cls


def available_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


def get_provider(name: str = None, cache_dir: str = None, **options) -> LLMProvider:
    """Создаёт провайдер по имени из конфига. Неизвестный/пустой → NullProvider."""
    name = (name or os.environ.get("LLM_PROVIDER") or "null").lower()
    cls = _REGISTRY.get(name, NullProvider)
    cache = ResponseCache(cache_dir) if cache_dir else None
    return cls(cache=cache, **options)
