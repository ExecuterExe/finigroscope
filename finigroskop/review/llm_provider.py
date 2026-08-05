"""Абстракция LLM-провайдера (раздел 6.2 проектного документа).

Сервис НЕ привязан к одному вендору: провайдер выбирается конфигом (.env) или
прямо в интерфейсе (страница «Настройки ИИ», см. review/llm_settings.py).
Зарегистрированы:

  null        — заглушка: ИИ недоступен, сервис при этом работает (деградация)
  openrouter  — шлюз к десяткам моделей одним ключом, есть бесплатные
  gemini      — Google Gemini напрямую (нативный REST-протокол)
  xai         — xAI Grok
  deepseek    — DeepSeek (её же рекомендует промпт симуляциониста)
  anthropic   — Anthropic Claude (нативный /v1/messages)
  openai      — OpenAI
  groq        — Groq (очень быстрый инференс)
  custom      — любой свой OpenAI-совместимый сервер (Ollama, LM Studio, vLLM,
                прокси, в т.ч. OpenAI-совместимый эндпоинт самого Gemini)

Ключевое наблюдение, на котором держится вся конструкция: подавляющее
большинство вендоров говорит на ОДНОМ протоколе — OpenAI Chat Completions
(POST {base}/chat/completions, Bearer-ключ, messages[]). Поэтому такие
провайдеры описываются декларативно — классом на 5 строк с базовым URL,
именем переменной ключа и моделью по умолчанию (см. OpenAICompatProvider).
Отдельного кода требуют только те, кто протокол не повторяет: Gemini
(system_instruction/contents) и Anthropic (x-api-key + /v1/messages).

Модель нигде не «зашита» намертво: у каждого провайдера есть DEFAULT_MODEL,
переменная окружения и `list_models()` — живой список от самого вендора. Если
вендор переименует модели (это регулярно случается), список в интерфейсе
покажет актуальные имена, а не устаревшую константу из кода.

Кэширование по хэшу (раздел 6.2): повторная рецензия неизменённого ввода —
ноль токенов. Кэш абстрактный (по умолчанию — на диске JSON). Ключ кэша
включает имя провайдера и модель, поэтому смена LLM не отдаёт чужой ответ.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
from dataclasses import dataclass

DEFAULT_TIMEOUT = 25
DEFAULT_TEMPERATURE = 0.3


@dataclass
class LLMResponse:
    text: str
    provider: str
    cached: bool = False
    available: bool = True
    error: str | None = None
    model: str | None = None


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
    """Базовый класс провайдера. Конкретные провайдеры реализуют `_complete`.

    Атрибуты класса — это ещё и МЕТАДАННЫЕ для интерфейса выбора LLM
    (см. provider_catalog): как называется, где взять ключ, в какой переменной
    он лежит, какая модель по умолчанию. Благодаря этому страница настроек не
    содержит захардкоженного списка вендоров — она рисуется по реестру.
    """

    name = "base"
    TITLE = "Базовый провайдер"
    KEY_ENV: str | None = None       # переменная окружения с ключом
    MODEL_ENV: str | None = None     # переменная окружения с моделью
    KEY_URL: str | None = None       # где получить ключ
    # Что обязательно задать в .env, чтобы провайдер заработал. По умолчанию —
    # ключ. None означает «ничего не нужно» (заглушка null), и тогда интерфейс не
    # рисует бейдж настроенности: он там был бы бессмысленным.
    REQUIRES: str | None = None
    DEFAULT_MODEL: str | None = None
    FALLBACK_MODELS: tuple = ()      # каскад при перегрузке (если модель не закреплена)
    FREE_TIER = False                # есть ли бесплатный доступ
    NOTES = ""                       # короткая подсказка для интерфейса

    def __init__(self, cache: ResponseCache | None = None, **options):
        self.cache = cache
        self.options = options
        self.model = None

    # --- метаданные для интерфейса -----------------------------------------
    @classmethod
    def is_configured(cls) -> bool:
        """Есть ли всё нужное, чтобы провайдер реально заработал."""
        return not cls.KEY_ENV or bool(os.environ.get(cls.KEY_ENV))

    @classmethod
    def describe(cls) -> dict:
        return {
            "name": cls.name,
            "title": cls.TITLE,
            "key_env": cls.KEY_ENV,
            "model_env": cls.MODEL_ENV,
            "key_url": cls.KEY_URL,
            "requires": cls.REQUIRES or cls.KEY_ENV,
            "default_model": cls.DEFAULT_MODEL,
            "free_tier": cls.FREE_TIER,
            "notes": cls.NOTES,
            "configured": cls.is_configured(),
            "can_list_models": hasattr(cls, "list_models"),
        }

    # --- вызов --------------------------------------------------------------
    def _timeout(self, opts: dict):
        """Таймаут: из вызова, иначе из LLM_TIMEOUT, иначе по умолчанию.

        Короткий намеренно: сервер разработки Flask однопоточный, и пока модель
        не ответила, ЛЮБОЙ другой запрос тоже подвисает — долгое ожидание
        выглядит как «сайт завис», а не «модель ещё думает».
        """
        if opts.get("timeout"):
            return opts["timeout"]
        try:
            return int(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            return DEFAULT_TIMEOUT

    @abc.abstractmethod
    def _complete(self, system: str, user: str, **opts) -> str:
        """Сырой вызов модели. Реализуется конкретным провайдером."""
        raise NotImplementedError

    def complete(self, system: str, user: str, use_cache: bool = True, **opts) -> LLMResponse:
        """Запрос с кэшем и обработкой ошибок (изящная деградация)."""
        key = None
        if self.cache and use_cache:
            # Модель — часть ключа: сменили LLM, значит ответ должен считаться
            # заново, а не подтянуться из кэша предыдущей.
            key = ResponseCache.make_key(self.name, self.model or "", system, user)
            hit = self.cache.get(key)
            if hit is not None:
                return LLMResponse(text=hit["text"], provider=self.name, cached=True,
                                   model=hit.get("model") or self.model)
        try:
            text = self._complete(system, user, **opts)
        except Exception as exc:  # сеть/ключи/лимиты не должны ронять сервис
            return LLMResponse(text="", provider=self.name, available=False,
                               error=str(exc), model=self.model)
        if self.cache and key:
            self.cache.put(key, {"text": text, "model": self.model})
        return LLMResponse(text=text, provider=self.name, model=self.model)


class NullProvider(LLMProvider):
    """Провайдер-заглушка: ИИ недоступен. Используется по умолчанию."""

    name = "null"
    TITLE = "Не настроен (ИИ выключен)"
    NOTES = "Сервис работает, но ИИ-агенты честно сообщают о недоступности."

    def _complete(self, system: str, user: str, **opts) -> str:
        raise RuntimeError("LLM-провайдер не настроен (NullProvider)")

    def complete(self, system: str, user: str, use_cache: bool = True, **opts) -> LLMResponse:
        return LLMResponse(
            text="", provider=self.name, available=False,
            error="ИИ-рецензия временно недоступна: провайдер не настроен.",
        )


# --- OpenAI-совместимые провайдеры ------------------------------------------
class _RetryableModelError(RuntimeError):
    """Ошибка конкретной модели, при которой имеет смысл попробовать другую
    (лимит/перегрузка/модель не найдена) — в отличие от неверного ключа."""


class OpenAICompatProvider(LLMProvider):
    """Общий провайдер для всех, кто говорит на OpenAI Chat Completions.

    Подклассу достаточно объявить API_BASE, KEY_ENV, MODEL_ENV и DEFAULT_MODEL —
    сетевой код, разбор ошибок, каскад моделей и список моделей наследуются.

    Каскад FALLBACK_MODELS включается ТОЛЬКО если модель не закреплена явно
    (ни в .env, ни в интерфейсе): бесплатные модели часто уходят в rate-limit,
    и вместо падения разумно попробовать следующую. Если модель выбрана
    осознанно — уважаем выбор и не подменяем её молча.
    """

    API_BASE: str | None = None
    BASE_URL_ENV: str | None = None   # позволяет переопределить адрес (для custom/прокси)
    EXTRA_HEADERS: dict = {}
    LIST_MODELS_HINT = ""             # подсказка в тексте ошибки

    def __init__(self, cache: ResponseCache | None = None, **options):
        super().__init__(cache=cache, **options)
        self.api_key = options.get("api_key") or (
            os.environ.get(self.KEY_ENV) if self.KEY_ENV else None)
        explicit_model = options.get("model") or (
            os.environ.get(self.MODEL_ENV) if self.MODEL_ENV else None)
        self.model = explicit_model or self.DEFAULT_MODEL
        self.model_explicit = bool(explicit_model)
        self.api_base = (
            options.get("base_url")
            or (os.environ.get(self.BASE_URL_ENV) if self.BASE_URL_ENV else None)
            or self.API_BASE
        )

    # --- вспомогательное ----------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.EXTRA_HEADERS)
        return headers

    def _missing_key_error(self) -> str:
        where = f" Ключ: {self.KEY_URL}" if self.KEY_URL else ""
        return (f"{self.KEY_ENV} не задан. Впишите ключ в .env в корне проекта "
                f"(см. .env.example).{where}")

    def list_models(self, free_only: bool = False) -> list[str]:
        """Живой список моделей от вендора (GET {base}/models).

        Именно этот вызов избавляет от гадания по документации: имена моделей у
        вендоров меняются, а список — всегда актуален.
        """
        import requests

        resp = requests.get(f"{self.api_base}/models", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        return [m.get("id", "") for m in (items or []) if m.get("id")]

    # --- сам вызов ----------------------------------------------------------
    def _call_model(self, model: str, system: str, user: str, **opts) -> str:
        """Один запрос к одной конкретной модели. Бросает _RetryableModelError
        для проблем, из-за которых стоит попробовать другую модель."""
        import requests

        url = f"{self.api_base}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": opts.get("temperature", DEFAULT_TEMPERATURE),
        }
        if opts.get("max_tokens"):
            payload["max_tokens"] = opts["max_tokens"]
        try:
            resp = requests.post(url, headers=self._headers(), json=payload,
                                 timeout=self._timeout(opts))
        except requests.RequestException as exc:
            # Сетевая ошибка/таймаут — не обязательно проблема именно этой модели,
            # но имеет смысл попробовать следующую из каскада, а не сдаваться сразу.
            raise _RetryableModelError(f"{model}: сеть/таймаут ({exc})") from exc

        if resp.status_code in (401, 403):
            raise RuntimeError(f"{self.TITLE} отклонил ключ ({resp.status_code}) — "
                               f"проверьте {self.KEY_ENV} в .env.")
        if resp.status_code != 200:
            raise _RetryableModelError(f"{model}: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise _RetryableModelError(f"{model}: нет вариантов ответа")
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise _RetryableModelError(f"{model}: пустой ответ")
        return text

    def _complete(self, system: str, user: str, **opts) -> str:
        if self.KEY_ENV and not self.api_key:
            raise RuntimeError(self._missing_key_error())
        if not self.api_base:
            raise RuntimeError(
                f"Не задан адрес API для «{self.TITLE}»"
                + (f" — укажите {self.BASE_URL_ENV} в .env." if self.BASE_URL_ENV else "."))
        if not self.model:
            raise RuntimeError(
                f"Для «{self.TITLE}» не выбрана модель — укажите её в настройках ИИ"
                + (f" или в переменной {self.MODEL_ENV}." if self.MODEL_ENV else "."))

        candidates = [self.model] if self.model_explicit else list(
            self.FALLBACK_MODELS or [self.model])
        errors = []
        for model in candidates:
            try:
                text = self._call_model(model, system, user, **opts)
                self.model = model  # что реально ответило — видно в отчёте/кэше
                return text
            except _RetryableModelError as exc:
                errors.append(str(exc))
                continue

        raise RuntimeError(
            f"Все модели-кандидаты сейчас недоступны/перегружены: "
            f"{'; '.join(errors)}.{self.LIST_MODELS_HINT}"
        )


class OpenRouterProvider(OpenAICompatProvider):
    """OpenRouter — шлюз к десяткам моделей (включая бесплатные) одним ключом.

    Модель не задана явно → провайдер сам перебирает FALLBACK_MODELS при
    перегрузке (429) или недоступности конкретной модели: бесплатные модели
    делят общую квоту между всеми пользователями и часто на минуты уходят в
    rate-limit, особенно популярные.
    """

    name = "openrouter"
    TITLE = "OpenRouter"
    KEY_ENV = "OPENROUTER_API_KEY"
    MODEL_ENV = "OPENROUTER_MODEL"
    KEY_URL = "https://openrouter.ai/keys"
    API_BASE = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
    # Порядок — по убыванию предпочтения. Первая обязана совпадать с DEFAULT_MODEL.
    #
    # Список сверен с живым каталогом (python tests/llm_list_models.py openrouter):
    # бесплатные модели на OpenRouter приходят и уходят каждые несколько месяцев,
    # и предыдущий каскад успел устареть на три позиции из четырёх. Поэтому важен
    # не конкретный набор, а принцип: сначала самые сильные и «длинные» (агентам
    # нужен большой вход — правила игры + промпт, и большой выход — симуляционист
    # отдаёт целый файл кода), а замыкает список openrouter/free — служебный
    # роутер, который сам выбирает любую доступную бесплатную модель, то есть
    # продолжит работать даже когда все имена выше сменятся.
    FALLBACK_MODELS = (
        "nvidia/nemotron-3-ultra-550b-a55b:free",    # frontier-reasoning, вход 1M
        "nvidia/nemotron-3-super-120b-a12b:free",    # выход до 262K — под генерацию кода
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",                   # переживает смены каталога дольше всех
        "openrouter/free",                           # роутер по любым бесплатным моделям
    )
    FREE_TIER = True
    NOTES = ("Один ключ — десятки моделей, есть бесплатные. При перегрузке сам "
             "переключается на следующую модель, если модель не закреплена.")
    LIST_MODELS_HINT = (" Проверьте актуальные бесплатные модели: "
                        "python tests/openrouter_list_models.py")
    # необязательные, но рекомендуемые OpenRouter заголовки атрибуции
    EXTRA_HEADERS = {
        "HTTP-Referer": "https://finigroskop.local",
        "X-Title": "FinIgroSkop",
    }

    def list_models(self, free_only: bool = True) -> list[str]:
        """Каталог OpenRouter (по умолчанию — только бесплатные модели).

        В отличие от остальных вендоров каталог не завязан на ключ — это витрина
        всех моделей шлюза, ключ не нужен даже для этого запроса.
        """
        import requests

        resp = requests.get(f"{self.api_base}/models", timeout=30)
        resp.raise_for_status()
        out = []
        for m in resp.json().get("data", []):
            model_id = m.get("id", "")
            pricing = m.get("pricing", {})
            is_free = model_id.endswith(":free") or str(pricing.get("prompt")) in ("0", "0.0")
            if not free_only or is_free:
                out.append(model_id)
        return out


class XAIProvider(OpenAICompatProvider):
    """xAI Grok. OpenAI-совместимый API на api.x.ai/v1."""

    name = "xai"
    TITLE = "xAI Grok"
    KEY_ENV = "XAI_API_KEY"
    MODEL_ENV = "XAI_MODEL"
    KEY_URL = "https://console.x.ai"
    API_BASE = "https://api.x.ai/v1"
    DEFAULT_MODEL = "grok-4.5"
    FALLBACK_MODELS = ("grok-4.5", "grok-4.3")
    NOTES = "Сильные модели Grok. Оплата по токенам, бесплатного тарифа нет."
    LIST_MODELS_HINT = " Актуальные модели: python tests/llm_list_models.py xai"


class DeepSeekProvider(OpenAICompatProvider):
    """DeepSeek. Дешёвые сильные модели; её же рекомендует промпт симуляциониста."""

    name = "deepseek"
    TITLE = "DeepSeek"
    KEY_ENV = "DEEPSEEK_API_KEY"
    MODEL_ENV = "DEEPSEEK_MODEL"
    KEY_URL = "https://platform.deepseek.com/api_keys"
    API_BASE = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-v4-pro"
    FALLBACK_MODELS = ("deepseek-v4-pro", "deepseek-v4-flash")
    NOTES = "Хороша в коде — именно её рекомендует промпт агента-симуляциониста."
    LIST_MODELS_HINT = " Актуальные модели: python tests/llm_list_models.py deepseek"


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI напрямую."""

    name = "openai"
    TITLE = "OpenAI"
    KEY_ENV = "OPENAI_API_KEY"
    MODEL_ENV = "OPENAI_MODEL"
    KEY_URL = "https://platform.openai.com/api-keys"
    API_BASE = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-5.6"
    NOTES = "Если модель по умолчанию устарела — посмотрите список моделей кнопкой ниже."
    LIST_MODELS_HINT = " Актуальные модели: python tests/llm_list_models.py openai"


