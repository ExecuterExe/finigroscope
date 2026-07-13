"""Сборка промптов ИИ-рецензента (раздел 6.2: в промпт подаются факты этапов 1-2).

Системный промпт — мастер-анализатор (линзы Шелла + антипаттерны). Скелет
симулятора (Приложение B) подставляется из единого источника
`simulation/templates/game_skeleton.py`, чтобы не держать две копии кода.

Пользовательское сообщение собирается из установленных фактов: текст правил +
результаты этапа 1 (структура, извлечённые параметры) + метрики этапа 2, если
они есть. Рецензент комментирует факты, а не фантазирует с нуля — это режет
галлюцинации и стоимость.
"""

import json
import os

_HERE = os.path.dirname(__file__)
_PROMPTS_DIR = os.path.join(_HERE, "prompts")
_SKELETON_PATH = os.path.join(
    os.path.dirname(_HERE), "simulation", "templates", "game_skeleton.py"
)

_SKELETON_MARKER = "{{APPENDIX_B_SKELETON}}"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_skeleton_template() -> str:
    """Текст шаблона симулятора (альтернативный путь этапа 2)."""
    return _read(_SKELETON_PATH)


def load_master_prompt(embed_skeleton: bool = True) -> str:
    """Системный промпт мастер-анализатора. По желанию — со встроенным скелетом."""
    text = _read(os.path.join(_PROMPTS_DIR, "master_analyzer.md"))
    if embed_skeleton and _SKELETON_MARKER in text:
        text = text.replace(_SKELETON_MARKER, load_skeleton_template())
    return text


def load_balance_prompt() -> str:
    """Промпт-компаньон оценки технического баланса (Приложение C)."""
    return _read(os.path.join(_PROMPTS_DIR, "balance_eval.md"))


# --- сборка фактов этапов 1-2 в текст --------------------------------------
def _format_stage1(stage1: dict) -> str:
    if not stage1:
        return "Этап 1 (документный анализ): данных нет."
    lines = [f"Этап 1 — документный анализ ({stage1.get('doc_type_title', '')}):",
             f"  игр в файле: {stage1.get('games_count', '?')}"]
    for game in stage1.get("games", []):
        lines.append(f"  • {game.get('title', 'Игра')}: структура "
                     f"{game.get('structure_pct')}% ({game.get('found_count')}/{game.get('total')} разделов), "
                     f"замечаний: {game.get('warnings')}")
        params = (game.get("params") or {}).get("values", {})
        if params:
            bits = []
            if params.get("players"):
                bits.append(f"игроки {params['players']['min']}-{params['players']['max']}")
            if params.get("duration"):
                bits.append(f"{params['duration']['min']}-{params['duration']['max']} мин")
            if params.get("win_condition"):
                bits.append("есть условие победы")
            if params.get("end_condition"):
                bits.append("есть условие окончания")
            if bits:
                lines.append("    извлечено: " + ", ".join(bits))
        missing = (game.get("params") or {}).get("missing", [])
        if missing:
            lines.append("    НЕ извлечено: " + ", ".join(missing))
    return "\n".join(lines)


def _format_content(content: dict) -> str:
    if not content:
        return ""
    g = content.get("glossary", {})
    r = content.get("readability", {})
    lines = ["Детерминированный слой этапа 3:"]
    if g:
        lines.append(f"  термины ФГ: найдено {g.get('distinct_count')} тем "
                     f"(плотность {g.get('density_per_1000')}/1000 слов)")
    if r and r.get("available"):
        lines.append(f"  читабельность: ~{r.get('estimated_age')} лет vs цель {r.get('target_age')}+"
                     f"{' (СЛОЖНЕЕ заявленной аудитории)' if r.get('too_complex') else ''}")
    return "\n".join(lines)


def _format_stage2(stage2_metrics: dict) -> str:
    if not stage2_metrics:
        return ("Этап 2 (симуляция): не запускалась. Баллы «Экономика и баланс» и "
                "«достижимость победы» оцени предварительно по документу.")
    return "Этап 2 — метрики симуляции (JSON):\n" + json.dumps(
        stage2_metrics, ensure_ascii=False, indent=2)


def build_user_message(game_text: str, stage1: dict = None,
                       content: dict = None, stage2_metrics: dict = None) -> str:
    """Пользовательское сообщение: правила игры + установленные факты этапов 1-2."""
    parts = ["=== УСТАНОВЛЕННЫЕ ФАКТЫ (комментируй их, не выдумывай новое) ==="]
    parts.append(_format_stage1(stage1))
    content_block = _format_content(content)
    if content_block:
        parts.append(content_block)
    parts.append(_format_stage2(stage2_metrics))
    parts.append("\n=== ТЕКСТ ПРАВИЛ ИГРЫ ===")
    parts.append(game_text or "(текст правил не передан)")
    return "\n".join(parts)


def build_review_messages(game_text: str, stage1: dict = None,
                          content: dict = None, stage2_metrics: dict = None) -> dict:
    """Готовая пара system/user для вызова LLMProvider.complete()."""
    return {
        "system": load_master_prompt(embed_skeleton=True),
        "user": build_user_message(game_text, stage1, content, stage2_metrics),
    }
