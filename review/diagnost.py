"""Агент «Диагност» (v1) — исполнитель методички балансной верификации.

Первый шаг содержательного этапа: после [[stats-evaluator]] и до агента линз.
Берёт на себя всё, чего не видят шесть проверок «Оценщика статистик»: софтлоки,
эксплойт-петли, предопределённость, спираль смерти, баланс карт, компоненты,
масштабируемость, социальные ловушки. Полный текст роли —
review/prompts/diagnost.md, сама методичка — review/prompts/balance_methodology.md
(отдельным документом в user-сообщении).

Агент ДВУХПРОХОДНЫЙ, и это не стилистика: часть тестов невыполнима без
дополнительных прогонов симуляции, а заказать их можно только после того, как
разобрано, какие тесты вообще применимы к этой игре.

  проход 1 (триаж)     — применимость каждого теста + заказ прогонов, БЕЗ вердиктов;
  между проходами      — оркестратор дописывает прогоны в RUN_PLAN и перезапускает скелет;
  проход 2 (исполнение) — вердикты, флаги, заметки.

Самая дорогая ошибка этого шага — превратить `n/a` в `ok`. В итоговом отчёте они
выглядят одинаково («претензий нет»), но `ok` значит «проверили, проблемы нет», а
`n/a` — «не проверяли, есть проблема или нет неизвестно». Поэтому валидатор
следит за полнотой перечня тестов жёстче, чем за чем-либо ещё: пропущенный тест
исчезает бесследно, явный `n/a` виден.

БЮДЖЕТ ПРОГОНОВ ДЕРЖИТ КОД, А НЕ МОДЕЛЬ. В промпте лимит указан, но полагаться
на его соблюдение нельзя: лишний прогон — это деньги и время. `trim_run_requests`
режет заказ по приоритету из промпта, а тесты обрезанных прогонов обязаны уйти
в `n/a` с причиной `budget_exceeded` — это проверяет валидатор.
"""

import json
import os
import re

from review import prompts
from review.llm_provider import get_provider

_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "tests_registry.json")

# Закрытый словарь причин n/a. Каждая обязана называться явно: «не измерено» и
# «проблемы нет» — разные вещи, и в отчёте их путать нельзя.
NA_REASONS = ("mechanic_absent", "no_data", "method_blind",
              "search_incomplete", "budget_exceeded")

TRIAGE_STATUSES = ("applicable", "needs_run", "n/a")
VERDICTS = ("ok", "warning", "problem", "n/a")
SEVERITIES = ("critical", "major", "minor", "n/a")
CONFIDENCES = ("measured", "assumed")

RUN_TYPES = ("seeded_deal", "catchup_off", "extra_player_count", "persona_targeted")

# Не более шести доп. прогонов за итерацию (раздел «Бюджет прогонов»).
BUDGET_LIMIT = 6

# Приоритет обрезки — по порядку из промпта. Чем меньше число, тем важнее.
RUN_PRIORITY = {
    "persona_targeted": 0,      # тесты, способные дать critical (эксплойт, пути победы)
    "catchup_off": 1,           # контрфактуальный прогон catch-up
    "seeded_deal": 2,           # фиксированная раздача
    "extra_player_count": 3,    # дополнительные конфигурации числа игроков
}

# Социальные находки автоматически не чинятся: правка требует нового правила.
SOCIAL_CODES = ("coalition_effect", "kingmaking", "quarterbacking", "conflict_prone")


