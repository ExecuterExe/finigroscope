# -*- coding: utf-8 -*-
"""Печатает модели Gemini, реально доступные вашему ключу (для generateContent).

Google периодически снимает старые версии моделей с публичного доступа для
новых ключей — жёстко зашитое имя модели может однажды перестать работать
(именно так и случилось с "gemini-2.5-flash"). Этот скрипт спрашивает у самого
Google, что доступно ИМЕННО ВАШЕМУ ключу прямо сейчас, вместо гадания по
документации.

Запуск:  python tests/gemini_list_models.py
Нужен настроенный .env с GEMINI_API_KEY (см. .env.example).
"""
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from review.llm_provider import GeminiProvider  # noqa: E402

p = GeminiProvider()
if not p.api_key:
    print("GEMINI_API_KEY не задан. Впишите его в .env в корне проекта (см. .env.example).")
    sys.exit(1)

try:
    models = p.list_models()
except Exception as exc:
    print("Не удалось получить список моделей:", exc)
    sys.exit(1)

if not models:
    print("Ключ рабочий, но ни одна модель не поддерживает generateContent — странно, проверьте ключ.")
    sys.exit(1)

print(f"Доступно моделей с generateContent: {len(models)}\n")
for m in models:
    mark = "  <- используется по умолчанию (GeminiProvider.DEFAULT_MODEL)" if m == GeminiProvider.DEFAULT_MODEL else ""
    print(" ", m, mark)

print(f"\nТекущая модель по умолчанию: {GeminiProvider.DEFAULT_MODEL}")
print("Если её нет в списке выше или хочется другую — впишите в .env строку:")
print("  GEMINI_MODEL=<одно из имён выше>")
