# -*- coding: utf-8 -*-
"""Проверка OpenRouterProvider без реального ключа: сборка запроса, разбор
ответа, разбор ошибок, деградация без ключа, список моделей. Сеть подменяется —
тест не делает реальных вызовов к OpenRouter.
"""
import sys

sys.path.insert(0, ".")

import requests  # noqa: E402

from review.llm_provider import OpenRouterProvider, get_provider  # noqa: E402


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


def patch_post(fn):
    requests.post = fn


def patch_get(fn):
    requests.get = fn


# --- 1) нет ключа -> понятная ошибка, без сетевого вызова -------------------
calls = []
patch_post(lambda *a, **kw: calls.append((a, kw)) or FakeResp(200))
p = OpenRouterProvider(api_key=None)
resp = p.complete("system", "user")
print("1) нет ключа: available=", resp.available, "| error содержит подсказку:",
      "OPENROUTER_API_KEY" in (resp.error or ""))
print("   сетевых вызовов не было:", len(calls) == 0)

# --- 2) успешный ответ, формат OpenAI-совместимый ----------------------------
def ok_post(url, headers=None, json=None, timeout=None):
    assert headers["Authorization"] == "Bearer test-key"
    assert json["model"] == OpenRouterProvider.DEFAULT_MODEL
    assert json["messages"][0] == {"role": "system", "content": "SYS"}
    assert json["messages"][1] == {"role": "user", "content": "USR"}
    return FakeResp(200, {"choices": [{"message": {"role": "assistant", "content": "Привет, мир!"}}]})

patch_post(ok_post)
p = OpenRouterProvider(api_key="test-key")
resp = p.complete("SYS", "USR")
print("\n2) успешный ответ: available=", resp.available, "| текст:", repr(resp.text))
assert resp.available and resp.text == "Привет, мир!"

# --- 3) модель по умолчанию ---------------------------------------------------
p2 = OpenRouterProvider(api_key="k")
print("\n3) модель по умолчанию:", p2.model)
assert p2.model == "meta-llama/llama-3.3-70b-instruct:free"

# --- 4) ошибка API (модель недоступна) ---------------------------------------
patch_post(lambda *a, **kw: FakeResp(400, text='{"error":{"message":"model not found"}}'))
p3 = OpenRouterProvider(api_key="k")
resp = p3.complete("s", "u")
print("\n4) HTTP 400: available=", resp.available, "| error содержит подсказку:",
      "openrouter_list_models.py" in resp.error)
assert not resp.available and "openrouter_list_models.py" in resp.error

# --- 5) пустой ответ (нет choices) --------------------------------------------
patch_post(lambda *a, **kw: FakeResp(200, {"choices": []}))
p4 = OpenRouterProvider(api_key="k")
resp = p4.complete("s", "u")
print("\n5) пустые choices: available=", resp.available, "| error:", resp.error)
assert not resp.available

# --- 6) фабрика get_provider("openrouter") ------------------------------------
prov = get_provider("openrouter", api_key="x")
print("\n6) get_provider('openrouter') -> тип:", type(prov).__name__)
assert isinstance(prov, OpenRouterProvider)

# --- 7) list_models() парсит каталог, фильтрует бесплатные --------------------
def models_get(url, timeout=None):
    return FakeResp(200, {"data": [
        {"id": "deepseek/deepseek-chat-v3.1:free", "pricing": {"prompt": "0"}},
        {"id": "openai/gpt-4o", "pricing": {"prompt": "0.005"}},
        {"id": "meta-llama/llama-3.3-70b-instruct:free", "pricing": {"prompt": "0"}},
    ]})

patch_get(models_get)
p5 = OpenRouterProvider(api_key="k")
free_models = p5.list_models(free_only=True)
all_models = p5.list_models(free_only=False)
print("\n7) list_models(free_only=True):", free_models)
print("   list_models(free_only=False):", all_models)
assert set(free_models) == {"deepseek/deepseek-chat-v3.1:free", "meta-llama/llama-3.3-70b-instruct:free"}
assert len(all_models) == 3

# --- 8) каскад: первая модель занята (429) -> пробуем следующую из FALLBACK ---
seen_models = []

def rate_limited_then_ok(url, headers=None, json=None, timeout=None):
    seen_models.append(json["model"])
    if len(seen_models) == 1:
        return FakeResp(429, text='{"error":{"message":"rate-limited upstream"}}')
    return FakeResp(200, {"choices": [{"message": {"content": "готово со второй попытки"}}]})

patch_post(rate_limited_then_ok)
p6 = OpenRouterProvider(api_key="k")  # модель НЕ задана явно -> каскад разрешён
resp = p6.complete("s", "u")
print("\n8) каскад при 429: available=", resp.available, "| текст:", repr(resp.text))
print("   модели по порядку:", seen_models)
assert resp.available and resp.text == "готово со второй попытки"
assert seen_models == list(OpenRouterProvider.FALLBACK_MODELS[:2])

# --- 9) явно заданная модель -> каскад НЕ включается, даже при ошибке ---------
seen_models2 = []
patch_post(lambda url, headers=None, json=None, timeout=None:
           seen_models2.append(json["model"]) or FakeResp(429, text="rate-limited"))
p7 = OpenRouterProvider(api_key="k", model="qwen/qwen3-next-80b-a3b-instruct:free")
resp = p7.complete("s", "u")
print("\n9) явная модель при 429: available=", resp.available, "| попыток:", len(seen_models2))
assert not resp.available and len(seen_models2) == 1
assert seen_models2 == ["qwen/qwen3-next-80b-a3b-instruct:free"]

# --- 10) таймаут/сетевая ошибка на первой модели -> тоже каскад, не мгновенный крах ---
seen_models3 = []

def timeout_then_ok(url, headers=None, json=None, timeout=None):
    seen_models3.append(json["model"])
    if len(seen_models3) == 1:
        raise requests.exceptions.Timeout("read timed out")
    return FakeResp(200, {"choices": [{"message": {"content": "ответила вторая модель"}}]})

patch_post(timeout_then_ok)
p8 = OpenRouterProvider(api_key="k")
resp = p8.complete("s", "u")
print("\n10) таймаут первой модели -> каскад: available=", resp.available, "| текст:", repr(resp.text))
print("    модели по порядку:", seen_models3)
assert resp.available and resp.text == "ответила вторая модель"
assert seen_models3 == list(OpenRouterProvider.FALLBACK_MODELS[:2])

print("\nВСЁ ОК")