def load_registry() -> list:
    """Машиночитаемый реестр тестов методички.

    Нужен ТОЛЬКО оркестратору — для проверки полноты ответа. Пороги и контекст
    интерпретации в реестр не переносятся: они живут в методичке, и дублирование
    завело бы второй источник правды.
    """
    with open(_REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def registry_ids(registry: list = None) -> list:
    return [t["test"] for t in (registry if registry is not None else load_registry())]


def persona_blind_tests(registry: list = None) -> list:
    """Тесты, которые при metric_responds_immediately=false обязаны быть n/a.

    Список закрытый и взят из методички дословно (общее замечание к персонам).
    Расширять его от себя нельзя: от него зависит правило валидатора.
    """
    return [t["test"] for t in (registry if registry is not None else load_registry())
            if t.get("persona_blind_na")]


def stats_report_tests(registry: list = None) -> list:
    """Тесты типа ОТЧЕТ_СТАТИСТИК — их диагност не пересчитывает, а ссылается."""
    return [t["test"] for t in (registry if registry is not None else load_registry())
            if t.get("evidence_type") == "STATS_REPORT"]


def card_tests(registry: list = None) -> list:
    """Тесты по картам — при card_coverage.sufficient=false они не могут быть ok."""
    return [t["test"] for t in (registry if registry is not None else load_registry())
            if any("card_coverage" in c for c in (t.get("applies_when") or []))]


def _extract_json(raw_text: str):
    """Достаёт JSON из ответа, терпя обёртку ```json."""
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


def recompute_coverage(results: list, registry: list = None, model_summary: dict = None) -> dict:
    """Считает `coverage_summary` по `results[]` — это арифметика, не суждение.

    Раньше счётчики брались из ответа модели и проверялись только на то, что их
    сумма равна числу тестов. Сумма сходилась, а числа врали: живой прогон дал
    «ok 24 / warning 7 / problem 6» при фактических 27/5/5, и подложная
    `n_a_breakdown` через `score_confidence` определяла, как синтезатор подаёт
    балл автору. Значение здесь известно точно, поэтому оно вычисляется, а не
    выпрашивается у модели.

    `note` — единственное поле сводки, которое остаётся за моделью: это связный
    текст о том, ПОЧЕМУ часть тестов не выполнена, и вывести его из массива
    нельзя.
    """
    results = results or []
    verdicts = {"ok": 0, "warning": 0, "problem": 0, "n/a": 0}
    breakdown = {reason: 0 for reason in NA_REASONS}
    for r in results:
        v = (r or {}).get("verdict")
        if v in verdicts:
            verdicts[v] += 1
        if v == "n/a":
            reason = (r or {}).get("reason")
            if reason in breakdown:
                breakdown[reason] += 1
    summary = {
        # tests_total берём из РЕЕСТРА, а не из ответа: реестр — источник истины
        # о том, сколько тестов в методичке.
        "tests_total": len(registry_ids(registry)) if registry is not None else len(results),
        "ok": verdicts["ok"],
        "warning": verdicts["warning"],
        "problem": verdicts["problem"],
        "n_a": verdicts["n/a"],
        "n_a_breakdown": {k: v for k, v in breakdown.items() if v},
    }
    note = ((model_summary or {}).get("note") or "").strip()
    if note:
        summary["note"] = note
    return summary


def coverage_discrepancies(model_summary: dict, computed: dict) -> list:
    """Чем ответ модели разошёлся с пересчётом. Только для отчёта автору."""
    issues = []
    model_summary = model_summary or {}
    for key in ("tests_total", "ok", "warning", "problem", "n_a"):
        got, want = model_summary.get(key), computed.get(key)
        if got is not None and int(got or 0) != int(want or 0):
            issues.append(_issue(
                "coverage_recomputed",
                f"coverage_summary.{key}: модель сообщила {got}, по вердиктам в "
                f"results — {want}. Взято посчитанное.", "error"))
    mb = model_summary.get("n_a_breakdown") or {}
    cb = computed.get("n_a_breakdown") or {}
    if mb and mb != cb:
        issues.append(_issue(
            "coverage_breakdown_recomputed",
            f"n_a_breakdown: модель сообщила {mb}, по причинам в results — {cb}. "
            "Взято посчитанное: по этой разбивке синтезатор считает доверие к баллу.",
            "error"))
    return issues


# --- сборка входа ------------------------------------------------------------
def simulationist_meta(skeleton_meta: dict) -> dict:
    """Метаданные границ модели от Симуляциониста — ровно те, что нужны диагносту."""
    meta = skeleton_meta or {}
    return {
        "assumptions": meta.get("assumptions") or [],
        "subjective_actions": meta.get("subjective_actions") or [],
        "coalition_expressible": meta.get("coalition_expressible"),
        "coalition_note": meta.get("coalition_note"),
        "metric_responds_immediately": meta.get("metric_responds_immediately"),
        "fixed_length": meta.get("fixed_length"),
        "content_scale": meta.get("content_scale") or {},
        "ignored_components": meta.get("ignored_components") or [],
        "hooks_filled": meta.get("hooks_filled") or {},
        "manual_turn_order": meta.get("manual_turn_order"),
    }


def build_input(spec_root: dict, stats: list, diag: dict, finding_balance: dict,
                skeleton_meta: dict, canonical_text: str = None,
                attempt_number: int = 1, previous_findings: dict = None) -> dict:
    """Единый вход обоих проходов. Собирается из сессии, а не из запроса."""
    root = spec_root or {}
    return {
        "game_spec": root.get("game_spec") or {},
        "diagnostic_meta": root.get("diagnostic_meta") or {},
        "canonical_text": canonical_text or "",
        "stats": stats or [],
        "diag": diag,                      # None у скелетов v3 — это НЕ пустота
        "finding_balance": finding_balance or {},
        "sim_meta": simulationist_meta(skeleton_meta),
        "attempt_number": attempt_number,
        "previous_findings": previous_findings,
    }


def _common_blocks(data: dict) -> list:
    parts = [
        "=== МЕТОДИЧКА БАЛАНСНОЙ ВЕРИФИКАЦИИ (отдельный документ) ===",
        prompts.load_balance_methodology(),
        "",
        "=== GAME_SPEC (core + text) ===",
        json.dumps(data["game_spec"], ensure_ascii=False, indent=2),
        "",
        "=== DIAGNOSTIC_META (сайдкар от извлеченца) ===",
        json.dumps(data["diagnostic_meta"], ensure_ascii=False, indent=2),
        "",
        "=== КАНОНИЧЕСКИЙ ТЕКСТ ИГРЫ (для интерпретации контекста) ===",
        data["canonical_text"] or "(текст не передан)",
        "",
        "=== STATS_JSON (базовый прогон) ===",
        json.dumps(data["stats"], ensure_ascii=False, indent=2),
        "",
        "=== DIAG_JSON (диагностический блок) ===",
        json.dumps(data["diag"], ensure_ascii=False, indent=2) if data["diag"] is not None
        else "ОТСУТСТВУЕТ: скелет не собирал диагностический блок. Это НЕ пустые данные "
             "и НЕ отсутствие проблем — тесты, которым нужен DIAG_JSON, обязаны получить "
             "n/a с причиной no_data.",
        "",
        "=== FINDING_BALANCE.JSON (вердикты «Оценщика статистик», включая covered_tests) ===",
        json.dumps(data["finding_balance"], ensure_ascii=False, indent=2),
        "",
        "=== МЕТАДАННЫЕ СИМУЛЯЦИОНИСТА (границы модели) ===",
        json.dumps(data["sim_meta"], ensure_ascii=False, indent=2),
        "",
        f"=== ПОПЫТКА № {data['attempt_number']} ===",
    ]
    if data.get("previous_findings"):
        parts += ["", "=== ПРОШЛЫЕ НАХОДКИ (повторный заход после ремонта) ===",
                  json.dumps(data["previous_findings"], ensure_ascii=False, indent=2)]

    # Явные подсказки по границам модели: ошибиться здесь дороже всего.
    sim = data["sim_meta"]
    hints = []
    if sim.get("metric_responds_immediately") is False:
        blind = ", ".join(persona_blind_tests())
        hints.append(
            f"metric_responds_immediately = false: жадные персоны вырождаются в случайный "
            f"выбор. Персонные тесты ({blind}) обязаны получить n/a с причиной method_blind, "
            f"а НЕ ok — отсутствие различий между персонами не означает сбалансированности.")
    if sim.get("coalition_expressible") is False:
        hints.append(
            "coalition_expressible = false: сговор механически невыразим. Тесты на сговор "
            "и кингмейкинг — n/a с причиной mechanic_absent, а находка идёт в линзы, "
            "а не в critical_flags.")
    if sim.get("subjective_actions"):
        hints.append(
            "Мягкие числа (действия с subjective_judgment): "
            + ", ".join(sim["subjective_actions"])
            + ". По ним потолок вердикта — warning, находка уходит в notes_for_lenses.")
    if hints:
        parts += ["", "=== ГРАНИЦЫ МОДЕЛИ: ОБЯЗАТЕЛЬНО УЧЕСТЬ ==="] + [f"• {h}" for h in hints]
    return parts


def build_triage_message(data: dict) -> str:
    parts = _common_blocks(data)
    parts += [
        "",
        f"Это ПРОХОД 1 — ТРИАЖ. Верни JSON с phase=\"triage\": применимость ПО КАЖДОМУ "
        f"тесту методички ({len(registry_ids())} штук, ни одного не пропусти) и список "
        f"запрошенных прогонов. ВЕРДИКТОВ НА ЭТОМ ПРОХОДЕ БЫТЬ НЕ ДОЛЖНО. "
        f"Бюджет прогонов — не более {BUDGET_LIMIT}.",
    ]
    return "\n".join(parts)


def build_findings_message(data: dict, triage: dict, extra_runs: dict = None,
                           dropped: list = None) -> str:
    parts = _common_blocks(data)
    parts += [
        "",
        "=== ТВОЙ ТРИАЖ (проход 1) ===",
        json.dumps(triage or {}, ensure_ascii=False, indent=2),
        "",
        "=== РЕЗУЛЬТАТЫ ЗАКАЗАННЫХ ПРОГОНОВ ===",
        json.dumps(extra_runs, ensure_ascii=False, indent=2) if extra_runs
        else "прогоны не заказывались или не выполнялись",
    ]
    if dropped:
        ids = ", ".join(d["run_id"] for d in dropped)
        tests = sorted({t for d in dropped for t in (d.get("for_tests") or [])})
        parts += [
            "",
            "=== ПРОГОНЫ, ОБРЕЗАННЫЕ ПО БЮДЖЕТУ ===",
            f"Не выполнены: {ids}. Оркестратор обрезал заказ по приоритету из промпта.",
            f"Тесты {', '.join(tests)} обязаны получить n/a с причиной budget_exceeded — "
            "молча пропустить их нельзя, неисполненный тест должен быть виден в отчёте.",
        ]
    parts += [
        "",
        f"Это ПРОХОД 2 — ИСПОЛНЕНИЕ. Верни JSON с phase=\"findings\": ровно "
        f"{len(registry_ids())} записей в results (по одной на каждый тест методички), "
        "critical_flags, notes_for_lenses, handed_to_recommendations и coverage_summary.",
    ]
    return "\n".join(parts)


# --- бюджет прогонов ---------------------------------------------------------
def trim_run_requests(requests: list, limit: int = BUDGET_LIMIT) -> tuple:
    """Режет заказ до лимита по приоритету из промпта. Возвращает (kept, dropped).

    Бюджет держит оркестратор, а не модель: молча выполнить лишнее нельзя — это
    деньги и время. Обрезанные прогоны возвращаются отдельно, чтобы их тесты
    получили честный n/a: budget_exceeded, а не исчезли.
    """
    ordered = sorted(
        list(requests or []),
        key=lambda r: (RUN_PRIORITY.get(r.get("request_type"), 99),
                       (requests or []).index(r)))
    return ordered[:limit], ordered[limit:]


# --- валидация ---------------------------------------------------------------
def validate_triage(triage: dict, registry: list = None,
                    limit: int = BUDGET_LIMIT) -> list:
    """Проверки прохода 1 (раздел 6 ТЗ). Ничего не чинит — только сообщает."""
    issues = []
    registry = registry if registry is not None else load_registry()
    known = set(registry_ids(registry))
    triage = triage or {}

    items = triage.get("applicability") or []
    seen = {}
    for it in items:
        tid = it.get("test")
        if tid in seen:
            issues.append(_issue("triage_duplicate", f"Тест «{tid}» встретился дважды."))
        seen[tid] = it
        if tid not in known:
            issues.append(_issue("triage_unknown_test",
                                 f"В applicability тест «{tid}», которого нет в реестре."))
        status = it.get("status")
        if status not in TRIAGE_STATUSES:
            issues.append(_issue("triage_status_unknown",
                                 f"Тест «{tid}»: статус «{status}» вне множества "
                                 f"{'/'.join(TRIAGE_STATUSES)}.", "error"))
        if status == "n/a" and it.get("reason") not in NA_REASONS:
            issues.append(_issue(
                "triage_na_reason",
                f"Тест «{tid}»: причина n/a «{it.get('reason')}» вне закрытого словаря "
                f"({', '.join(NA_REASONS)}).", "error"))

    missing = [t for t in known if t not in seen]
    if missing:
        issues.append(_issue(
            "triage_tests_missing",
            f"В applicability нет {len(missing)} тестов: {', '.join(sorted(missing)[:12])}"
            + ("…" if len(missing) > 12 else "")
            + ". Пропущенный тест исчезает бесследно, явный n/a виден.", "error"))

    requests = triage.get("run_requests") or []
    if len(requests) > limit:
        issues.append(_issue(
            "budget_exceeded",
            f"Заказано {len(requests)} прогонов при лимите {limit}. Оркестратор обрежет "
            "по приоритету, а тесты обрезанных прогонов уйдут в n/a: budget_exceeded."))

    run_ids = set()
    for r in requests:
        rid = r.get("run_id")
        if not rid:
            issues.append(_issue("run_id_missing", "У заказа прогона нет run_id.", "error"))
        elif rid in run_ids:
            issues.append(_issue("run_id_duplicate", f"run_id «{rid}» повторяется.", "error"))
        run_ids.add(rid)
        if r.get("request_type") not in RUN_TYPES:
            issues.append(_issue("run_type_unknown",
                                 f"Неизвестный request_type «{r.get('request_type')}»."))
        if rid == "base":
            issues.append(_issue("run_id_base",
                                 "Заказан прогон с id «base» — базовый прогон трогать нельзя, "
                                 "на нём держится STATS_JSON.", "error"))
        for t in r.get("for_tests") or []:
            if t not in known:
                issues.append(_issue("run_for_unknown_test",
                                     f"Прогон «{rid}» заказан для теста «{t}», которого нет "
                                     "в реестре."))

    for tid, it in seen.items():
        if it.get("status") == "needs_run":
            rid = it.get("run_id")
            if not rid:
                issues.append(_issue("needs_run_without_id",
                                     f"Тест «{tid}» помечен needs_run без run_id.", "error"))
            elif rid not in run_ids:
                issues.append(_issue(
                    "needs_run_no_request",
                    f"Тест «{tid}» ссылается на прогон «{rid}», которого нет в run_requests. "
                    "На проходе 2 агент не сопоставит данные с тестом.", "error"))
    return issues


def validate_findings(findings: dict, registry: list = None, sim_meta: dict = None,
                      finding_balance: dict = None, diag: dict = None,
                      dropped: list = None) -> list:
    """Проверки прохода 2 (раздел 6 ТЗ). Вердикты не пересчитываются."""
    issues = []
    registry = registry if registry is not None else load_registry()
    by_id = {t["test"]: t for t in registry}
    known = set(by_id)
    findings = findings or {}
    sim_meta = sim_meta or {}

    results = findings.get("results") or []
    seen = {}
    for r in results:
        tid = r.get("test")
        if tid in seen:
            issues.append(_issue("result_duplicate", f"Тест «{tid}» встретился дважды."))
        seen[tid] = r
        if tid not in known:
            issues.append(_issue("result_unknown_test",
                                 f"В results тест «{tid}», которого нет в реестре."))
            continue
        verdict = r.get("verdict")
        if verdict not in VERDICTS:
            issues.append(_issue("verdict_unknown",
                                 f"Тест «{tid}»: вердикт «{verdict}» вне множества "
                                 f"{'/'.join(VERDICTS)}.", "error"))
        if verdict in ("ok", "warning", "problem"):
            if not (r.get("evidence") or "").strip():
                issues.append(_issue("evidence_missing",
                                     f"Тест «{tid}» с вердиктом {verdict} без evidence. "
                                     "Нет числа — нет вердикта.", "error"))
            if not (r.get("source") or "").strip():
                issues.append(_issue("source_missing",
                                     f"Тест «{tid}»: не указан источник числа (source)."))
        if verdict == "n/a" and r.get("reason") not in NA_REASONS:
            issues.append(_issue("na_reason_unknown",
                                 f"Тест «{tid}»: причина n/a «{r.get('reason')}» вне "
                                 "закрытого словаря.", "error"))
        if verdict == "problem" and r.get("confidence") == "assumed":
            issues.append(_issue(
                "problem_on_assumed",
                f"Тест «{tid}»: вердикт problem на мягком числе. Такое число отражает "
                "допущение симулятора, а не игру — потолок warning.", "error"))
        if r.get("severity") and r["severity"] not in SEVERITIES:
            issues.append(_issue("severity_unknown",
                                 f"Тест «{tid}»: неизвестная тяжесть «{r['severity']}»."))

    missing = [t for t in known if t not in seen]
    if missing:
        issues.append(_issue(
            "results_missing",
            f"В results нет {len(missing)} тестов: {', '.join(sorted(missing)[:12])}"
            + ("…" if len(missing) > 12 else "")
            + f". Ожидалось ровно {len(known)} записей — по одной на каждый тест методички.",
            "error"))

    # covered_tests: свой пересчёт запрещён, только ссылка
    covered_full = {c.get("test") for c in (finding_balance or {}).get("covered_tests") or []
                    if c.get("coverage") == "full"}
    ref_types = set(stats_report_tests(registry))
    for tid in sorted(covered_full & set(seen)):
        src = (seen[tid].get("source") or "").upper()
        if tid in ref_types and "ОТЧЕТ_СТАТИСТИК" not in src and "COVERED_TESTS" not in src:
            issues.append(_issue(
                "recomputed_covered_test",
                f"Тест «{tid}» закрыт «Оценщиком статистик» (coverage=full), но вердикт "
                "не помечен ссылкой на его отчёт. Два мнения об одном числе приводят к "
                "тому, что редизайнер чинит по случайному из них.", "error"))

    # персонная слепота
    if sim_meta.get("metric_responds_immediately") is False:
        for tid in persona_blind_tests(registry):
            r = seen.get(tid)
            if not r:
                continue
            if r.get("verdict") != "n/a" or r.get("reason") != "method_blind":
                issues.append(_issue(
                    "persona_not_blind",
                    f"Тест «{tid}» персонный, а metric_responds_immediately = false: жадные "
                    "персоны вырождаются в случайный выбор. Ожидался n/a с причиной "
                    f"method_blind, получено «{r.get('verdict')}».", "error"))

    # покрытие по картам
    runs = ((diag or {}).get("runs") or [])
    base = next((x for x in runs if x.get("run_id") == "base"), None)
    coverage = ((base or {}).get("diagnostics") or {}).get("card_coverage") or {}
    if coverage and coverage.get("sufficient") is False:
        for tid in card_tests(registry):
            r = seen.get(tid)
            if r and r.get("verdict") == "ok":
                issues.append(_issue(
                    "card_ok_without_coverage",
                    f"Тест «{tid}» получил ok, хотя card_coverage.sufficient = false — "
                    "наблюдений по картам не хватило. Ожидался n/a.", "error"))

    # полнота поиска петель
    ex = (diag or {}).get("exploit_search") or {}
    incomplete = [k for k, v in ex.items() if isinstance(v, dict)
                  and v.get("search_exhausted") is False]
    if incomplete:
        r = seen.get("6.2")
        if r and r.get("verdict") == "ok":
            issues.append(_issue(
                "exploit_ok_incomplete",
                "Тест 6.2 получил ok, хотя exploit_search.search_exhausted = false при N="
                + ", ".join(incomplete)
                + ". Отсутствие находки при неполном переборе не доказывает отсутствия петли.",
                "error"))

    # обрезанные по бюджету прогоны обязаны быть видны
    for d in dropped or []:
        for tid in d.get("for_tests") or []:
            r = seen.get(tid)
            if r and not (r.get("verdict") == "n/a" and r.get("reason") == "budget_exceeded"):
                issues.append(_issue(
                    "budget_test_not_marked",
                    f"Прогон «{d.get('run_id')}» обрезан по бюджету, но тест «{tid}» "
                    f"не помечен n/a: budget_exceeded (получено «{r.get('verdict')}»).",
                    "error"))

    # критичные флаги
    for f in findings.get("critical_flags") or []:
        if f.get("severity") != "critical":
            issues.append(_issue(
                "flag_not_critical",
                f"В critical_flags флаг «{f.get('code')}» с тяжестью «{f.get('severity')}». "
                "Major и minor ждут синтеза и приходят в режиме B.", "error"))
        if f.get("code") in SOCIAL_CODES:
            issues.append(_issue(
                "social_in_critical",
                f"Социальная находка «{f.get('code')}» в critical_flags. Автоматически она "
                "не чинится — правка требует нового правила; место таких находок в "
                "notes_for_lenses и handed_to_recommendations.", "error"))
        if f.get("confidence") not in CONFIDENCES:
            issues.append(_issue("flag_confidence_missing",
                                 f"У флага «{f.get('code')}» не проставлен confidence."))

    # Сводка покрытия ПЕРЕСЧИТЫВАЕТСЯ по results[], а ответ модели используется
    # только для сверки: расхождение — замечание, но в отчёт уходит посчитанное.
    cs_model = findings.get("coverage_summary") or {}
    cs = recompute_coverage(findings.get("results") or [], registry, cs_model)
    issues += coverage_discrepancies(cs_model, cs)

    unknown = sorted(set(cs_model.get("n_a_breakdown") or {}) - set(NA_REASONS))
    if unknown:
        issues.append(_issue("coverage_reason_unknown",
                             "В n_a_breakdown причины вне словаря: " + ", ".join(unknown)))
    br = cs.get("n_a_breakdown") or {}
    unmeasured = sum(int(br.get(k) or 0) for k in
                     ("no_data", "method_blind", "search_incomplete", "budget_exceeded"))
    if unmeasured > 0 and not (cs.get("note") or "").strip():
        issues.append(_issue(
            "coverage_note_missing",
            f"{unmeasured} тестов не выполнены (no_data / method_blind / "
            "search_incomplete / budget_exceeded), но coverage_summary.note пуст. "
            "Без него синтезатор примет непроверенное за проверенное.", "error"))

    # гибридные тесты обязаны дать вопрос линзам
    asked = {n.get("from_test") for n in findings.get("notes_for_lenses") or []}
    for t in registry:
        if t.get("evidence_type") == "HYBRID" and seen.get(t["test"]):
            if seen[t["test"]].get("verdict") != "n/a" and t["test"] not in asked:
                issues.append(_issue(
                    "hybrid_without_note",
                    f"Гибридный тест «{t['test']}» выполнен, но вопроса для линз нет. "
                    "Вердикт по нему выносят линзы, а не диагност."))
    return issues


# --- вызовы ------------------------------------------------------------------
# Диагност — самый тяжёлый вызов конвейера: на вход уходит методичка целиком,
# STATS_JSON и DIAG_JSON со всеми прогонами (~50 тыс. токенов), на выходе — по
# записи на каждый из 50 тестов. Общий таймаут в 25 секунд подобран под короткие
# вызовы вроде «Проверить связь» и здесь гарантированно срывает запрос ещё до
# того, как модель дописала ответ, после чего каскад впустую перебирает модели.
LLM_TIMEOUT = 300


def _call(system: str, user: str, provider_name, cache_dir):
    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user, timeout=LLM_TIMEOUT)
    if not resp.available:
        return None, {"available": False, "error": resp.error}
    data = _extract_json(resp.text)
    if data is None:
        return None, {"available": False,
                      "error": "Диагност вернул ответ, который не удалось разобрать как JSON.",
                      "raw": resp.text}
    return (data, resp), None


