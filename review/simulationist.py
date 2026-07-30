"""Агент «Симуляционист» — генерация кода скелета-игры по game_spec.core.

Третий шаг ветки анализа, сразу после приёмки структуры автором. Берёт
структурное ядро игры (`game_spec.core`) и заполняет шаблон-симулятор
`game_skeleton.py` так, чтобы тот прогонял игровой цикл тысячи раз. Полный текст
роли — review/prompts/simulationist.md.

Ключевая дисциплина агента — НЕ ВЫДУМЫВАТЬ механику, которой нет в core: если без
неё цикл не собрать, он честно объявляет игру несимулируемой (`simulatable:
false`) с причиной, а не пишет наугад. Этот модуль только собирает промпт,
вызывает LLMProvider и разбирает JSON-ответ; никакой игровой логики здесь нет.

ВАЖНО: сервис код НЕ исполняет — только показывает сгенерированный скелет автору.
Прогон в песочнице и сбор статистики — отдельный, более поздний шаг конвейера.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider


def _extract_json(raw_text: str):
    """Достаёт JSON из ответа. Симуляционист обязан вернуть ТОЛЬКО JSON, но модели
    иногда оборачивают его в ```-блок или добавляют текст вокруг. Поле `code`
    содержит целый Python-файл строкой — при корректном JSON он экранирован и
    парсится штатно."""
    raw_text = (raw_text or "").strip()
    for candidate in (
        re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_text),
        re.search(r"(\{[\s\S]*\})", raw_text),
    ):
        if candidate:
            try:
                return json.loads(candidate.group(1))
            except json.JSONDecodeError:
                continue
    return None


def _build_user_message(core: dict) -> str:
    """Вход агента: game_spec.core + шаблон game_skeleton.py (его подаёт оркестратор)."""
    return "\n".join([
        "=== GAME_SPEC.CORE (структурное ядро игры) ===",
        json.dumps(core or {}, ensure_ascii=False, indent=2),
        "",
        "=== ШАБЛОН game_skeleton.py (заполни блок СКЕЛЕТ, ДВИЖОК не трогай) ===",
        prompts.load_skeleton_template(),
    ])


def run(core: dict, provider_name: str = None, cache_dir: str = None) -> dict:
    """Один вызов симуляциониста по ядру игры.

    Возвращает при успехе:
      {available: True, simulatable: True, code, player_counts, assumptions,
       raw, provider} — заполненный скелет готов к показу;
      {available: True, simulatable: False, reason, missing, raw, provider} —
       игра честно объявлена несимулируемой;
    при сбое провайдера/парсинга:
      {available: False, error, raw?}.
    """
    system = prompts.load_simulationist_prompt()
    user = _build_user_message(core)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    data = _extract_json(resp.text)
    if data is None:
        return {"available": False,
                "error": "Симуляционист вернул ответ, который не удалось разобрать как JSON.",
                "raw": resp.text}

    result = {
        "available": True,
        "simulatable": bool(data.get("simulatable")),
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
    if result["simulatable"]:
        code = data.get("code")
        if not code or not str(code).strip():
            # Модель заявила симулируемость, но кода нет — трактуем как сбой,
            # чтобы не показывать автору пустой экран.
            return {"available": False,
                    "error": "Симуляционист пометил игру симулируемой, но не вернул код.",
                    "raw": resp.text}
        result["code"] = str(code)
        result["player_counts"] = data.get("player_counts") or []
        result["assumptions"] = data.get("assumptions") or []
    else:
        result["reason"] = data.get("reason") or "Причина не указана."
        result["missing"] = data.get("missing") or []
    return result
