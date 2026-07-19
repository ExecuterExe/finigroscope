"""Агент «Зеркало понимания» (v2) — первый шаг ветки анализа, ДО извлечения структуры.

Двухпроходный диалог (полный текст роли — review/prompts/mirror.md):
  проход 1 — агент читает СЫРОЙ ТЕКСТ игры (никакого game_spec на входе — его
    ещё не существует), пересказывает игру, задаёт вопросы по «слепым пятнам», СТОП;
  проход 2 — учитывает ответы автора, отдаёт подтверждённый текст (исходный текст
    + уточнения автора) и ready_to_proceed. game_spec агент не строит — это
    работа следующего, отдельного агента-извлеченца (пока не реализован).

Этот модуль не решает игровую логику — только собирает промпт, вызывает
LLMProvider и разбирает ответ на человекочитаемую часть и машинный JSON-блок.
Через LLMProvider наследуется изящная деградация: без настроенного провайдера
`run_pass` вернёт `available=False`, а не упадёт.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider


def _split_response(raw_text: str):
    """Делит ответ агента на читаемый текст и JSON-блок в конце.

    Модель обязана класть JSON последним и без пояснений вокруг — ищем сначала
    блок в ```json fence```, затем как запасной вариант — последний `{...}` от
    конца текста. Если ничего не распарсилось — human-текст возвращается
    целиком, а json = None (вызывающий код обязан на это отреагировать, не упасть).
    """
    raw_text = (raw_text or "").strip()
    m = re.search(r"```json\s*(\{.*?\})\s*```\s*$", raw_text, re.DOTALL)
    if not m:
        m = re.search(r"(\{[\s\S]*\})\s*$", raw_text)
    if not m:
        return raw_text, None
    human = raw_text[: m.start()].strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return raw_text, None
    return human, data


def _build_user_message(game_text: str, prior_json: dict = None, author_answer: str = None) -> str:
    """Текст игры как есть — v2 работает с прозой напрямую, никакого game_spec."""
    parts = [
        "=== ТЕКСТ ИГРЫ (по разделам, как в документе) ===",
        game_text or "(текст игры не передан)",
    ]
    if author_answer is None:
        parts += ["", "Ответов автора пока нет — это ПРОХОД 1."]
    else:
        parts += [
            "",
            "=== ТВОЁ ЗЕРКАЛО С ПРОШЛОГО ПРОХОДА ===",
            json.dumps(prior_json or {}, ensure_ascii=False, indent=2),
            "",
            "=== ОТВЕТ АВТОРА ===",
            author_answer,
            "",
            "Это ПРОХОД 2 — сверка и финал.",
        ]
    return "\n".join(parts)


def run_pass(game_text: str, prior_json: dict = None, author_answer: str = None,
             provider_name: str = None, cache_dir: str = None) -> dict:
    """Один вызов агента: проход 1 (author_answer=None) или проход 2.

    Возвращает {available, text, json, raw, provider} либо {available: False, error}.
    """
    system = prompts.load_mirror_prompt()
    user = _build_user_message(game_text, prior_json, author_answer)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    human_text, data = _split_response(resp.text)
    return {
        "available": True,
        "text": human_text,
        "json": data,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