def _retry_message(user: str, issues: list) -> str:
    """Один повтор с перечнем нарушений — ответ моделью, а не постобработкой.

    Проект не падает на провале валидации (изящная деградация), но и не «чинит»
    ответ за модель: даём ей конкретный список нарушений и просим исправить.
    """
    lines = [f"• [{i['severity']}] {i['code']}: {i['message']}" for i in issues]
    return (user + "\n\n=== ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ ===\n"
            + "\n".join(lines)
            + "\n\nИсправь ровно эти нарушения и верни ПОЛНЫЙ JSON заново. "
              "Не сокращай ответ и не убирай тесты — их число должно остаться прежним.")


def run_triage(data: dict, provider_name: str = None, cache_dir: str = None) -> dict:
    """Проход 1 — триаж. Возвращает {available, triage, issues, kept, dropped, raw}."""
    system = prompts.load_diagnost_prompt()
    user = build_triage_message(data)

    got, err = _call(system, user, provider_name, cache_dir)
    if err:
        return err
    triage, resp = got
    issues = validate_triage(triage)

    if [i for i in issues if i["severity"] == "error"]:
        got2, err2 = _call(system, _retry_message(user, issues), provider_name, cache_dir)
        if not err2:
            triage2, resp = got2
            issues2 = validate_triage(triage2)
            if len(issues2) < len(issues):
                triage, issues = triage2, issues2

    kept, dropped = trim_run_requests(triage.get("run_requests") or [])

    # budget_used модель сообщала сама, а держит бюджет код (trim_run_requests).
    # Опасности в расхождении не было, но это вычислимое число — считаем его.
    reported = triage.get("budget_used")
    if reported is not None and int(reported or 0) != len(kept):
        issues.append(_issue(
            "budget_used_recomputed",
            f"budget_used: модель сообщила {reported}, фактически оставлено прогонов "
            f"{len(kept)} (остальное обрезано бюджетом). Взято посчитанное."))
    triage["budget_used"] = len(kept)
    triage["budget_limit"] = BUDGET_LIMIT

    return {
        "available": True,
        "triage": triage,
        "issues": issues,
        "kept_requests": kept,
        "dropped_requests": dropped,
        "raw": resp.text,
        "provider": resp.provider,
    }


