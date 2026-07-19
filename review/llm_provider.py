"""Абстракция LLM-провайдера (раздел 6.2 проектного документа).

Провайдер меняется строкой конфига (LLM_PROVIDER в .env). Зарегистрированы:
`null` (заглушка — по умолчанию, если ничего не настроено), `openrouter`
(рекомендуется — шлюз к десяткам моделей, включая бесплатные, один ключ),
`gemini` (альтернатива, напрямую). NullProvider не делает сетевых вызовов и
честно сообщает, что ИИ недоступен — это изящная деградация, сервис не падает
без ключей.

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


class GeminiProvider(LLMProvider):
    """Google Gemini через REST API generativelanguage.googleapis.com.

    Ключ и модель берутся из окружения (см. .env.example в корне проекта):
      GEMINI_API_KEY — обязателен, ключ с aistudio.google.com/apikey
      GEMINI_MODEL   — необязателен, по умолчанию "gemini-flash-latest"

    По умолчанию используется РОЛЛИНГ-АЛИАС "gemini-flash-latest", а не
    конкретная версия вроде "gemini-2.5-flash": Google периодически снимает
    старые версии с публичного доступа для новых ключей (это уже случалось —
    см. tests/gemini_list_models.py, чтобы посмотреть, что доступно именно
    вашему ключу прямо сейчас), а алиас Google обновляет сам.

    Никакого специального SDK не требуется — только уже используемый в проекте
    `requests`, что держит зависимости минимальными.
    """

    name = "gemini"
    DEFAULT_MODEL = "gemini-flash-latest"
    API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, cache: ResponseCache | None = None, **options):
        super().__init__(cache=cache, **options)
        self.api_key = options.get("api_key") or os.environ.get("GEMINI_API_KEY")
        self.model = options.get("model") or os.environ.get("GEMINI_MODEL") or self.DEFAULT_MODEL

    def list_models(self) -> list[str]:
        """Модели, реально доступные этому ключу и поддерживающие generateContent.

        Обёртка над ListModels API — самый надёжный способ узнать актуальные
        имена моделей, не гадая по документации (которая быстро устаревает).
        """
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY не задан.")
        import requests

        resp = requests.get(self.API_BASE, params={"key": self.api_key}, timeout=30)
        resp.raise_for_status()
        return [
            m["name"].split("/", 1)[-1]
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

    def _complete(self, system: str, user: str, **opts) -> str:
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY не задан. Получите ключ на aistudio.google.com/apikey "
                "и впишите его в .env в корне проекта (см. .env.example)."
            )

        import requests

        url = f"{self.API_BASE}/{self.model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": opts.get("temperature", 0.3)},
        }
        try:
            # Короткий таймаут: сервер разработки Flask однопоточный, долгое
            # ожидание модели выглядит как «сайт завис», а не «модель думает».
            resp = requests.post(url, params={"key": self.api_key}, json=payload,
                                 timeout=opts.get("timeout", 25))
        except requests.RequestException as exc:
            raise RuntimeError(f"Gemini API недоступен: {exc}") from exc

        if resp.status_code != 200:
            hint = ""
            if resp.status_code == 404:
                hint = (f" Модель «{self.model}» недоступна для вашего ключа — узнайте актуальные "
                        "модели командой: python tests/gemini_list_models.py")
            raise RuntimeError(f"Gemini API вернул {resp.status_code}: {resp.text[:300]}{hint}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                raise RuntimeError(f"Gemini заблокировал ответ (blockReason={block_reason})")
            raise RuntimeError("Gemini не вернул ни одного кандидата ответа")

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            raise RuntimeError("Gemini вернул пустой ответ")
        return text


class _OpenRouterRetryable(RuntimeError):
    """Ошибка конкретной модели, при которой имеет смысл попробовать другую
    (лимит/перегрузка/модель не найдена) — в отличие от неверного ключа."""


class OpenRouterProvider(LLMProvider):
    """OpenRouter — шлюз к десяткам моделей (включая бесплатные) через один
    OpenAI-совместимый Chat Completions API. Один ключ, одна абстракция,
    можно сменить модель конфигом, не завися от прихотей одного вендора.

    Ключ и модель из окружения (см. .env.example в корне проекта):
      OPENROUTER_API_KEY — обязателен, ключ с openrouter.ai/keys
      OPENROUTER_MODEL   — необязателен. Если НЕ задан явно, провайдер сам
        перебирает FALLBACK_MODELS при перегрузке (429) или недоступности
        конкретной модели (400/404/5xx) — бесплатные модели на OpenRouter
        делят общую квоту между всеми пользователями и часто на минуты
        уходят в rate-limit, особенно популярные вроде Llama 3.3 70B; вместо
        того чтобы падать, пробуем следующую в списке. Если модель задана
        явно (в .env или конфиге) — уважаем выбор и не подменяем её молча.

    Никакого специального SDK не требуется — только уже используемый в проекте
    `requests`.
    """

    name = "openrouter"
    DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
    # Порядок — по убыванию предпочтения: сильные и проверенные модели из
    # каталога на момент подключения (см. tests/openrouter_list_models.py,
    # если список опять изменится). Первая — DEFAULT_MODEL.
    FALLBACK_MODELS = (
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-20b:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
    )
    API_BASE = "https://openrouter.ai/api/v1"

    def __init__(self, cache: ResponseCache | None = None, **options):
        super().__init__(cache=cache, **options)
        self.api_key = options.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
        explicit = options.get("model") or os.environ.get("OPENROUTER_MODEL")
        self.model = explicit or self.DEFAULT_MODEL
        self.model_explicit = bool(explicit)

    def list_models(self, free_only: bool = True) -> list[str]:
        """Модели из публичного каталога OpenRouter (по умолчанию — только бесплатные).

        В отличие от Gemini каталог не завязан на конкретный ключ — это витрина
        всех моделей, доступных через шлюз, ключ не нужен даже для этого запроса.
        """
        import requests

        resp = requests.get(f"{self.API_BASE}/models", timeout=30)
        resp.raise_for_status()
        out = []
        for m in resp.json().get("data", []):
            model_id = m.get("id", "")
            pricing = m.get("pricing", {})
            is_free = model_id.endswith(":free") or str(pricing.get("prompt")) in ("0", "0.0")
            if not free_only or is_free:
                out.append(model_id)
        return out

    def _call_model(self, model: str, system: str, user: str, **opts) -> str:
        """Один запрос к одной конкретной модели. Бросает _OpenRouterRetryable
        для проблем, из-за которых стоит попробовать другую модель."""
        import requests

        url = f"{self.API_BASE}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": opts.get("temperature", 0.3),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # необязательные, но рекомендуемые OpenRouter заголовки атрибуции
            "HTTP-Referer": "https://finigroskop.local",
            "X-Title": "FinIgroSkop",
        }
        try:
            # Таймаут короче, чем можно было бы подумать: сервер разработки Flask
            # однопоточный, и пока модель не ответила, ЛЮБОЙ другой запрос
            # (даже открыть другую страницу) тоже подвисает — долгий таймаут
            # выглядит как «сайт завис», а не «модель ещё думает».
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=opts.get("timeout", 25))
        except requests.RequestException as exc:
            # Сетевая ошибка/таймаут — не обязательно проблема именно этой модели,
            # но имеет смысл попробовать следующую из каскада, а не сдаваться сразу.
            raise _OpenRouterRetryable(f"{model}: сеть/таймаут ({exc})") from exc

        if resp.status_code in (401, 403):
            raise RuntimeError(f"OpenRouter отклонил ключ ({resp.status_code}) — "
                               "проверьте OPENROUTER_API_KEY в .env.")
        if resp.status_code != 200:
            raise _OpenRouterRetryable(f"{model}: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise _OpenRouterRetryable(f"{model}: нет вариантов ответа")
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise _OpenRouterRetryable(f"{model}: пустой ответ")
        return text

    def _complete(self, system: str, user: str, **opts) -> str:
        if not self.api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY не задан. Получите ключ на openrouter.ai/keys "
                "и впишите его в .env в корне проекта (см. .env.example)."
            )

        candidates = [self.model] if self.model_explicit else list(self.FALLBACK_MODELS)
        errors = []
        for model in candidates:
            try:
                return self._call_model(model, system, user, **opts)
            except _OpenRouterRetryable as exc:
                errors.append(str(exc))
                continue

        hint = " Проверьте актуальные бесплатные модели: python tests/openrouter_list_models.py"
        raise RuntimeError(
            f"Все модели-кандидаты сейчас недоступны/перегружены: {'; '.join(errors)}.{hint}"
        )


# --- реестр и фабрика -------------------------------------------------------
# Конкретные провайдеры регистрируются здесь по мере реализации:
#   register("gigachat", GigaChatProvider)
_REGISTRY: dict[str, type] = {
    "null": NullProvider,
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
}


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
