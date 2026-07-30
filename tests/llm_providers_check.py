# -*- coding: utf-8 -*-
"""Проверка мультипровайдерного слоя: сервис не привязан к одному вендору.

Сеть подменяется — реальных вызовов к вендорам тест не делает. Проверяется:
  • каждый OpenAI-совместимый провайдер шлёт запрос на СВОЙ адрес со своим ключом;
  • нативные протоколы (Gemini, Anthropic) собираются по-своему и разбираются верно;
  • деградация без ключа — понятная ошибка и НИ ОДНОГО сетевого вызова;
  • каскад моделей включается только когда модель не закреплена;
  • приоритеты выбора провайдера: аргумент > FORCE > интерфейс > .env > null;
  • ключ кэша учитывает модель (смена LLM не отдаёт чужой ответ);
  • каталог для интерфейса не раскрывает ключи.
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

# Изолируем файл выбора провайдера, чтобы тест не зависел от того, что нажато в UI.
_tmp = tempfile.mkdtemp(prefix="finigro-llm-")
os.environ["LLM_SETTINGS_PATH"] = os.path.join(_tmp, "llm_settings.json")
for _var in ("LLM_PROVIDER", "LLM_PROVIDER_FORCE", "OPENROUTER_MODEL", "XAI_MODEL",
             "DEEPSEEK_MODEL", "ANTHROPIC_MODEL", "OPENAI_MODEL", "GROQ_MODEL",
             "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
    os.environ.pop(_var, None)

import requests  # noqa: E402

from review import llm_settings  # noqa: E402
from review.llm_provider import (  # noqa: E402
    AnthropicProvider,
    CustomOpenAIProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    NullProvider,
    OpenAICompatProvider,
    OpenRouterProvider,
    ResponseCache,
    XAIProvider,
    available_providers,
    get_provider,
    provider_catalog,
    resolve_provider_name,
)

checks = {}


class FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


def ok_chat(content="готово"):
    return FakeResp(200, {"choices": [{"message": {"content": content}}]})


# --- 1) каждый OpenAI-совместимый вендор идёт на свой адрес со своим ключом ----
seen = {}


def spy_post(url, headers=None, json=None, timeout=None, params=None):
    seen["url"] = url
    seen["headers"] = headers or {}
    seen["json"] = json or {}
    return ok_chat("ответ вендора")


requests.post = spy_post

cases = [
    (XAIProvider, "https://api.x.ai/v1/chat/completions", "grok-4.5"),
    (DeepSeekProvider, "https://api.deepseek.com/v1/chat/completions", "deepseek-v4-pro"),
    # у OpenRouter имя модели берём из константы: бесплатный каталог вендора
    # меняется, и вписанное сюда имя устарело бы вместе с ним
    (OpenRouterProvider, "https://openrouter.ai/api/v1/chat/completions",
     OpenRouterProvider.DEFAULT_MODEL),
]
for cls, expect_url, expect_model in cases:
    p = cls(api_key="key-" + cls.name)
    resp = p.complete("SYS", "USR", use_cache=False)
    checks[f"{cls.name}: свой адрес"] = seen["url"] == expect_url
    checks[f"{cls.name}: Bearer-ключ"] = seen["headers"].get("Authorization") == f"Bearer key-{cls.name}"
    checks[f"{cls.name}: модель по умолчанию"] = seen["json"].get("model") == expect_model
    checks[f"{cls.name}: system+user в messages"] = seen["json"]["messages"] == [
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}]
    checks[f"{cls.name}: ответ разобран"] = resp.available and resp.text == "ответ вендора"

# OpenRouter добавляет заголовки атрибуции — они не должны потеряться в общем базовом классе
OpenRouterProvider(api_key="k").complete("s", "u", use_cache=False)
checks["openrouter: заголовки атрибуции"] = seen["headers"].get("X-Title") == "FinIgroSkop"

# --- 2) нет ключа -> понятная ошибка и ни одного сетевого вызова ---------------
calls = []
requests.post = lambda *a, **kw: calls.append(1) or ok_chat()
for cls, env_name in ((XAIProvider, "XAI_API_KEY"), (DeepSeekProvider, "DEEPSEEK_API_KEY"),
                      (AnthropicProvider, "ANTHROPIC_API_KEY")):
    os.environ.pop(env_name, None)
    resp = cls(api_key=None).complete("s", "u", use_cache=False)
    checks[f"{cls.name}: без ключа деградирует"] = (
        not resp.available and env_name in (resp.error or ""))
checks["без ключа сети не было"] = len(calls) == 0

# --- 3) Groq/custom без модели просят её выбрать, а не гадают ------------------
resp = GroqProvider(api_key="k").complete("s", "u", use_cache=False)
checks["groq: без модели просит выбрать"] = not resp.available and "модель" in (resp.error or "")
resp = CustomOpenAIProvider().complete("s", "u", use_cache=False)
checks["custom: без адреса просит LLM_BASE_URL"] = (
    not resp.available and "LLM_BASE_URL" in (resp.error or ""))

# свой сервер: адрес и модель из окружения, ключ не обязателен
os.environ["LLM_BASE_URL"] = "http://localhost:11434/v1"
os.environ["LLM_MODEL"] = "llama3.1"
requests.post = spy_post
resp = CustomOpenAIProvider().complete("s", "u", use_cache=False)
checks["custom: свой адрес из .env"] = seen["url"] == "http://localhost:11434/v1/chat/completions"
checks["custom: работает без ключа"] = resp.available and "Authorization" not in seen["headers"]
os.environ.pop("LLM_BASE_URL"); os.environ.pop("LLM_MODEL")

# --- 4) каскад: не закреплённая модель перебирается, закреплённая — нет --------
tried = []


def rate_limited_then_ok(url, headers=None, json=None, timeout=None, params=None):
    tried.append(json["model"])
    if len(tried) == 1:
        return FakeResp(429, text="rate limited")
    return ok_chat("вторая модель")


requests.post = rate_limited_then_ok
resp = XAIProvider(api_key="k").complete("s", "u", use_cache=False)
checks["xai: каскад при 429"] = resp.available and tried == list(XAIProvider.FALLBACK_MODELS[:2])
checks["xai: в ответе реально сработавшая модель"] = resp.model == XAIProvider.FALLBACK_MODELS[1]

tried2 = []
requests.post = lambda url, headers=None, json=None, timeout=None, params=None: (
    tried2.append(json["model"]) or FakeResp(429, text="rate limited"))
resp = XAIProvider(api_key="k", model="grok-4.3").complete("s", "u", use_cache=False)
checks["xai: закреплённую модель не подменяет"] = (not resp.available and tried2 == ["grok-4.3"])

# --- 5) неверный ключ (401) — это НЕ повод перебирать модели -------------------
tried3 = []
requests.post = lambda url, headers=None, json=None, timeout=None, params=None: (
    tried3.append(json["model"]) or FakeResp(401, text="unauthorized"))
resp = XAIProvider(api_key="bad").complete("s", "u", use_cache=False)
checks["401 не запускает каскад"] = len(tried3) == 1 and not resp.available
checks["401 указывает на переменную ключа"] = "XAI_API_KEY" in (resp.error or "")

# --- 6) нативный Anthropic: x-api-key, system отдельным полем ------------------
requests.post = spy_post
requests.post = lambda url, headers=None, json=None, timeout=None, params=None: (
    seen.update({"url": url, "headers": headers, "json": json})
    or FakeResp(200, {"content": [{"type": "text", "text": "Claude на связи"}]}))
resp = AnthropicProvider(api_key="ant-key").complete("SYS", "USR", use_cache=False)
checks["anthropic: свой адрес"] = seen["url"] == "https://api.anthropic.com/v1/messages"
checks["anthropic: ключ в x-api-key"] = seen["headers"].get("x-api-key") == "ant-key"
checks["anthropic: версия API"] = seen["headers"].get("anthropic-version") == "2023-06-01"
checks["anthropic: system отдельным полем"] = seen["json"].get("system") == "SYS"
checks["anthropic: max_tokens задан"] = seen["json"].get("max_tokens") == AnthropicProvider.MAX_TOKENS
checks["anthropic: ответ разобран"] = resp.available and resp.text == "Claude на связи"

# --- 7) нативный Gemini не сломан общим рефакторингом --------------------------
requests.post = lambda url, params=None, json=None, timeout=None, headers=None: (
    seen.update({"url": url, "params": params, "json": json})
    or FakeResp(200, {"candidates": [{"content": {"parts": [{"text": "Gemini на связи"}]}}]}))
resp = GeminiProvider(api_key="g-key").complete("SYS", "USR", use_cache=False)
checks["gemini: ключ параметром"] = seen["params"].get("key") == "g-key"
checks["gemini: system_instruction"] = (
    seen["json"]["system_instruction"]["parts"][0]["text"] == "SYS")
checks["gemini: ответ разобран"] = resp.available and resp.text == "Gemini на связи"

# --- 8) приоритеты выбора провайдера ------------------------------------------
llm_settings.clear()
os.environ.pop("LLM_PROVIDER", None)
checks["без настроек -> null"] = resolve_provider_name()[0] == "null"

os.environ["LLM_PROVIDER"] = "gemini"
checks[".env даёт провайдера"] = resolve_provider_name()[0] == "gemini"

llm_settings.save("xai", "")
checks["интерфейс перебивает .env"] = resolve_provider_name()[0] == "xai"

os.environ["LLM_PROVIDER_FORCE"] = "null"
checks["FORCE перебивает интерфейс"] = resolve_provider_name()[0] == "null"
os.environ.pop("LLM_PROVIDER_FORCE")

checks["аргумент перебивает всё"] = resolve_provider_name("deepseek")[0] == "deepseek"
checks["источник решения виден"] = resolve_provider_name()[1] == "выбор в интерфейсе"

# фабрика отдаёт класс выбранного провайдера
checks["get_provider следует выбору"] = isinstance(get_provider(), XAIProvider)
checks["неизвестное имя -> null"] = isinstance(get_provider("нетакого"), NullProvider)

# модель, закреплённая в интерфейсе, применяется — но только для своего провайдера
llm_settings.save("xai", "grok-4.3")
checks["модель из интерфейса применяется"] = get_provider().model == "grok-4.3"
checks["чужая модель не переносится"] = get_provider("deepseek").model == DeepSeekProvider.DEFAULT_MODEL
llm_settings.clear()
os.environ.pop("LLM_PROVIDER", None)

# --- 9) кэш учитывает модель: смена LLM не отдаёт чужой ответ ------------------
cache = ResponseCache(os.path.join(_tmp, "cache"))
requests.post = lambda url, headers=None, json=None, timeout=None, params=None: ok_chat(
    "ответ от " + json["model"])
a = XAIProvider(cache=cache, api_key="k", model="grok-4.5").complete("S", "U")
b = XAIProvider(cache=cache, api_key="k", model="grok-4.5").complete("S", "U")
c = XAIProvider(cache=cache, api_key="k", model="grok-4.3").complete("S", "U")
checks["кэш срабатывает на той же модели"] = b.cached and b.text == a.text
checks["другая модель считается заново"] = (not c.cached) and c.text == "ответ от grok-4.3"

# --- 10) каталог для интерфейса: полный, но без секретов -----------------------
os.environ["XAI_API_KEY"] = "secret-value-do-not-leak"
catalog = provider_catalog()
names = [p["name"] for p in catalog]
checks["каталог покрывает реестр"] = set(names) == set(available_providers())
checks["каталог знает про настроенность"] = next(
    p["configured"] for p in catalog if p["name"] == "xai") is True
checks["каталог не раскрывает ключ"] = all(
    "secret-value-do-not-leak" not in str(p) for p in catalog)
checks["у каждого есть человеческое название"] = all(p["title"] for p in catalog)
os.environ.pop("XAI_API_KEY")

# --- 11) новые провайдеры видны как OpenAI-совместимые -------------------------
checks["новые провайдеры на общем базовом классе"] = all(
    issubclass(cls, OpenAICompatProvider)
    for cls in (XAIProvider, DeepSeekProvider, GroqProvider, OpenRouterProvider,
                CustomOpenAIProvider))

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