def run_findings(data: dict, triage: dict, extra_runs: dict = None,
                 dropped: list = None, provider_name: str = None,
                 cache_dir: str = None) -> dict:
    """Проход 2 — исполнение. Возвращает {available, findings, issues, raw}."""
    system = prompts.load_diagnost_prompt()
    user = build_findings_message(data, triage, extra_runs, dropped)

    got, err = _call(system, user, provider_name, cache_dir)
    if err:
        return err
    findings, resp = got

    def _check(f):
        return validate_findings(f, sim_meta=data.get("sim_meta"),
                                 finding_balance=data.get("finding_balance"),
                                 diag=data.get("diag"), dropped=dropped)

    issues = _check(findings)
    # Повтор запрашиваем только по тем нарушениям, которые модель ДОЛЖНА чинить
    # сама. Расхождение в вычислимой величине к ним не относится: правильное
    # значение известно точно, и просить модель угадать его ещё раз — трата
    # вызова. Такие замечания остаются в отчёте, но повтор не запускают.
    RECOMPUTED = ("coverage_recomputed", "coverage_breakdown_recomputed")
    blocking = [i for i in issues
                if i["severity"] == "error" and i["code"] not in RECOMPUTED]
    if blocking:
        got2, err2 = _call(system, _retry_message(user, blocking), provider_name, cache_dir)
        if not err2:
            findings2, resp = got2
            issues2 = _check(findings2)
            if len(issues2) < len(issues):
                findings, issues = findings2, issues2

    # Посчитанная сводка подменяет сообщённую: это арифметика по results[], а не
    # суждение. Расхождение уже зафиксировано отдельным замечанием выше.
    findings["coverage_summary"] = recompute_coverage(
        findings.get("results") or [], load_registry(),
        findings.get("coverage_summary"))

    return {
        "available": True,
        "findings": findings,
        "issues": issues,
        "raw": resp.text,
        "provider": resp.provider,
    }