class GroqProvider(OpenAICompatProvider):
    """Groq — очень быстрый инференс открытых моделей, есть бесплатный тариф.

    DEFAULT_MODEL намеренно НЕ задан: каталог Groq меняется часто, и лучше
    честно попросить выбрать модель из живого списка, чем подставить имя,
    которое, возможно, уже снято.
    """

    name = "groq"
    TITLE = "Groq"
    KEY_ENV = "GROQ_API_KEY"
    MODEL_ENV = "GROQ_MODEL"
    KEY_URL = "https://console.groq.com/keys"
    API_BASE = "https://api.groq.com/openai/v1"
    DEFAULT_MODEL = None
    FREE_TIER = True
    NOTES = "Очень быстрый. Модель нужно выбрать из списка — каталог часто меняется."
    LIST_MODELS_HINT = " Актуальные модели: python tests/llm_list_models.py groq"


class CustomOpenAIProvider(OpenAICompatProvider):
    """Любой свой OpenAI-совместимый сервер: Ollama, LM Studio, vLLM, прокси.

    Сюда же можно подключить OpenAI-совместимый эндпоинт Gemini
    (https://generativelanguage.googleapis.com/v1beta/openai), если нативный
    протокол по какой-то причине не подходит.

    Ключ необязателен — локальные серверы обычно его не требуют.
    """

    name = "custom"
    TITLE = "Свой сервер (OpenAI-совместимый)"
    KEY_ENV = None                       # ключ может и не понадобиться
    MODEL_ENV = "LLM_MODEL"
    BASE_URL_ENV = "LLM_BASE_URL"
    REQUIRES = "LLM_BASE_URL"            # адрес обязателен, ключ — нет
    API_BASE = None
    DEFAULT_MODEL = None
    NOTES = ("Укажите LLM_BASE_URL (например http://localhost:11434/v1 для Ollama) "
             "и модель. Ключ — при необходимости в LLM_API_KEY.")

    def __init__(self, cache: ResponseCache | None = None, **options):
        super().__init__(cache=cache, **options)
        # у «своего сервера» нет фиксированного KEY_ENV, но ключ может быть нужен
        self.api_key = self.api_key or os.environ.get("LLM_API_KEY")

    @classmethod
    def is_configured(cls) -> bool:
        return bool(os.environ.get(cls.BASE_URL_ENV))


