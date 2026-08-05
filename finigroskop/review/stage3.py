"""Этап 3 — оркестратор содержательного анализа.

Два слоя (раздел 6 проектного документа):
  • детерминированный (бесплатно, всегда): словарь финтерминов + читабельность;
  • ИИ-рецензия (опционально, каскадом): запускается только если структура выше
    порога (Принцип 5), через абстракцию LLMProvider с изящной деградацией.

Сейчас ИИ-слой по умолчанию деградирует к NullProvider (вендор не подключён) —
это фундамент, на который позже наслаивается реальный провайдер и ИИ-агент.
"""

from analysis import glossary
from review import prompts
from review.llm_provider import get_provider

# Порог структуры (%) для запуска платной ИИ-рецензии (каскад, Принцип 5).
LLM_STRUCTURE_THRESHOLD = 70.0


def analyze(game_text: str, stage1_game: dict = None, stage1_full: dict = None,
            stage2_metrics: dict = None, target_age: int = 12,
            run_llm: bool = False, provider_name: str = None,
            cache_dir: str = None) -> dict:
    """Содержательный анализ одной игры.

    `game_text`    — полный текст игры (для словаря/читабельности и промпта)
    `stage1_game`  — отчёт этапа 1 по этой игре (структура, параметры)
    `stage1_full`  — весь отчёт этапа 1 (для контекста промпта)
    `stage2_metrics` — метрики симуляции, если есть
    `run_llm`      — пытаться ли запускать ИИ-рецензию
    """
    content = glossary.analyze_content(game_text, target_age=target_age)

    structure_pct = (stage1_game or {}).get("structure_pct", 0.0)
    review = _maybe_review(
        game_text, stage1_full, content, stage2_metrics,
        structure_pct, run_llm, provider_name, cache_dir,
    )

    return {
        "deterministic": content,
        "review": review,
        "structure_pct": structure_pct,
    }


def _maybe_review(game_text, stage1_full, content, stage2_metrics,
                  structure_pct, run_llm, provider_name, cache_dir):
    """Запускает ИИ-рецензию каскадом или объясняет, почему пропущена."""
    if not run_llm:
        return {"available": False, "reason": "ИИ-рецензия не запрашивалась.",
                "skipped": True}

    if structure_pct < LLM_STRUCTURE_THRESHOLD:
        return {"available": False, "skipped": True,
                "reason": (f"Структура {structure_pct}% ниже порога "
                           f"{LLM_STRUCTURE_THRESHOLD}% — для сырых работ "
                           f"достаточно автоотчёта этапа 1 (каскад, Принцип 5).")}

    messages = prompts.build_review_messages(game_text, stage1_full, content, stage2_metrics)
    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(messages["system"], messages["user"])

    return {
        "available": resp.available,
        "provider": resp.provider,
        "cached": resp.cached,
        "text": resp.text,
        "reason": resp.error if not resp.available else None,
    }