# --- маршрутизация -----------------------------------------------------------
def route(findings: dict) -> dict:
    """Раскладывает Findings_diagnost.json по потребителям.

    Ключевое: `n/a` НИГДЕ не схлопывается в «проблем не найдено». `coverage_summary`
    едет к синтезатору обязательно и целиком — доля непроверенного влияет на то,
    с какой уверенностью формулируется итоговый отчёт.
    """
    findings = findings or {}
    critical = [f for f in findings.get("critical_flags") or []
                if f.get("severity") == "critical" and f.get("code") not in SOCIAL_CODES]
    cs = findings.get("coverage_summary") or {}
    br = cs.get("n_a_breakdown") or {}
    unmeasured = sum(int(br.get(k) or 0) for k in
                     ("no_data", "method_blind", "search_incomplete", "budget_exceeded"))
    return {
        # авто-редизайнер, режим D — немедленно, не дожидаясь синтеза
        "to_redesigner_mode_d": critical,
        "needs_redesign": bool(critical),
        # агент линз — только когда критичного не осталось
        "to_lenses": findings.get("notes_for_lenses") or [],
        "lenses_ready": not critical,
        # синтезатор — обязательно и целиком
        "to_synthesis": {
            "coverage_summary": cs,
            "results": findings.get("results") or [],
            "handed_to_recommendations": findings.get("handed_to_recommendations") or [],
        },
        "unmeasured_count": unmeasured,
    }
