# -*- coding: utf-8 -*-
"""Печатает бесплатные модели, доступные прямо сейчас через OpenRouter.

Каталог OpenRouter не завязан на конкретный ключ (в отличие от Gemini) — это
общая витрина всех моделей шлюза, поэтому список актуален для любого ключа.
Список бесплатных моделей меняется чаще документации, поэтому лучше спросить
у самого OpenRouter, а не гадать по имени модели в коде.

Запуск:  python tests/openrouter_list_models.py
Ключ не обязателен для самого списка, но нужен, чтобы реально вызывать модель
(см. .env.example).
"""
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from review.llm_provider import OpenRouterProvider  # noqa: E402

p = OpenRouterProvider()

try:
    models = p.list_models(free_only=True)
except Exception as exc:
    print("Не удалось получить список моделей:", exc)
    sys.exit(1)

if not models:
    print("Бесплатных моделей сейчас не нашлось (или изменился формат ответа API).")
    sys.exit(1)

print(f"Бесплатных моделей на OpenRouter: {len(models)}\n")
for m in sorted(models):
    mark = "  <- используется по умолчанию (OpenRouterProvider.DEFAULT_MODEL)" if m == OpenRouterProvider.DEFAULT_MODEL else ""
    print(" ", m, mark)

print(f"\nТекущая модель по умолчанию: {OpenRouterProvider.DEFAULT_MODEL}")
print("Если её нет в списке выше или хочется другую — впишите в .env строку:")
print("  OPENROUTER_MODEL=<одно из имён выше>")

if not p.api_key:
    print("\nПодсказка: OPENROUTER_API_KEY не задан — сам вызов модели пока не заработает,")
    print("это только список каталога. Ключ — на https://openrouter.ai/keys")
