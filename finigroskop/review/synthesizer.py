"""Агент «Синтезатор оценки» (v2) — финал анализа.

Последний шаг конвейера: сводит качественные баллы линз, числовой отчёт баланса
и вердикты диагноста в общий балл, оценивает надёжность собственного вывода и
пишет отчёт автору. Полный текст роли — review/prompts/synthesizer.md.

Он ничего не оценивает заново — агрегирует и объясняет. Оркестратор читает у
него `revision_required` (а НЕ сам балл) и `overall_score`.

Почему арифметика считается здесь, а не только у модели. Промпт требует, чтобы
балл был «воспроизводим читателем вручную»: Σ(балл × вес) / Σ(весов), доля
непроверенного, стоимость каждой находки, потолки снижения. Это правила, а не
суждение, и модель на них ошибается тихо — сумма сходится «на глаз», а число
уезжает. Поэтому код считает эталон сам, отдаёт его агенту в промпт и потом
сверяет ответ. Суждение (что важно, как объяснить, что в приоритетах) остаётся
за моделью.

САМОЕ ВАЖНОЕ ПОЛЕ — `revision_required`. По нему оркестратор решает, гнать ли
игру на новый круг. Оно обязано быть true не только при балле ниже шести, но и
при незакрытой критичной находке: игра может набрать проходные 6.4 за счёт
сильной темы, имея рабочую эксплойт-петлю. Поэтому код считает его сам и
сверяет — ошибиться здесь дороже всего.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider

# Веса категорий. Имена совпадают с теми, что использует агент линз, —
# сопоставление идёт ПО ИМЕНИ, и рассинхрон означает молчаливое выпадение
# категории из расчёта (именно это чинил пункт 7 changelog v2).
CATEGORY_WEIGHTS = {
    "Замысел и образовательная ценность": 1.5,
    "Экономика и баланс": 1.5,
    "Структурная целостность и физический интерфейс": 1.25,
    "Целостность дизайна и цели": 1.0,
    "Механика и решения игрока": 1.0,
    "Навык, шанс, напряжение": 1.0,
    "Игрок, доступность, вовлечение": 1.0,
    "Реиграбельность и нарратив": 1.0,
    "Социальное взаимодействие": 0.75,
}

# Категория методички → категория синтеза.
DIAGNOST_CATEGORY_MAP = {
    1: "Структурная целостность и физический интерфейс",
    2: "Структурная целостность и физический интерфейс",
    3: "Навык, шанс, напряжение",
    4: "Экономика и баланс",
    5: "Игрок, доступность, вовлечение",
    6: "Структурная целостность и физический интерфейс",
    7: "Социальное взаимодействие",
    8: "Замысел и образовательная ценность",
    9: "Экономика и баланс",
    10: "Структурная целостность и физический интерфейс",
    11: "Реиграбельность и нарратив",
    12: "Игрок, доступность, вовлечение",
    13: "Экономика и баланс",
    14: "Навык, шанс, напряжение",
}

# Стоимость находки по тяжести.
SEVERITY_COST = {"critical": 3.0, "major": 2.0, "minor": 0.5, "warning": 0.5}

# Потолки накопления: иначе три пересекающиеся находки об одной проблеме
# обнулят категорию трижды за одно и то же.
MAX_CATEGORY_PENALTY = 5.0
MIN_CATEGORY_SCORE = 1.0
CRITICAL_CATEGORY_CAP = 5.0     # категория с critical не может быть выше пяти

# Причины n/a, которые НЕ снижают доверие: механики нет — проверять нечего.
NEUTRAL_NA = ("mechanic_absent",)
UNMEASURED_NA = ("no_data", "method_blind", "search_incomplete", "budget_exceeded")

# Границы из методички: «менее 10%» — high, «10–30%» — medium, «более 30%» — low.
# Края включены в medium в обе стороны — ровно так, как написано в таблице.
CONFIDENCE_HIGH_BELOW = 0.10
CONFIDENCE_MEDIUM_UPTO = 0.30

PASSING_SCORE = 6.0

DISCLAIMER = (
    "Всё вышеизложенное — предложения, а не предписания. Вы знаете свою игру "
    "лучше: если какая-то правка, на ваш взгляд, сделает игру хуже, противоречит "
    "замыслу или вовсе её ломает — не применяйте её. Оценка и рекомендации — "
    "повод подумать и проверить, а не обязательная инструкция."
)

# Синтезатор — тяжёлый вызов: три отчёта на входе и длинный report_md на выходе.
LLM_TIMEOUT = 300


def _extract_json(raw_text: str):
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


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


# --- детерминированные расчёты (эталон для сверки) ---------------------------
def is_soft(finding: dict, subjective_actions=None) -> bool:
    """Мягкая находка: измеряли допущение симулятора, а не игру."""
    if (finding or {}).get("confidence") == "assumed":
        return True
    subj = set(subjective_actions or [])
    if not subj:
        return False
    blob = " ".join(str(finding.get(k) or "") for k in ("detail", "where", "code", "evidence"))
    return any(a and a in blob for a in subj)


def finding_cost(finding: dict, subjective_actions=None) -> float:
    """Стоимость находки в баллах категории (отрицательное число).

    Мягкая находка и находка, проявляющаяся лишь при части конфигураций, стоят
    вдвое меньше. Оба множителя применяются, если оба условия выполнены.
    """
    base = SEVERITY_COST.get((finding or {}).get("severity"), 0.0)
    if not base:
        return 0.0
    if is_soft(finding, subjective_actions):
        base /= 2
    configs = finding.get("affected_configs")
    if configs and finding.get("all_configs") is not True:
        base /= 2
    return -round(base, 4)


def _band(share: float) -> str:
    if share < CONFIDENCE_HIGH_BELOW:
        return "high"
    return "medium" if share <= CONFIDENCE_MEDIUM_UPTO else "low"


def coverage_share(coverage_summary: dict, category_scores: dict = None) -> dict:
    """Доля непроверенного + уровень доверия к баллу.

    Считаются ДВА разрыва, и берётся худший.

    Первый — доля невыполненных тестов среди ПРИМЕНИМЫХ (формула методички).
    Ключевое здесь: `mechanic_absent` вычитается из знаменателя, а не считается
    непроверенным. Механики нет — тест неприменим, и это нормально.

    Второй — доля веса категорий, которые не оценивались вовсе. Промпт задаёт
    только первый, но без второго получается прямая ложь: когда «Оценщика по
    линзам» ещё нет, восемь категорий из девяти уходят в N/A, а доверие
    остаётся «high», потому что тесты-то диагност выполнил. Балл, собранный по
    одной категории из девяти, надёжным назвать нельзя — это ровно тот тихий
    сдвиг, который весь конвейер и ловит. Оба числа возвращаются раздельно,
    чтобы решение можно было перепроверить.
    """
    cs = coverage_summary or {}
    total = int(cs.get("tests_total") or 0)
    br = cs.get("n_a_breakdown") or {}
    absent = sum(int(br.get(k) or 0) for k in NEUTRAL_NA)
    unmeasured = sum(int(br.get(k) or 0) for k in UNMEASURED_NA)
    applicable = max(0, total - absent)
    share = (unmeasured / applicable) if applicable else 0.0

    scored = sum(w for c, w in CATEGORY_WEIGHTS.items()
                 if (category_scores or {}).get(c, {}).get("score") is not None)
    all_weight = sum(CATEGORY_WEIGHTS.values())
    cat_share = (all_weight - scored) / all_weight if category_scores is not None else 0.0

    levels = {"high": 0, "medium": 1, "low": 2}
    level = max(_band(share), _band(cat_share), key=levels.get)
    return {
        "tests_total": total, "applicable": applicable, "unmeasured": unmeasured,
        "share": round(share, 4), "tests_confidence": _band(share),
        "categories_unscored_share": round(cat_share, 4),
        "categories_confidence": _band(cat_share),
        "confidence": level,
        "breakdown": {k: int(br.get(k) or 0) for k in UNMEASURED_NA if br.get(k)},
    }


def diagnost_penalties(findings_diagnost: dict, subjective_actions=None) -> list:
    """Переводит вердикты диагноста в снижения по категориям синтеза."""
    out = []
    for r in (findings_diagnost or {}).get("results") or []:
        if r.get("verdict") not in ("warning", "problem"):
            continue
        sev = r.get("severity")
        if sev in (None, "n/a"):
            sev = "major" if r.get("verdict") == "problem" else "minor"
        try:
            cat_no = int(str(r.get("test", "")).split(".")[0])
        except (ValueError, IndexError):
            continue
        category = DIAGNOST_CATEGORY_MAP.get(cat_no)
        if not category:
            continue
        item = dict(r, severity=sev)
        out.append({
            "source": "diagnost", "test": r.get("test"), "severity": sev,
            "confidence": r.get("confidence", "measured"),
            "category": category, "cost": finding_cost(item, subjective_actions),
            "detail": r.get("evidence") or r.get("name") or "",
        })
    return out


def balance_penalties(finding_balance: dict, subjective_actions=None) -> list:
    """Флаги «Оценщика статистик» → снижения. Категорию берём консервативно."""
    out = []
    for f in (finding_balance or {}).get("flags") or []:
        out.append({
            "source": "balance", "code": f.get("code"), "severity": f.get("severity"),
            "confidence": f.get("confidence", "measured"),
            "category": "Экономика и баланс",
            "cost": finding_cost(f, subjective_actions),
            "detail": f.get("detail") or "",
        })
    return out


def apply_penalties(base_scores: dict, penalties: list) -> dict:
    """Считает итоговые баллы категорий с потолками накопления.

    Возвращает {category: {base, penalty, score, capped_by_critical}}.
    """
    out = {}
    for cat, base in (base_scores or {}).items():
        if base is None:
            continue
        rows = [p for p in penalties if p.get("category") == cat]
        penalty = sum(p.get("cost") or 0.0 for p in rows)
        penalty = max(penalty, -MAX_CATEGORY_PENALTY)      # не глубже -5
        score = float(base) + penalty
        has_critical = any(p.get("severity") == "critical" for p in rows)
        if has_critical:
            score = min(score, CRITICAL_CATEGORY_CAP)
        score = max(score, MIN_CATEGORY_SCORE)             # не ниже 1
        out[cat] = {"base": float(base), "penalty": round(penalty, 4),
                    "score": round(score, 4), "capped_by_critical": has_critical}
    return out


def overall_score(category_scores: dict) -> dict:
    """Σ(балл × вес) / Σ(весов оценённых категорий), три знака.

    Категории с N/A в обе суммы не входят — это и есть правило «N/A не ноль».
    """
    rows, weighted, weights = [], 0.0, 0.0
    for cat, weight in CATEGORY_WEIGHTS.items():
        info = (category_scores or {}).get(cat)
        if not info or info.get("score") is None:
            rows.append({"category": cat, "na": True, "weight": weight})
            continue
        product = round(float(info["score"]) * weight, 4)
        rows.append({"category": cat, "na": False, "score": float(info["score"]),
                     "weight": weight, "product": product})
        weighted += product
        weights += weight
    score = round(weighted / weights, 3) if weights else None
    return {"rows": rows, "sum_weighted": round(weighted, 3),
            "sum_weights": round(weights, 3), "overall": score}


def revision_decision(score, findings_diagnost: dict, finding_balance: dict) -> dict:
    """Три условия обязательной ревизии. Считает КОД: по этому полю оркестратор
    решает, гнать ли игру на новый круг, и ошибка здесь дороже всего.

    Второе и третье условия важны отдельно от балла: проходные 6.4 при рабочей
    эксплойт-петле — хуже, чем заниженный балл.
    """
    reasons = []
    if score is not None and score < PASSING_SCORE:
        reasons.append(f"overall_score = {score} < {PASSING_SCORE:g}")
    diag_crit = [f for f in (findings_diagnost or {}).get("critical_flags") or []
                 if f.get("severity") == "critical"]
    if diag_crit:
        codes = ", ".join(sorted({f.get("code") or "?" for f in diag_crit}))
        reasons.append(f"незакрытые критичные находки диагноста: {codes}")
    bal_crit = [f for f in (finding_balance or {}).get("flags") or []
                if f.get("severity") == "critical"]
    if bal_crit:
        codes = ", ".join(sorted({f.get("code") or "?" for f in bal_crit}))
        reasons.append(f"критичные флаги баланса: {codes}")
    return {"required": bool(reasons), "reasons": reasons}


def compute_reference(lenses: dict, finding_balance: dict, findings_diagnost: dict,
                      sim_meta: dict = None) -> dict:
    """Эталонный расчёт целиком: баллы категорий, общий балл, доверие, ревизия."""
    sim_meta = sim_meta or {}
    subj = sim_meta.get("subjective_actions") or []

    base = {}
    for c in (lenses or {}).get("categories") or []:
        name = c.get("name")
        if name not in CATEGORY_WEIGHTS:
            continue
        base[name] = None if c.get("na") else c.get("category_avg_preliminary")

    # «Экономика и баланс» считается на данных, а не на впечатлении.
    scores = (finding_balance or {}).get("scores") or {}
    if scores.get("balance") is not None:
        base["Экономика и баланс"] = scores["balance"]

    penalties = (diagnost_penalties(findings_diagnost, subj)
                 + balance_penalties(finding_balance, subj))
    cats = apply_penalties({k: v for k, v in base.items() if v is not None}, penalties)
    total = overall_score(cats)
    cov = coverage_share((findings_diagnost or {}).get("coverage_summary"), cats)
    rev = revision_decision(total["overall"], findings_diagnost, finding_balance)

    # Находка по НЕОЦЕНЁННОЙ категории балл снизить не может — снижать нечего.
    # Но исчезнуть она не имеет права: это реальная проблема игры, просто
    # оценивать её было нечем. Держим такие отдельно, чтобы отчёт их назвал.
    applied = [p for p in penalties if p.get("category") in cats]
    unscored = [p for p in penalties if p.get("category") not in cats]
    return {"category_scores": cats, "total": total, "coverage": cov,
            "revision": rev, "penalties": applied,
            "penalties_outside_scored": unscored}


# --- сборка входа ------------------------------------------------------------
def build_input(lenses: dict, finding_balance: dict, findings_diagnost: dict,
                stats: list = None, sim_meta: dict = None,
                redesign_not_touched: list = None,
                redesign_recommendations: list = None,
                attempt_number: int = 1, previous_score=None,
                attempts_left: int = None) -> dict:
    return {
        "lenses": lenses,
        "finding_balance": finding_balance or {},
        "findings_diagnost": findings_diagnost or {},
        "stats": stats,
        "sim_meta": sim_meta or {},
        "not_touched": redesign_not_touched or [],
        "redesign_recommendations": redesign_recommendations or [],
        "attempt_number": attempt_number,
        "previous_score": previous_score,
        "attempts_left": attempts_left,
    }


def build_message(data: dict, reference: dict) -> str:
    parts = [
        "=== FINDINGS_LENSES.JSON (качественная оценка) ===",
        json.dumps(data["lenses"], ensure_ascii=False, indent=2) if data.get("lenses")
        else "ОТСУТСТВУЕТ: оценка по линзам не выполнялась. Категории, которые "
             "считаются только по линзам, обязаны быть помечены na: true и исключены "
             "из обеих сумм формулы — но НЕ обнулены. В резюме скажи прямо, что "
             "качественная сторона не оценивалась и балл поэтому частичный.",
        "",
        "=== FINDING_BALANCE.JSON (числовой баланс) ===",
        json.dumps(data["finding_balance"], ensure_ascii=False, indent=2),
        "",
        "=== FINDINGS_DIAGNOST.JSON (вердикты методички) ===",
        json.dumps(data["findings_diagnost"], ensure_ascii=False, indent=2),
        "",
        "=== STATS_JSON (числа для обоснований) ===",
        json.dumps(data["stats"], ensure_ascii=False, indent=2) if data.get("stats")
        else "ОТСУТСТВУЕТ: симуляция не прошла. Балл баланса предварительный — "
             "выстави balance_score_preliminary: true и скажи об этом в резюме.",
        "",
        "=== МЕТАДАННЫЕ СИМУЛЯЦИОНИСТА (границы модели) ===",
        json.dumps(data["sim_meta"], ensure_ascii=False, indent=2),
        "",
        "=== ОТЛОЖЕННЫЕ НАХОДКИ АВТО-РЕДИЗАЙНЕРА (not_touched) ===",
        json.dumps(data["not_touched"], ensure_ascii=False, indent=2),
        "",
        "=== ПЕРЕДАНО В РЕКОМЕНДАЦИИ (handed_to_recommendations) ===",
        json.dumps(data["redesign_recommendations"], ensure_ascii=False, indent=2),
        "",
        f"=== ПОПЫТКА № {data['attempt_number']}"
        + (f", балл предыдущей итерации: {data['previous_score']}"
           if data.get("previous_score") is not None else "") + " ===",
    ]

    # Эталонный расчёт подаём готовым: это арифметика и правила, а не суждение,
    # и модель на них ошибается тихо — сумма «сходится», а число уезжает.
    total = reference["total"]
    cov = reference["coverage"]
    lines = []
    for row in total["rows"]:
        if row["na"]:
            lines.append(f"  {row['category']:<48} N/A — исключена из обеих сумм")
        else:
            lines.append(f"  {row['category']:<48} {row['score']:>5.2f} × {row['weight']:<4} "
                         f"= {row['product']:>7.3f}")
    parts += [
        "",
        "=== ЭТАЛОННЫЙ РАСЧЁТ (посчитан оркестратором — используй эти числа) ===",
        "\n".join(lines),
        f"  Σ(взвешенных) = {total['sum_weighted']} ; Σ(весов) = {total['sum_weights']}",
        f"  Общий балл = {total['overall']}",
        "",
        "Снижения по находкам (уже с учётом мягкости и частичных конфигураций):",
        json.dumps(reference["penalties"], ensure_ascii=False, indent=2),
        "",
        f"Доверие: применимых тестов {cov['applicable']}, не выполнено "
        f"{cov['unmeasured']} ({cov['share']:.0%}) → {cov['tests_confidence']}; "
        f"не оценено категорий по весу {cov['categories_unscored_share']:.0%} → "
        f"{cov['categories_confidence']}. Итоговый score_confidence — худший из "
        f"двух: {cov['confidence']}. Балл, собранный по части категорий, надёжным "
        "называть нельзя, даже если тесты диагноста выполнены полностью.",
        "",
        f"Ревизия: revision_required = "
        f"{str(reference['revision']['required']).lower()}"
        + (" — " + "; ".join(reference["revision"]["reasons"])
           if reference["revision"]["reasons"] else " (условия не сработали)"),
    ]

    outside = reference.get("penalties_outside_scored") or []
    if outside:
        parts += [
            "",
            "=== НАХОДКИ, КОТОРЫЕ НЕ ПОВЛИЯЛИ НА БАЛЛ ===",
            f"Их {len(outside)}. Они относятся к категориям, которые в этом прогоне "
            "не оценивались, — снижать было нечего, поэтому в формулу они не вошли. "
            "Это НЕ значит, что проблем нет: они реальны и измерены. Назови их в "
            "отчёте отдельным блоком и скажи прямо, что балл их не отражает.",
            json.dumps(outside, ensure_ascii=False, indent=2),
        ]

    if data.get("attempts_left") is not None:
        if data["attempts_left"] <= 0:
            parts += [
                "",
                "ВНИМАНИЕ: бюджет попыток авто-редизайна ИСЧЕРПАН. Если ревизия всё ещё "
                "требуется, отчёт всё равно выдаётся автору — с честным пояснением, какие "
                "находки остались незакрытыми и почему они не чинятся параметрами "
                "(часть проблем требует решения автора, а не подстройки числа).",
            ]
        else:
            parts += ["", f"Попыток авто-редизайна осталось: {data['attempts_left']}."]

    parts += [
        "",
        "Верни один JSON с машинными полями и полным отчётом в report_md. "
        "Дисклеймер приведи ДОСЛОВНО.",
    ]
    return "\n".join(parts)


# --- валидация ---------------------------------------------------------------
def validate(report: dict, reference: dict, lenses: dict = None,
             sim_meta: dict = None) -> list:
    """Сверяет ответ агента с эталонным расчётом. Ничего не чинит."""
    issues = []
    report = report or {}
    sim_meta = sim_meta or {}
    ref_total = reference["total"]
    ref_cov = reference["coverage"]
    ref_rev = reference["revision"]

    # 1) общий балл обязан сходиться с эталоном
    got = report.get("overall_score")
    want = ref_total["overall"]
    if want is None:
        if got is not None:
            issues.append(_issue("score_without_categories",
                                 "Выставлен балл, хотя ни одна категория не оценена.", "error"))
    elif got is None:
        issues.append(_issue("score_missing", "Нет поля overall_score.", "error"))
    elif abs(float(got) - want) > 0.011:
        issues.append(_issue(
            "score_mismatch",
            f"overall_score = {got}, а по формуле Σ({ref_total['sum_weighted']}) / "
            f"Σ({ref_total['sum_weights']}) = {want}. Балл обязан быть воспроизводим "
            "вручную из таблицы категорий.", "error"))

    # 2) N/A не превращён в ноль и не попал в суммы
    ref_na = {r["category"] for r in ref_total["rows"] if r["na"]}
    for c in report.get("categories") or []:
        name = c.get("name")
        if name in ref_na and not c.get("na"):
            issues.append(_issue(
                "na_category_scored",
                f"Категория «{name}» не оценивалась (N/A), но получила балл "
                f"{c.get('score')}. Занижать за то, что нельзя было оценить, запрещено.",
                "error"))
        if c.get("na") and c.get("score") == 0:
            issues.append(_issue("na_as_zero",
                                 f"Категория «{name}» помечена N/A и получила ноль. "
                                 "N/A не равно нулю.", "error"))
        if not c.get("na") and name in CATEGORY_WEIGHTS:
            if c.get("weight") is not None and abs(
                    float(c["weight"]) - CATEGORY_WEIGHTS[name]) > 1e-6:
                issues.append(_issue("weight_wrong",
                                     f"У категории «{name}» вес {c.get('weight')}, "
                                     f"а должен быть {CATEGORY_WEIGHTS[name]}."))
            score = c.get("score")
            if score is not None and not (MIN_CATEGORY_SCORE <= float(score) <= 10):
                issues.append(_issue("category_out_of_range",
                                     f"Балл категории «{name}» = {score} вне диапазона "
                                     f"{MIN_CATEGORY_SCORE}–10.", "error"))
        if name and name not in CATEGORY_WEIGHTS:
            issues.append(_issue(
                "category_unknown",
                f"Категория «{name}» не из фиксированного списка — по имени она не "
                "найдётся и молча выпадет из расчёта."))

    # 3) доверие считается по ПРИМЕНИМЫМ тестам
    if report.get("score_confidence") != ref_cov["confidence"]:
        issues.append(_issue(
            "confidence_mismatch",
            f"score_confidence = «{report.get('score_confidence')}», а по доле "
            f"непроверенного {ref_cov['share']:.0%} от {ref_cov['applicable']} применимых "
            f"тестов должно быть «{ref_cov['confidence']}». Причина mechanic_absent в "
            "знаменатель не входит.", "error"))
    gap = ref_cov["unmeasured"] or ref_cov.get("categories_unscored_share")
    if gap and not (report.get("confidence_note") or "").strip():
        issues.append(_issue(
            "confidence_note_missing",
            f"Не выполнено тестов: {ref_cov['unmeasured']}; не оценено категорий по весу: "
            f"{(ref_cov.get('categories_unscored_share') or 0):.0%}. При таком разрыве "
            "confidence_note не может быть пустым.", "error"))

    # 4) ГЛАВНОЕ: решение о ревизии
    if bool(report.get("revision_required")) != ref_rev["required"]:
        issues.append(_issue(
            "revision_mismatch",
            f"revision_required = {report.get('revision_required')}, а по трём условиям "
            f"должно быть {ref_rev['required']}"
            + (" (" + "; ".join(ref_rev["reasons"]) + ")" if ref_rev["reasons"] else "")
            + ". По этому полю оркестратор решает, гнать ли игру на новый круг.", "error"))
    if ref_rev["required"] and not (report.get("revision_reason") or []):
        issues.append(_issue("revision_reason_missing",
                             "Ревизия требуется, но revision_reason пуст.", "error"))

    # 5) каждое снижение привязано к находке с указанной стоимостью
    for f in report.get("findings_applied") or []:
        if f.get("cost") in (None, 0):
            issues.append(_issue("finding_without_cost",
                                 f"У находки «{f.get('code') or f.get('test')}» не указана "
                                 "стоимость снижения."))
        if not (f.get("detail") or "").strip():
            issues.append(_issue("finding_without_detail",
                                 f"У находки «{f.get('code') or f.get('test')}» нет "
                                 "доказательства."))
        if f.get("category") and f["category"] not in CATEGORY_WEIGHTS:
            issues.append(_issue("finding_category_unknown",
                                 f"Находка отнесена к категории «{f['category']}», которой "
                                 "нет в списке."))

    # 6) мягкие находки не могут быть приоритетом для автора
    subj = sim_meta.get("subjective_actions") or []
    soft_marks = {str(f.get("code") or f.get("test") or "")
                  for f in (report.get("findings_applied") or [])
                  if is_soft(f, subj)}
    soft_marks |= {str(p.get("code") or p.get("test") or "")
                   for p in reference["penalties"] if is_soft(p, subj)}
    soft_marks.discard("")
    for pr in report.get("top_priorities") or []:
        hit = sorted(m for m in soft_marks if m and m in str(pr))
        if hit:
            issues.append(_issue(
                "soft_in_priorities",
                f"В топ-приоритеты попала мягкая находка ({', '.join(hit)}). Она измеряла "
                "допущение симулятора, а не игру — чинить по ней механику бессмысленно.",
                "error"))
    n_pr = len(report.get("top_priorities") or [])
    if n_pr and not (3 <= n_pr <= 7):
        issues.append(_issue("priorities_count",
                             f"Приоритетов {n_pr}, ожидалось от 3 до 7."))

    # 7) ничего не потеряно: последний шаг конвейера
    md = report.get("report_md") or ""
    if lenses and lenses.get("playtest_recommended") and not report.get("playtest_recommended"):
        issues.append(_issue("playtest_lost",
                             "Линзы рекомендовали живой плейтест, но в синтезе этого нет. "
                             "Для игр с субъективными механиками это главное, что нужно "
                             "сказать автору.", "error"))
    if report.get("playtest_recommended") and not (report.get("playtest_reason") or "").strip():
        issues.append(_issue("playtest_reason_missing",
                             "Плейтест рекомендован без объяснения причины."))
    # Находка по неоценённой категории балл не снизила — но исчезнуть не может.
    # Здесь это особенно легко: в формуле её нет, и в отчёте её отсутствие
    # ничем не выдаёт.
    outside = reference.get("penalties_outside_scored") or []
    if outside:
        marks = [str(p.get("code") or p.get("test") or "") for p in outside]
        lost = [m for m in marks if m and m not in md
                and m not in json.dumps(report.get("findings_applied") or [],
                                        ensure_ascii=False)]
        if lost:
            issues.append(_issue(
                "findings_outside_score_lost",
                "Находки " + ", ".join(sorted(set(lost))) + " не влияют на балл "
                "(их категории не оценивались) и не упомянуты в отчёте. Так они "
                "исчезают бесследно: в формуле их нет, а в тексте не назвали.",
                "error"))

    if lenses and (lenses.get("answers_to_diagnost") or []) and not (
            report.get("lens_answers_carried") or []):
        issues.append(_issue(
            "lens_answers_lost",
            "Линзы ответили на вопросы диагноста, но ответы не дошли до отчёта. "
            "Это последний шаг конвейера — потерянное здесь не увидит никто.", "error"))

    # 8) баланс без статистики — предварительный
    if reference.get("stats_missing") and not report.get("balance_score_preliminary"):
        issues.append(_issue("preliminary_not_marked",
                             "STATS_JSON отсутствует, но balance_score_preliminary не "
                             "выставлен.", "error"))

    # 9) отчёт для автора: структура и дословный дисклеймер
    if not md.strip():
        issues.append(_issue("report_missing", "Пустой report_md — автору нечего показать.",
                             "error"))
    else:
        if _normalize(DISCLAIMER) not in _normalize(md):
            issues.append(_issue("disclaimer_missing",
                                 "Дисклеймер отсутствует или изменён — он обязателен "
                                 "дословно.", "error"))
        if ref_cov["unmeasured"] and not re.search(
                r"непровер|не выполнен|не изм", md, re.I):
            issues.append(_issue(
                "unmeasured_section_missing",
                "В отчёте нет раздела о непроверенном, хотя часть тестов не выполнена. "
                "Раздел обязателен всегда — даже при высоком доверии."))
    return issues


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


# --- вызов -------------------------------------------------------------------
def run(data: dict, provider_name: str = None, cache_dir: str = None) -> dict:
    """Один вызов синтезатора. Возвращает {available, report, issues, reference}."""
    reference = compute_reference(data.get("lenses"), data.get("finding_balance"),
                                  data.get("findings_diagnost"), data.get("sim_meta"))
    reference["stats_missing"] = not data.get("stats")

    system = prompts.load_synthesizer_prompt()
    user = build_message(data, reference)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user, timeout=LLM_TIMEOUT)
    if not resp.available:
        return {"available": False, "error": resp.error, "reference": reference}

    report = _extract_json(resp.text)
    if report is None:
        return {"available": False,
                "error": "Синтезатор вернул ответ, который не удалось разобрать как JSON.",
                "raw": resp.text, "reference": reference}

    issues = validate(report, reference, data.get("lenses"), data.get("sim_meta"))
    if [i for i in issues if i["severity"] == "error"]:
        lines = [f"• [{i['severity']}] {i['code']}: {i['message']}" for i in issues]
        retry = (user + "\n\n=== ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ ===\n"
                 + "\n".join(lines)
                 + "\n\nИсправь ровно эти нарушения и верни ПОЛНЫЙ JSON заново, "
                   "включая report_md целиком.")
        resp2 = provider.complete(system, retry, timeout=LLM_TIMEOUT)
        if resp2.available:
            report2 = _extract_json(resp2.text)
            if report2 is not None:
                issues2 = validate(report2, reference, data.get("lenses"), data.get("sim_meta"))
                if len(issues2) < len(issues):
                    report, issues, resp = report2, issues2, resp2

    return {
        "available": True, "report": report, "issues": issues, "reference": reference,
        "raw": resp.text, "provider": resp.provider,
    }


# --- маршрутизация -----------------------------------------------------------
def route(report: dict, reference: dict, attempts_used: int, max_attempts: int) -> dict:
    """Что делать дальше. Счётчик попыток ведёт оркестратор, а не агент.

    Ключевое поведение при исчерпанном бюджете: отчёт всё равно выдаётся автору,
    но с честным пояснением, что осталось незакрытым. Часть проблем в принципе
    не чинится параметрами (лестница вмешательства авто-редизайнера) и требует
    решения автора — молча прятать это нельзя.
    """
    required = bool((report or {}).get("revision_required",
                                       reference["revision"]["required"]))
    left = max(0, max_attempts - attempts_used)
    return {
        "revision_required": required,
        "reasons": (report or {}).get("revision_reason") or reference["revision"]["reasons"],
        "attempts_used": attempts_used,
        "attempts_left": left,
        # Новый круг возможен, только пока бюджет не исчерпан.
        "can_revise": required and left > 0,
        # Отчёт выдаётся автору либо когда ревизия не нужна, либо когда попытки
        # кончились: держать его при себе бессмысленно.
        "report_ready": (not required) or left <= 0,
        "budget_exhausted": required and left <= 0,
        "to_redesigner_mode_b": {
            "top_priorities": (report or {}).get("top_priorities") or [],
            "findings_applied": (report or {}).get("findings_applied") or [],
        } if required and left > 0 else None,
    }