# --- нативные протоколы -----------------------------------------------------
class GeminiProvider(LLMProvider):
    """Google Gemini через REST API generativelanguage.googleapis.com.

    Протокол у Gemini свой (system_instruction + contents), поэтому это
    отдельная реализация, а не подкласс OpenAICompatProvider. Взамен получаем
    внятные сообщения об ошибках (блокировка по безопасности, снятая модель).

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
    TITLE = "Google Gemini"
    KEY_ENV = "GEMINI_API_KEY"
    MODEL_ENV = "GEMINI_MODEL"
    KEY_URL = "https://aistudio.google.com/apikey"
    DEFAULT_MODEL = "gemini-flash-latest"
    API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    FREE_TIER = True
    NOTES = ("Модель по умолчанию — плавающий алиас, Google обновляет его сам. "
             "Есть бесплатная квота.")

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
            "generationConfig": {"temperature": opts.get("temperature", DEFAULT_TEMPERATURE)},
        }
        try:
            resp = requests.post(url, params={"key": self.api_key}, json=payload,
                                 timeout=self._timeout(opts))
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


class AnthropicProvider(LLMProvider):
    """Anthropic Claude через нативный Messages API.

    Протокол отличается от OpenAI: ключ в заголовке x-api-key, обязательный
    заголовок версии, system — отдельным полем (а не сообщением в messages), и
    max_tokens обязателен. Лимит вывода взят с запасом: агент-симуляционист
    возвращает целый файл кода, и коротким лимитом ответ бы обрезало.
    """

    name = "anthropic"
    TITLE = "Anthropic Claude"
    KEY_ENV = "ANTHROPIC_API_KEY"
    MODEL_ENV = "ANTHROPIC_MODEL"
    KEY_URL = "https://console.anthropic.com/settings/keys"
    API_BASE = "https://api.anthropic.com/v1"
    API_VERSION = "2023-06-01"
    DEFAULT_MODEL = "claude-sonnet-5"
    MAX_TOKENS = 16384
    NOTES = "Хорошо держит длинные инструкции и формат ответа."

    def __init__(self, cache: ResponseCache | None = None, **options):
        super().__init__(cache=cache, **options)
        self.api_key = options.get("api_key") or os.environ.get(self.KEY_ENV)
        self.model = (options.get("model") or os.environ.get(self.MODEL_ENV)
                      or self.DEFAULT_MODEL)

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

    def list_models(self) -> list[str]:
        import requests

        resp = requests.get(f"{self.API_BASE}/models", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return [m.get("id", "") for m in resp.json().get("data", []) if m.get("id")]

    def _complete(self, system: str, user: str, **opts) -> str:
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан. Получите ключ на "
                "console.anthropic.com/settings/keys и впишите его в .env "
                "(см. .env.example)."
            )

        import requests

        payload = {
            "model": self.model,
            "max_tokens": opts.get("max_tokens", self.MAX_TOKENS),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": opts.get("temperature", DEFAULT_TEMPERATURE),
        }
        try:
            resp = requests.post(f"{self.API_BASE}/messages", headers=self._headers(),
                                 json=payload, timeout=self._timeout(opts))
        except requests.RequestException as exc:
            raise RuntimeError(f"Anthropic API недоступен: {exc}") from exc

        if resp.status_code in (401, 403):
            raise RuntimeError(f"Anthropic отклонил ключ ({resp.status_code}) — "
                               "проверьте ANTHROPIC_API_KEY в .env.")
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API вернул {resp.status_code}: {resp.text[:300]}")

        blocks = resp.json().get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip():
            raise RuntimeError("Anthropic вернул пустой ответ")
        return text


# --- реестр и фабрика -------------------------------------------------------
# Порядок важен: в таком виде провайдеры показываются на странице настроек.
_REGISTRY: dict[str, type] = {}


def register(name: str, cls: type) -> None:
    _REGISTRY[name] = cls


for _cls in (NullProvider, OpenRouterProvider, GeminiProvider, XAIProvider,
             DeepSeekProvider, AnthropicProvider, OpenAIProvider, GroqProvider,
             CustomOpenAIProvider):
    register(_cls.name, _cls)


def available_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


def provider_class(name: str) -> type:
    return _REGISTRY.get((name or "").lower(), NullProvider)


def provider_catalog() -> list[dict]:
    """Метаданные всех провайдеров для страницы выбора LLM (в порядке реестра).

    Ключей API здесь НЕТ и быть не может — только факт «настроен / не настроен».
    """
    return [cls.describe() for cls in _REGISTRY.values()]


def resolve_provider_name(explicit: str = None) -> tuple[str, str]:
    """Какой провайдер использовать и откуда взялось это решение.

    Порядок приоритетов (от сильного к слабому):
      1) явный аргумент в коде;
      2) LLM_PROVIDER_FORCE — жёсткий override для тестов и CI (заодно
         полностью игнорирует выбор в интерфейсе, чтобы прогон тестов не
         зависел от того, что кто-то нажал на странице настроек);
      3) выбор в интерфейсе (review/llm_settings);
      4) LLM_PROVIDER из .env — значение по умолчанию для этой установки;
      5) null — ИИ выключен.
    """
    if explicit:
        return explicit.lower(), "аргумент вызова"
    forced = os.environ.get("LLM_PROVIDER_FORCE")
    if forced:
        return forced.lower(), "LLM_PROVIDER_FORCE"

    from review import llm_settings

    chosen = (llm_settings.load() or {}).get("provider")
    if chosen:
        return chosen.lower(), "выбор в интерфейсе"
    from_env = os.environ.get("LLM_PROVIDER")
    if from_env:
        return from_env.lower(), ".env (LLM_PROVIDER)"
    return "null", "по умолчанию"


def get_provider(name: str = None, cache_dir: str = None, **options) -> LLMProvider:
    """Создаёт провайдер по имени. Неизвестный/пустой → NullProvider.

    Если имя не передано — берётся из настроек интерфейса или .env
    (см. resolve_provider_name). Модель, выбранная в интерфейсе для ЭТОГО же
    провайдера, применяется автоматически, если вызывающий код не задал свою.
    """
    resolved, _source = resolve_provider_name(name)
    cls = _REGISTRY.get(resolved, NullProvider)

    if "model" not in options:
        from review import llm_settings

        settings = llm_settings.load() or {}
        # Модель применяем только если она сохранена ДЛЯ ЭТОГО провайдера:
        # имена моделей у вендоров разные, чужая модель гарантированно не найдётся.
        if settings.get("provider") == resolved and settings.get("model"):
            options["model"] = settings["model"]

    cache = ResponseCache(cache_dir) if cache_dir else None
    return cls(cache=cache, **options)
