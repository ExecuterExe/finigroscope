# -*- coding: utf-8 -*-
"""Проверка GeminiProvider без реального ключа: сборка запроса, разбор ответа,
разбор ошибок (не-200, блокировка, пустой ответ), деградация без ключа.
Сеть подменяется — тест не делает реальных вызовов к Google.
"""
import sys

sys.path.insert(0, ".")

import requests  # noqa: E402

from review.llm_provider import GeminiProvider, get_provider  # noqa: E402


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


# --- 1) нет ключа -> понятная ошибка, без сетевого вызова -------------------
calls = []
patch_post(lambda *a, **kw: calls.append((a, kw)) or FakeResp(200))
p = GeminiProvider(api_key=None)
resp = p.complete("system", "user")
print("1) нет ключа: available=", resp.available, "| error содержит подсказку:",
      "GEMINI_API_KEY" in (resp.error or ""))
print("   сетевых вызовов не было:", len(calls) == 0)

# --- 2) успешный ответ -------------------------------------------------------
def ok_post(url, params=None, json=None, timeout=None):
    assert params["key"] == "test-key"
    assert json["system_instruction"]["parts"][0]["text"] == "SYS"
    assert json["contents"][0]["parts"][0]["text"] == "USR"
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": "Привет, "}, {"text": "мир!"}]}}]})

patch_post(ok_post)
p = GeminiProvider(api_key="test-key")
resp = p.complete("SYS", "USR")
print("\n2) успешный ответ: available=", resp.available, "| текст:", repr(resp.text))
assert resp.available and resp.text == "Привет, мир!"

# --- 3) провайдер и модель по умолчанию --------------------------------------
p2 = GeminiProvider(api_key="k")
print("\n3) модель по умолчанию:", p2.model)
assert p2.model == "gemini-flash-latest"

# --- 4) ошибка сети / не-200 --------------------------------------------------
patch_post(lambda *a, **kw: FakeResp(429, text="quota exceeded"))
p3 = GeminiProvider(api_key="k")
resp = p3.complete("s", "u")
print("\n4) HTTP 429: available=", resp.available, "| error:", resp.error)
assert not resp.available and "429" in resp.error

# --- 5) заблокированный ответ -------------------------------------------------
patch_post(lambda *a, **kw: FakeResp(200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))
p4 = GeminiProvider(api_key="k")
resp = p4.complete("s", "u")
print("\n5) заблокировано: available=", resp.available, "| error:", resp.error)
assert not resp.available and "SAFETY" in resp.error

# --- 6) фабрика get_provider("gemini") создаёт нужный класс -------------------
prov = get_provider("gemini", api_key="x")
print("\n6) get_provider('gemini') -> тип:", type(prov).__name__)
assert isinstance(prov, GeminiProvider)

# --- 7) 404 с моделью -> подсказка на диагностический скрипт ------------------
patch_post(lambda *a, **kw: FakeResp(404, text='{"error":{"message":"no longer available"}}'))
p5 = GeminiProvider(api_key="k", model="gemini-2.5-flash")
resp = p5.complete("s", "u")
print("\n7) 404 модель: error содержит подсказку на диагностику:",
      "gemini_list_models.py" in resp.error)
assert "gemini_list_models.py" in resp.error

# --- 8) list_models() парсит ListModels-ответ ---------------------------------
def models_get(url, params=None, timeout=None):
    assert params["key"] == "k"
    return FakeResp(200, {"models": [
        {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
    ]})

requests.get = models_get
p6 = GeminiProvider(api_key="k")
models = p6.list_models()
print("\n8) list_models():", models)
assert models == ["gemini-flash-latest"]

print("\nВСЁ ОК")
