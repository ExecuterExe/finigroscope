"""Агент «Оценщик статистик» (v4) — интерпретация STATS_JSON после симуляции.

Четвёртый шаг ветки анализа. На входе — статистика базового прогона скелета,
ядро игры и ТРИ поля сайдкара diagnostic_meta (tie_breaker, actions_resolution,
win_paths). На выходе — Finding_balance.json: шесть проверок с вердиктами,
красные флаги с тяжестью, два балла и приоритетные правки.
Полный текст роли — review/prompts/stats_evaluator.md.

Почему здесь, как и у извлеченца, есть валидация: у этого агента две ошибки
дорогие и при этом незаметные в отчёте.

1. Ничья, посчитанная как недостижимость победы. Симуляционист при равенстве
   без тай-брейка возвращает winner=None с причиной tie_unresolved, и это
   попадает в no_winner_rate. Если не вычесть ничьи, агент выдаст critical
   «победа недостижима» игре, где победа достигается всегда, — и авто-редизайнер
   пойдёт поднимать порог победы вместо того, чтобы дописать правило ничьей.
   Разложение — чистая арифметика, поэтому код считает его сам и сверяет.
2. Вердикт problem на «мягком» числе. Доля субъективного действия (питч,
   голосование) — артефакт допущения симулятора, а не поведение игроков.

Оба случая выглядят в отчёте как обычный обоснованный вердикт, поэтому их
ловит код, а не только самопроверка внутри промпта.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider

# Ровно шесть проверок, ни одной сверх (правило 11 промпта).
CHECK_IDS = ("seat_fairness", "win_reachability", "duration",
             "action_space", "economy_runaway", "eliminations")

# Словарь флагов из промпта.
FLAG_CODES = ("too_easy", "too_hard", "unreachable_win", "tie_unresolved", "deadlock",
              "seat_advantage", "too_long", "too_short", "high_variance", "dead_action",
              "dominant_strategy", "runaway_leader", "early_elimination", "spec_violation")

# Из какой проверки может вырасти какой флаг: «флаг без проверки-источника
# запрещён», а проверить это можно только по такому соответствию.
FLAG_SOURCE = {
    "seat_advantage": {"seat_fairness"},
    "unreachable_win": {"win_reachability"},
    "tie_unresolved": {"win_reachability"},
    "too_easy": {"win_reachability"},
    "too_hard": {"win_reachability"},
    "deadlock": {"win_reachability"},
    "too_long": {"duration"},
    "too_short": {"duration"},
    "high_variance": {"duration"},
    "dead_action": {"action_space"},
    "dominant_strategy": {"action_space"},
    "runaway_leader": {"economy_runaway"},
    "early_elimination": {"eliminations"},
    # расхождение кода и спеки может вскрыться и на ничьих, и на вылетах
    "spec_violation": {"win_reachability", "eliminations"},
}

VERDICTS = ("ok", "warn", "problem", "n/a")
SEVERITIES = ("minor", "major", "critical")
CONFIDENCES = ("measured", "assumed")

# Порог из промпта: истинная недостижимость выше — problem/critical.
UNREACHABLE_PROBLEM = 0.15


def _extract_json(raw_text: str):
    """Достаёт JSON из ответа. Агент обязан вернуть только JSON, но модели
    иногда оборачивают его в ```-блок."""
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


# --- детерминированные вычисления (их незачем поручать модели) ---------------
def split_no_winner(config: dict) -> dict:
    """Раскладывает no_winner_rate одной конфигурации на ничьи и недостижимость.

    Это арифметика, а не суждение, поэтому считаем её кодом: именно на этом
    разложении v3 ошибался и выдавал ложный critical про недостижимость победы.
    """
    reasons = config.get("end_reason_share") or {}
    no_winner = float(config.get("no_winner_rate") or 0)
    ties = float(reasons.get("tie_unresolved") or 0)
    round_cap = float(reasons.get("round_cap") or 0)
    true_unreachable = max(0.0, no_winner - ties)
    return {
        "num_players": config.get("num_players"),
        "no_winner_rate": round(no_winner, 4),
        "tie_share": round(ties, 4),
        "true_unreachable": round(true_unreachable, 4),
        "round_cap_share": round(round_cap, 4),
        # То, с чем промпт велит сравнивать пороги недостижимости.
        "unreachable_total": round(min(1.0, true_unreachable + round_cap), 4),
    }


def reachability_breakdown(stats: list) -> list:
    """Разложение по всем конфигурациям — показывается автору и идёт в промпт."""
    return [split_no_winner(c) for c in (stats or []) if isinstance(c, dict)]


def soft_actions(diagnostic_meta: dict, subjective_actions: list = None) -> list:
    """Действия, дающие «мягкие» числа: их исход смоделирован случайной величиной.

    Источник — метка subjective_judgment в diagnostic_meta.actions_resolution
    плюс список от симуляциониста, если он его дал.
    """
    resolution = (diagnostic_meta or {}).get("actions_resolution") or {}
    soft = {name for name, kind in resolution.items() if kind == "subjective_judgment"}
    soft.update(a for a in (subjective_actions or []) if a)
    return sorted(soft)


def readable_meta(diagnostic_meta: dict) -> dict:
    """Ровно три поля сайдкара, которые агенту разрешено читать.

    Отдаём именно срез, а не весь diagnostic_meta: лишние поля соблазняли бы
    завести седьмую проверку, а это работа диагноста.
    """
    meta = diagnostic_meta or {}
    return {
        "tie_breaker": meta.get("tie_breaker"),
        "actions_resolution": meta.get("actions_resolution"),
        "win_paths": meta.get("win_paths"),
    }


def _build_user_message(core: dict, stats: list, diagnostic_meta: dict,
                        assumptions: list = None, subjective_actions: list = None) -> str:
    soft = soft_actions(diagnostic_meta, subjective_actions)
    parts = [
        "=== GAME_SPEC.CORE (режим, победа, catch_up, elimination, время) ===",
        json.dumps(core or {}, ensure_ascii=False, indent=2),
        "",
        "=== STATS_JSON (список конфигураций базового прогона) ===",
        json.dumps(stats or [], ensure_ascii=False, indent=2),
        "",
        "=== DIAGNOSTIC_META (только на чтение: tie_breaker, actions_resolution, win_paths) ===",
        json.dumps(readable_meta(diagnostic_meta), ensure_ascii=False, indent=2),
        "",
        "=== ДОПУЩЕНИЯ СИМУЛЯЦИОНИСТА ===",
        json.dumps(assumptions or [], ensure_ascii=False, indent=2),
        "",
        "=== ДЕЙСТВИЯ С МЯГКИМИ ЧИСЛАМИ (исход смоделирован случайной величиной) ===",
        json.dumps(soft, ensure_ascii=False),
    ]
    if soft:
        parts.append(
            "По этим действиям максимальный вердикт — warn, тяжесть флага на ступень ниже, "
            "приоритетную правку не предлагать; наблюдение — в notes_for_lenses.")

    # Разложение считаем кодом и подаём готовым: это арифметика, и именно на ней
    # ломался v3, путая неразрешённые ничьи с недостижимой победой.
    parts += [
        "",
        "=== РАЗЛОЖЕНИЕ no_winner_rate (посчитано оркестратором, используй эти числа) ===",
        json.dumps(reachability_breakdown(stats), ensure_ascii=False, indent=2),
        "",
        "Напоминание: истинная_недостижимость = no_winner_rate − доля ничьих. "
        "Ничьи дают флаг tie_unresolved и правку в правило разрешения ничьей, "
        "а НЕ unreachable_win и НЕ правку порога победы.",
    ]
    return "\n".join(parts)


# --- самопроверка отчёта -----------------------------------------------------
def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


def validate(report: dict, stats: list = None, diagnostic_meta: dict = None,
             subjective_actions: list = None) -> tuple:
    """Прогоняет самопроверку из промпта по факту. Возвращает (report, issues).

    Ничего не «чинит»: вердикт — это суждение, и подменять его кодом значило бы
    выдумывать за агента. Задача проверки — сделать молчаливую ошибку видимой.
    """
    issues = []
    report = dict(report or {})
    checks = report.get("checks") or []
    flags = report.get("flags") or []
    soft = set(soft_actions(diagnostic_meta, subjective_actions))

    # 1) ровно шесть проверок с каноническими id
    by_id = {}
    for ch in checks:
        cid = ch.get("id")
        if cid in by_id:
            issues.append(_issue("check_duplicated", f"Проверка «{cid}» встретилась дважды."))
        by_id[cid] = ch
    for missing in [c for c in CHECK_IDS if c not in by_id]:
        issues.append(_issue(
            "check_missing",
            f"Проверка «{missing}» отсутствует. Пропущенная проверка должна быть n/a "
            "с причиной, а не исчезать из отчёта.", "error"))
    for extra in [c for c in by_id if c not in CHECK_IDS]:
        issues.append(_issue(
            "check_extra",
            f"Лишняя проверка «{extra}»: их ровно шесть, остальное — зона диагноста."))

    for ch in checks:
        if ch.get("verdict") not in VERDICTS:
            issues.append(_issue(
                "verdict_unknown",
                f"Проверка «{ch.get('id')}»: неизвестный вердикт «{ch.get('verdict')}»."))
        if ch.get("verdict") in ("warn", "problem") and not (ch.get("evidence") or "").strip():
            issues.append(_issue(
                "evidence_missing",
                f"Проверка «{ch.get('id')}» с вердиктом {ch.get('verdict')} без доказательства. "
                "Нет числа — нет вердикта.", "error"))
        # 2) problem не может опираться на мягкое число
        if ch.get("verdict") == "problem" and ch.get("confidence") == "assumed":
            issues.append(_issue(
                "problem_on_assumed",
                f"Проверка «{ch.get('id')}»: вердикт problem на мягком числе. "
                "По числам, полученным из смоделированной случайностью механики, "
                "максимум warn.", "error"))

    # 3) флаги: код из словаря, confidence проставлен, есть проверка-источник
    problem_checks = {c.get("id") for c in checks if c.get("verdict") == "problem"}
    warn_or_worse = {c.get("id") for c in checks if c.get("verdict") in ("warn", "problem")}
    flagged_sources = set()
    for fl in flags:
        code = fl.get("code")
        if code not in FLAG_CODES:
            issues.append(_issue("flag_unknown", f"Флаг «{code}» вне словаря флагов."))
        if fl.get("confidence") not in CONFIDENCES:
            issues.append(_issue(
                "flag_confidence_missing",
                f"У флага «{code}» не проставлен confidence (measured|assumed).", "error"))
        if fl.get("severity") not in SEVERITIES:
            issues.append(_issue(
                "flag_severity_unknown",
                f"У флага «{code}» неизвестная тяжесть «{fl.get('severity')}»."))
        if fl.get("confidence") == "assumed" and fl.get("severity") == "critical":
            issues.append(_issue(
                "critical_on_assumed",
                f"Флаг «{code}»: тяжесть critical на мягком числе недостижима в принципе.",
                "error"))
        sources = FLAG_SOURCE.get(code, set())
        if sources and not (sources & warn_or_worse):
            issues.append(_issue(
                "flag_without_check",
                f"Флаг «{code}» не вырастает ни из одной проверки со статусом warn/problem "
                f"(ожидались: {', '.join(sorted(sources))}).", "error"))
        flagged_sources |= sources

    for cid in problem_checks - flagged_sources:
        issues.append(_issue(
            "problem_without_flag",
            f"Проверка «{cid}» с вердиктом problem не дала ни одного флага.", "error"))

    # 4) главная ловушка v3: ничьи, посчитанные как недостижимость победы
    breakdown = reachability_breakdown(stats or [])
    ties_exist = any(b["tie_share"] > 0 for b in breakdown)
    max_true_unreachable = max((b["unreachable_total"] for b in breakdown), default=0.0)
    unreachable_flags = [f for f in flags if f.get("code") == "unreachable_win"]
    if unreachable_flags and ties_exist and max_true_unreachable <= UNREACHABLE_PROBLEM:
        issues.append(_issue(
            "tie_counted_as_unreachable",
            "Выставлен флаг unreachable_win, хотя после вычета ничьих истинная "
            f"недостижимость = {max_true_unreachable:.1%} (порог {UNREACHABLE_PROBLEM:.0%}). "
            "Похоже, неразрешённые ничьи посчитаны недостижимой победой — правка уйдёт "
            "в порог победы вместо правила тай-брейка.", "error"))
    if ties_exist and not any(f.get("code") in ("tie_unresolved", "spec_violation") for f in flags):
        worst_tie = max(b["tie_share"] for b in breakdown)
        if worst_tie > 0.03:
            issues.append(_issue(
                "tie_not_flagged",
                f"Доля неразрешённых ничьих доходит до {worst_tie:.1%}, но флага "
                "tie_unresolved нет.", "error" if worst_tie > 0.10 else "warning"))

    # ничьи при tie_breaker.applicable = false — расхождение кода и спеки
    tie_applicable = ((diagnostic_meta or {}).get("tie_breaker") or {}).get("applicable")
    if ties_exist and tie_applicable is False and not any(
            f.get("code") == "spec_violation" for f in flags):
        issues.append(_issue(
            "tie_vs_spec",
            "В спеке равенство объявлено невозможным (tie_breaker.applicable=false), "
            "а симуляция даёт ничьи — ожидался флаг spec_violation."))

    # 5) правки: конкретное поле core, направление, и ничего по мягким числам
    assumed_flags = {f.get("code") for f in flags if f.get("confidence") == "assumed"}
    for fix in report.get("priority_fixes") or []:
        target = (fix.get("target") or "").strip()
        if not target.startswith("core"):
            issues.append(_issue(
                "fix_target_vague",
                f"Правка «{target or '—'}» не указывает поле core — авто-редизайнер не "
                "сможет её применить."))
        if not (fix.get("direction") or "").strip():
            issues.append(_issue("fix_direction_missing",
                                 f"У правки «{target}» не указано направление."))
        closes = {c.strip() for c in (fix.get("closes") or "").split(",") if c.strip()}
        if closes & assumed_flags:
            issues.append(_issue(
                "fix_on_assumed",
                f"Правка «{target}» закрывает флаг на мягком числе "
                f"({', '.join(sorted(closes & assumed_flags))}) — чинить механику по "
                "артефакту допущения бессмысленно.", "error"))

    # 6) кооператив: перекос по местам не считается
    if (report.get("mode") == "cooperative"
            and (by_id.get("seat_fairness") or {}).get("verdict") not in (None, "n/a")):
        issues.append(_issue(
            "coop_seat_fairness",
            "Режим кооперативный, но проверка seat_fairness не помечена n/a: "
            "winner там маркирует команду, и перекос мест — артефакт.", "error"))

    # 7) баллы и покрытые тесты
    scores = report.get("scores") or {}
    for key in ("balance", "win_reachability"):
        val = scores.get(key)
        if not isinstance(val, (int, float)) or not (1 <= val <= 10):
            issues.append(_issue("score_invalid",
                                 f"Балл «{key}» = {val!r}: ожидалось число 1–10."))
    if not report.get("covered_tests"):
        issues.append(_issue(
            "covered_tests_missing",
            "Не заполнен covered_tests — диагност будет пересчитывать уже закрытое."))

    # 8) конфигурации: проверки прогоняются по каждому числу игроков
    stats_configs = [c.get("num_players") for c in (stats or []) if isinstance(c, dict)]
    listed = report.get("configs_analyzed") or []
    if stats_configs and sorted(x for x in listed if x is not None) != sorted(
            x for x in stats_configs if x is not None):
        issues.append(_issue(
            "configs_mismatch",
            f"configs_analyzed={listed}, а в STATS_JSON конфигурации {stats_configs}."))

    return report, issues


def run(core: dict, stats: list, diagnostic_meta: dict = None,
        assumptions: list = None, subjective_actions: list = None,
        provider_name: str = None, cache_dir: str = None) -> dict:
    """Один вызов оценщика статистик.

    Возвращает {available, report, issues, raw, provider} либо
    {available: False, error}.
    """
    if not stats:
        return {"available": False,
                "error": "Нет STATS_JSON: сначала нужно прогнать скелет-симулятор."}

    system = prompts.load_stats_evaluator_prompt()
    user = _build_user_message(core, stats, diagnostic_meta, assumptions, subjective_actions)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    report = _extract_json(resp.text)
    if report is None:
        return {"available": False,
                "error": "Оценщик вернул ответ, который не удалось разобрать как JSON.",
                "raw": resp.text}

    report, issues = validate(report, stats, diagnostic_meta, subjective_actions)
    return {
        "available": True,
        "report": report,
        "issues": issues,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
