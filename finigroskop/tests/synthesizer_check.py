# -*- coding: utf-8 -*-
"""Проверка агента «Синтезатор оценки»: арифметика балла и решение о ревизии.

Самая дорогая ошибка этого шага — не «неточный балл», а два молчаливых сдвига.
Первый: N/A превращается в ноль, и игру наказывают за то, что её не смогли
проверить. Второй: `revision_required` расходится с фактами — игра с рабочей
эксплойт-петлёй набирает проходные 6.4 и уходит в отчёт нечинёной.

Плюс то, что теряется на последнем шаге и потому не всплывает нигде: ответы
линз диагносту, рекомендация плейтеста, дословный дисклеймер и мягкие находки,
попавшие в приоритеты автору.
"""
import json
import os
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import synthesizer as S  # noqa: E402
from review import redesigner as R  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

checks = {}
CATS = list(S.CATEGORY_WEIGHTS)


def codes(issues):
    return [i["code"] for i in issues]


# ============================================================================
# ЧАСТЬ 1. Константы и соответствие контрактам
# ============================================================================
checks["категорий девять"] = len(S.CATEGORY_WEIGHTS) == 9
checks["веса как в промпте"] = (
    S.CATEGORY_WEIGHTS["Замысел и образовательная ценность"] == 1.5
    and S.CATEGORY_WEIGHTS["Экономика и баланс"] == 1.5
    and S.CATEGORY_WEIGHTS["Структурная целостность и физический интерфейс"] == 1.25
    and S.CATEGORY_WEIGHTS["Социальное взаимодействие"] == 0.75)
checks["категорий методички четырнадцать"] = sorted(S.DIAGNOST_CATEGORY_MAP) == list(range(1, 15))
# Сопоставление идёт по ИМЕНИ: опечатка тут = молчаливое выпадение категории.
checks["все цели сопоставления существуют"] = all(
    v in S.CATEGORY_WEIGHTS for v in S.DIAGNOST_CATEGORY_MAP.values())
checks["категория 5 → доступность"] = S.DIAGNOST_CATEGORY_MAP[5] == "Игрок, доступность, вовлечение"
checks["категория 9 → экономика"] = S.DIAGNOST_CATEGORY_MAP[9] == "Экономика и баланс"
checks["категория 7 → социальное"] = S.DIAGNOST_CATEGORY_MAP[7] == "Социальное взаимодействие"
checks["стоимости находок"] = (S.SEVERITY_COST["critical"] == 3.0
                               and S.SEVERITY_COST["major"] == 2.0
                               and S.SEVERITY_COST["minor"] == 0.5)
checks["mechanic_absent — нейтральная причина"] = S.NEUTRAL_NA == ("mechanic_absent",)
checks["четыре причины «не измерено»"] = len(S.UNMEASURED_NA) == 4

# ============================================================================
# ЧАСТЬ 2. Стоимость находки: мягкость и частичные конфигурации
# ============================================================================
checks["critical стоит -3"] = S.finding_cost({"severity": "critical"}) == -3.0
checks["major стоит -2"] = S.finding_cost({"severity": "major"}) == -2.0
checks["minor стоит -0.5"] = S.finding_cost({"severity": "minor"}) == -0.5
checks["неизвестная тяжесть ничего не стоит"] = S.finding_cost({"severity": "???"}) == 0.0
checks["мягкая находка стоит вдвое меньше"] = S.finding_cost(
    {"severity": "major", "confidence": "assumed"}) == -1.0
checks["частичные конфигурации стоят вдвое меньше"] = S.finding_cost(
    {"severity": "major", "affected_configs": [4]}) == -1.0
checks["все конфигурации — полная стоимость"] = S.finding_cost(
    {"severity": "major", "affected_configs": [2, 3, 4], "all_configs": True}) == -2.0
checks["мягкая и частичная — четверть"] = S.finding_cost(
    {"severity": "critical", "confidence": "assumed", "affected_configs": [4]}) == -0.75
# Мягкость определяется и по составу действий: симуляционист заявляет, какие
# исходы он смоделировал случайной величиной.
checks["мягкость по subjective_actions"] = S.is_soft(
    {"severity": "major", "detail": "доминирует negotiate"}, ["negotiate"]) is True
checks["без списка действий мягкости нет"] = S.is_soft({"severity": "major"}, []) is False

# ============================================================================
# ЧАСТЬ 3. Доля непроверенного — знаменатель без mechanic_absent
# ============================================================================
cov = S.coverage_share({"tests_total": 50, "n_a_breakdown": {
    "mechanic_absent": 10, "no_data": 3, "method_blind": 1,
    "search_incomplete": 0, "budget_exceeded": 0}})
checks["применимых 40, а не 50"] = cov["applicable"] == 40
checks["непроверенных 4"] = cov["unmeasured"] == 4
checks["доля считается от применимых"] = cov["share"] == 0.1
checks["10% — уже medium"] = cov["confidence"] == "medium"
checks["меньше 10% — high"] = S.coverage_share(
    {"tests_total": 50, "n_a_breakdown": {"no_data": 3}})["confidence"] == "high"
checks["больше 30% — low"] = S.coverage_share(
    {"tests_total": 50, "n_a_breakdown": {"no_data": 20}})["confidence"] == "low"
checks["ровно 30% ещё medium"] = S.coverage_share(
    {"tests_total": 50, "n_a_breakdown": {"no_data": 15}})["confidence"] == "medium"
# 25 из 50 неприменимы, из оставшихся 25 не измерено 10 → 40%, а не 20%.
checks["absent не разбавляет долю"] = S.coverage_share(
    {"tests_total": 50, "n_a_breakdown": {"mechanic_absent": 25, "no_data": 10}}
)["confidence"] == "low"
checks["пустая сводка не падает"] = S.coverage_share({})["confidence"] == "high"

# --- второй разрыв: категории, которые не оценивались вовсе -------------------
# Тесты диагноста выполнены полностью, но балл собран по одной категории из
# девяти. Называть такой балл надёжным нельзя — берётся худший из двух уровней.
full_tests = {"tests_total": 50, "n_a_breakdown": {}}
one_cat = {"Экономика и баланс": {"score": 7.0}}
cov1 = S.coverage_share(full_tests, one_cat)
checks["по тестам доверие высокое"] = cov1["tests_confidence"] == "high"
checks["по категориям доверие низкое"] = cov1["categories_confidence"] == "low"
checks["итоговое доверие — худшее"] = cov1["confidence"] == "low"
checks["доля неоценённых категорий посчитана"] = cov1["categories_unscored_share"] == 0.85
all_cats = {c: {"score": 7.0} for c in CATS}
checks["все категории оценены — доверие не падает"] = S.coverage_share(
    full_tests, all_cats)["confidence"] == "high"
checks["без сведений о категориях старая формула"] = S.coverage_share(
    full_tests)["confidence"] == "high"

# ============================================================================
# ЧАСТЬ 4. Потолки накопления и N/A в формуле
# ============================================================================
pen = [{"category": "Экономика и баланс", "severity": "major", "cost": -2.0},
       {"category": "Экономика и баланс", "severity": "major", "cost": -2.0},
       {"category": "Экономика и баланс", "severity": "major", "cost": -2.0},
       {"category": "Экономика и баланс", "severity": "minor", "cost": -0.5}]
res = S.apply_penalties({"Экономика и баланс": 8.0}, pen)
checks["снижение упирается в -5"] = res["Экономика и баланс"]["penalty"] == -5.0
checks["балл после потолка"] = res["Экономика и баланс"]["score"] == 3.0

res = S.apply_penalties({"Экономика и баланс": 3.0}, pen)
checks["ниже единицы не опускается"] = res["Экономика и баланс"]["score"] == 1.0

res = S.apply_penalties({"Экономика и баланс": 9.0},
                        [{"category": "Экономика и баланс", "severity": "critical",
                          "cost": -3.0}])
checks["critical ставит потолок 5"] = res["Экономика и баланс"]["score"] == 5.0
checks["потолок отмечен"] = res["Экономика и баланс"]["capped_by_critical"] is True

res = S.apply_penalties({"Механика и решения игрока": 7.0}, [])
checks["без находок балл не меняется"] = res["Механика и решения игрока"]["score"] == 7.0

# --- формула: N/A не входит ни в числитель, ни в знаменатель
scores = {c: {"score": 6.0} for c in CATS}
tot = S.overall_score(scores)
checks["все шестёрки дают шесть"] = tot["overall"] == 6.0
checks["сумма весов полная"] = tot["sum_weights"] == 10.0

partial = {c: {"score": 6.0} for c in CATS if c != "Социальное взаимодействие"}
tot = S.overall_score(partial)
checks["N/A убрана из знаменателя"] = tot["sum_weights"] == 9.25
checks["N/A не занизила балл"] = tot["overall"] == 6.0
checks["N/A помечена в строках"] = [r for r in tot["rows"] if r["na"]][0]["category"] == \
    "Социальное взаимодействие"
# То же, но если бы N/A считали нулём — балл упал бы. Это и есть запрещённое.
zeroed = dict(partial, **{"Социальное взаимодействие": {"score": 0.0}})
checks["ноль вместо N/A даёт другой балл"] = S.overall_score(zeroed)["overall"] < 6.0
checks["без единой категории балла нет"] = S.overall_score({})["overall"] is None

# --- вес действительно влияет
w = S.overall_score(dict({c: {"score": 5.0} for c in CATS},
                         **{"Экономика и баланс": {"score": 10.0}}))
checks["тяжёлая категория тянет сильнее"] = w["overall"] == round((5.0 * 8.5 + 10.0 * 1.5) / 10.0, 3)

# ============================================================================
# ЧАСТЬ 5. revision_required — три условия, считает КОД
# ============================================================================
rv = S.revision_decision(5.9, {}, {})
checks["балл ниже шести требует ревизии"] = rv["required"] is True
checks["ровно шесть — не требует"] = S.revision_decision(6.0, {}, {})["required"] is False
checks["причина названа"] = "6" in rv["reasons"][0]

rv = S.revision_decision(8.5, {"critical_flags": [
    {"code": "exploit_loop", "severity": "critical"}]}, {})
checks["критичная находка диагноста важнее балла"] = rv["required"] is True
checks["код находки в причине"] = "exploit_loop" in rv["reasons"][0]

rv = S.revision_decision(9.0, {}, {"flags": [{"code": "seat_bias", "severity": "critical"}]})
checks["критичный флаг баланса требует ревизии"] = rv["required"] is True

checks["чистая игра ревизии не требует"] = S.revision_decision(
    7.5, {"critical_flags": [{"code": "x", "severity": "major"}]},
    {"flags": [{"code": "y", "severity": "major"}]})["required"] is False
checks["без балла и находок ревизии нет"] = S.revision_decision(None, {}, {})["required"] is False

# ============================================================================
# ЧАСТЬ 6. Сквозной эталонный расчёт
# ============================================================================
LENSES = {"categories": [{"name": c, "category_avg_preliminary": 7.0} for c in CATS],
          "playtest_recommended": True,
          "playtest_reason": "торг игроки ведут сами",
          "answers_to_diagnost": [{"from_test": "7.1", "answer": "сговор задуман"}]}
BALANCE = {"scores": {"balance": 8.0, "win_reachability": 9.0},
           "flags": [{"code": "dead_action", "severity": "minor", "confidence": "measured",
                      "detail": "действие invest выбирается в 1% ходов"}]}
DIAGNOST = {
    "results": [
        {"test": "2.1", "verdict": "problem", "severity": "major", "confidence": "measured",
         "evidence": "softlock_rate = 0.12"},
        {"test": "9.2", "verdict": "warning", "severity": "minor", "confidence": "measured",
         "evidence": "разброс winrate карт 14 п.п."},
        {"test": "8.1", "verdict": "ok"},
        {"test": "3.3", "verdict": "n/a", "reason": "mechanic_absent"},
    ],
    "critical_flags": [],
    "coverage_summary": {"tests_total": 50, "ok": 40,
                         "n_a_breakdown": {"mechanic_absent": 6, "no_data": 4}},
}
REF = S.compute_reference(LENSES, BALANCE, DIAGNOST, {"subjective_actions": []})
cs = REF["category_scores"]
checks["балл экономики взят из числового отчёта"] = cs["Экономика и баланс"]["base"] == 8.0
# Экономика собирает снижения из двух источников сразу: минорный флаг баланса
# (dead_action) и минорный вердикт диагноста по картам (9.2) — по 0.5 каждый.
checks["снижения двух источников сложились"] = cs["Экономика и баланс"]["score"] == 7.0
checks["тест 2.1 попал в структурную целостность"] = (
    cs["Структурная целостность и физический интерфейс"]["score"] == 5.0)
checks["тест 9.2 попал в экономику"] = any(
    p["test"] == "9.2" and p["category"] == "Экономика и баланс" for p in REF["penalties"])
checks["ok-вердикты ничего не стоят"] = not any(p.get("test") == "8.1" for p in REF["penalties"])
checks["n/a-вердикты ничего не стоят"] = not any(p.get("test") == "3.3" for p in REF["penalties"])
checks["доверие high при 4 из 44"] = REF["coverage"]["confidence"] == "high"
checks["ревизия не требуется"] = REF["revision"]["required"] is False
checks["все девять категорий оценены"] = all(not r["na"] for r in REF["total"]["rows"])

# --- вердикт problem без severity не теряется
NO_SEV = {"results": [{"test": "6.1", "verdict": "problem", "evidence": "петля найдена"}],
          "coverage_summary": {"tests_total": 1, "n_a_breakdown": {}}}
p = S.diagnost_penalties(NO_SEV)
checks["problem без тяжести считается major"] = p[0]["severity"] == "major"
checks["warning без тяжести считается minor"] = S.diagnost_penalties(
    {"results": [{"test": "6.1", "verdict": "warning"}]})[0]["severity"] == "minor"
checks["нечисловой номер теста не роняет расчёт"] = S.diagnost_penalties(
    {"results": [{"test": "abc", "verdict": "problem"}]}) == []

# --- линз нет: их категории уходят в N/A, а не в ноль
REF_NL = S.compute_reference(None, BALANCE, DIAGNOST, {})
na_names = {r["category"] for r in REF_NL["total"]["rows"] if r["na"]}
checks["без линз почти всё N/A"] = "Замысел и образовательная ценность" in na_names
checks["экономика считается и без линз"] = "Экономика и баланс" not in na_names
checks["без линз балл не обнулён"] = REF_NL["total"]["overall"] is not None
checks["без линз знаменатель сузился"] = REF_NL["total"]["sum_weights"] < 10.0
checks["без линз доверие низкое"] = REF_NL["coverage"]["confidence"] == "low"
# Вердикт 2.1 относится к структурной целостности, которую без линз не оценивали:
# снизить он ничего не может, но исчезнуть не имеет права.
outside_marks = {p.get("test") for p in REF_NL["penalties_outside_scored"]}
checks["находка вне оценённых категорий сохранена"] = "2.1" in outside_marks
checks["она не попала в применённые снижения"] = not any(
    p.get("test") == "2.1" for p in REF_NL["penalties"])
checks["с линзами таких находок нет"] = REF["penalties_outside_scored"] == []

msg_out = S.build_message(S.build_input(None, BALANCE, DIAGNOST), REF_NL)
checks["находки вне балла поданы агенту"] = "НЕ ПОВЛИЯЛИ НА БАЛЛ" in msg_out
checks["сказано, что проблем это не отменяет"] = "НЕ значит, что проблем нет" in msg_out

# ============================================================================
# ЧАСТЬ 7. Валидатор
# ============================================================================
def report(**kw):
    base = {
        "overall_score": REF["total"]["overall"],
        "score_confidence": REF["coverage"]["confidence"],
        "confidence_note": "4 теста из 44 применимых не выполнены: не хватило данных.",
        "revision_required": False,
        "revision_reason": [],
        "categories": [{"name": c, "score": cs[c]["score"],
                        "weight": S.CATEGORY_WEIGHTS[c]} for c in CATS],
        "findings_applied": [{"code": "dead_action", "category": "Экономика и баланс",
                              "cost": -0.5, "detail": "invest в 1% ходов"}],
        "top_priorities": [{"what": "оживить действие invest"},
                           {"what": "закрыть софтлок"},
                           {"what": "выровнять карты"}],
        "playtest_recommended": True,
        "playtest_reason": "торг игроки ведут сами",
        "lens_answers_carried": [{"from_test": "7.1", "answer": "сговор задуман"}],
        "report_md": ("# Отчёт\n\n## Что не проверено\n4 теста не выполнены.\n\n"
                      + S.DISCLAIMER),
    }
    base.update(kw)
    return base


checks["корректный отчёт замечаний не даёт"] = S.validate(report(), REF, LENSES, {}) == []

c = codes(S.validate(report(overall_score=8.5), REF, LENSES, {}))
checks["расхождение балла поймано"] = "score_mismatch" in c
checks["в сообщении есть обе суммы"] = str(REF["total"]["sum_weights"]) in "".join(
    i["message"] for i in S.validate(report(overall_score=8.5), REF, LENSES, {}))

c = codes(S.validate(report(overall_score=None), REF, LENSES, {}))
checks["пропавший балл пойман"] = "score_missing" in c

# Половина применимых тестов не выполнена — доверие обязано быть low, и «high»
# в ответе агента должно поймать код, а не читатель отчёта.
REF_LOW = S.compute_reference(
    LENSES, BALANCE,
    dict(DIAGNOST, coverage_summary={"tests_total": 50,
                                     "n_a_breakdown": {"mechanic_absent": 6, "no_data": 22}}),
    {})
checks["половина непроверенного даёт low"] = REF_LOW["coverage"]["confidence"] == "low"
c = codes(S.validate(report(overall_score=REF_LOW["total"]["overall"],
                            score_confidence="high"), REF_LOW, LENSES, {}))
checks["неверное доверие поймано"] = "confidence_mismatch" in c

c = codes(S.validate(report(confidence_note=""), REF, LENSES, {}))
checks["пустая заметка о доверии поймана"] = "confidence_note_missing" in c

# --- N/A: две запрещённые подмены
bad_cats = [{"name": c_, "score": 6.0, "weight": S.CATEGORY_WEIGHTS[c_]} for c_ in CATS]
c = codes(S.validate(report(categories=bad_cats), REF_NL, None, {}))
checks["балл за неоценённую категорию пойман"] = "na_category_scored" in c

zero_na = [{"name": "Замысел и образовательная ценность", "na": True, "score": 0}]
checks["N/A с нулём поймана"] = "na_as_zero" in codes(
    S.validate(report(categories=zero_na), REF_NL, None, {}))

wrong_w = [{"name": "Экономика и баланс", "score": 7.5, "weight": 1.0}]
checks["подменённый вес пойман"] = "weight_wrong" in codes(
    S.validate(report(categories=wrong_w), REF, LENSES, {}))

checks["балл вне диапазона пойман"] = "category_out_of_range" in codes(
    S.validate(report(categories=[{"name": "Экономика и баланс", "score": 0.4, "weight": 1.5}]),
               REF, LENSES, {}))
checks["выдуманная категория поймана"] = "category_unknown" in codes(
    S.validate(report(categories=[{"name": "Красота коробки", "score": 8, "weight": 1}]),
               REF, LENSES, {}))

# --- главное поле
c = codes(S.validate(report(revision_required=True, revision_reason=["хочется"]),
                     REF, LENSES, {}))
checks["ложная ревизия поймана"] = "revision_mismatch" in c

REF_BAD = S.compute_reference(
    LENSES, dict(BALANCE, flags=[{"code": "exploit_loop", "severity": "critical",
                                  "confidence": "measured", "detail": "петля"}]),
    DIAGNOST, {})
checks["критичный флаг открыл ревизию"] = REF_BAD["revision"]["required"] is True
c = codes(S.validate(report(overall_score=REF_BAD["total"]["overall"],
                            categories=[{"name": k, "score": v["score"],
                                         "weight": S.CATEGORY_WEIGHTS[k]}
                                        for k, v in REF_BAD["category_scores"].items()],
                            revision_required=False), REF_BAD, LENSES, {}))
checks["пропущенная ревизия поймана"] = "revision_mismatch" in c
checks["в сообщении названа причина"] = "exploit_loop" in "".join(
    i["message"] for i in S.validate(report(revision_required=False), REF_BAD, LENSES, {}))

# --- находки
checks["находка без стоимости поймана"] = "finding_without_cost" in codes(
    S.validate(report(findings_applied=[{"code": "x", "detail": "есть"}]), REF, LENSES, {}))
checks["находка без доказательства поймана"] = "finding_without_detail" in codes(
    S.validate(report(findings_applied=[{"code": "x", "cost": -1}]), REF, LENSES, {}))

# --- мягкая находка в приоритетах: чинить нечего, это артефакт симулятора
SOFT_REF = S.compute_reference(
    LENSES, dict(BALANCE, flags=[{"code": "action_dominance", "severity": "major",
                                  "confidence": "assumed", "detail": "negotiate доминирует"}]),
    DIAGNOST, {"subjective_actions": ["negotiate"]})
c = codes(S.validate(
    report(overall_score=SOFT_REF["total"]["overall"],
           top_priorities=["устранить доминирование action_dominance", "b", "c"]),
    SOFT_REF, LENSES, {"subjective_actions": ["negotiate"]}))
checks["мягкая находка в приоритетах поймана"] = "soft_in_priorities" in c
checks["мягкий флаг стоит вдвое меньше"] = any(
    p.get("code") == "action_dominance" and p["cost"] == -1.0 for p in SOFT_REF["penalties"])

checks["слишком мало приоритетов поймано"] = "priorities_count" in codes(
    S.validate(report(top_priorities=[{"what": "одно"}]), REF, LENSES, {}))

# --- потери на последнем шаге
checks["потерянный плейтест пойман"] = "playtest_lost" in codes(
    S.validate(report(playtest_recommended=False), REF, LENSES, {}))
checks["плейтест без причины пойман"] = "playtest_reason_missing" in codes(
    S.validate(report(playtest_reason=""), REF, LENSES, {}))
checks["потерянные ответы линз пойманы"] = "lens_answers_lost" in codes(
    S.validate(report(lens_answers_carried=[]), REF, LENSES, {}))

# --- находка, которая не влияет на балл, обязана дойти до текста отчёта
rep_nl = report(overall_score=REF_NL["total"]["overall"],
                score_confidence=REF_NL["coverage"]["confidence"],
                categories=[{"name": k, "score": v["score"],
                             "weight": S.CATEGORY_WEIGHTS[k]}
                            for k, v in REF_NL["category_scores"].items()],
                revision_required=REF_NL["revision"]["required"],
                revision_reason=REF_NL["revision"]["reasons"])
checks["молча потерянная находка поймана"] = "findings_outside_score_lost" in codes(
    S.validate(rep_nl, REF_NL, None, {}))
named = dict(rep_nl, report_md=rep_nl["report_md"]
             + "\n\nНе отражено в балле: тесты 2.1, 9.2 — категории не оценивались.")
checks["названная в отчёте находка пропускается"] = (
    "findings_outside_score_lost" not in codes(S.validate(named, REF_NL, None, {})))

# --- отчёт автору
checks["пустой отчёт пойман"] = "report_missing" in codes(
    S.validate(report(report_md=""), REF, LENSES, {}))
checks["пропавший дисклеймер пойман"] = "disclaimer_missing" in codes(
    S.validate(report(report_md="# Отчёт\nчто не проверено: ничего"), REF, LENSES, {}))
checks["перефразированный дисклеймер пойман"] = "disclaimer_missing" in codes(
    S.validate(report(report_md="# Отчёт\nнепроверенного нет\nЭто советы, а не приказы."),
               REF, LENSES, {}))
# Перенос строк внутри дисклеймера не должен считаться изменением.
checks["перенос строк в дисклеймере допустим"] = "disclaimer_missing" not in codes(
    S.validate(report(report_md="# Отчёт\nнепроверенное:\n\n"
                      + S.DISCLAIMER.replace(" ", "\n", 3)), REF, LENSES, {}))
checks["пропущенный раздел о непроверенном пойман"] = "unmeasured_section_missing" in codes(
    S.validate(report(report_md="# Отчёт\nвсё отлично.\n\n" + S.DISCLAIMER),
               REF, LENSES, {}))

REF_NOSTATS = dict(REF, stats_missing=True)
checks["баланс без статистики помечается"] = "preliminary_not_marked" in codes(
    S.validate(report(), REF_NOSTATS, LENSES, {}))
checks["помеченный предварительный баланс проходит"] = "preliminary_not_marked" not in codes(
    S.validate(report(balance_score_preliminary=True), REF_NOSTATS, LENSES, {}))

# ============================================================================
# ЧАСТЬ 8. Маршрутизация под счётчиком попыток
# ============================================================================
rt = S.route({"revision_required": True, "revision_reason": ["балл 5.2"]}, REF, 0, 3)
checks["первый круг разрешён"] = rt["can_revise"] is True
checks["осталось три попытки"] = rt["attempts_left"] == 3
checks["отчёт пока не финальный"] = rt["report_ready"] is False
checks["приоритеты уходят в режим B"] = rt["to_redesigner_mode_b"] is not None

rt = S.route({"revision_required": True, "revision_reason": ["балл 5.2"]}, REF, 3, 3)
checks["бюджет исчерпан"] = rt["budget_exhausted"] is True
checks["новый круг запрещён"] = rt["can_revise"] is False
# Ключевое: отчёт всё равно выдаётся автору, а не удерживается навсегда.
checks["отчёт выдаётся и при исчерпанном бюджете"] = rt["report_ready"] is True
checks["в режим B ничего не уходит"] = rt["to_redesigner_mode_b"] is None

rt = S.route({"revision_required": False}, REF, 0, 3)
checks["без ревизии отчёт финальный"] = rt["report_ready"] is True
checks["без ревизии круг не открывается"] = rt["can_revise"] is False

# ============================================================================
# ЧАСТЬ 9. Авто-редизайн открывается вердиктом синтезатора
# ============================================================================
t = R.should_trigger({"flags": [{"code": "dead_action", "severity": "minor"}]}, None)
checks["без синтеза мелкий флаг круг не открывает"] = t["trigger"] is False

t = R.should_trigger({"flags": [{"code": "dead_action", "severity": "minor"}]},
                     {"revision_required": True, "revision_reason": ["overall_score = 5.4 < 6"]})
checks["синтезатор открывает круг без критичного флага"] = t["trigger"] is True
checks["режим B"] = t["mode"] == R.MODE_B
checks["причина синтеза в поводе"] = "5.4" in t["reason"]

t = R.should_trigger({"flags": [{"code": "exploit_loop", "severity": "critical",
                                 "confidence": "measured"}]},
                     {"revision_required": True})
checks["критичный флаг остаётся режимом A"] = t["mode"] == R.MODE_A
checks["готовый отчёт круг не открывает"] = R.should_trigger(
    {"flags": []}, {"revision_required": False})["trigger"] is False

# ============================================================================
# ЧАСТЬ 10. Разбор ответа и ретрай
# ============================================================================
wrapped = "Готово:\n\n```json\n" + json.dumps(report(), ensure_ascii=False) + "\n```"
checks["обёртка ```json разбирается"] = (S._extract_json(wrapped) or {}).get(
    "score_confidence") == "high"
checks["мусор вместо JSON отбивается"] = S._extract_json("никакого json") is None


class SynthMock(LLMProvider):
    """Подставной синтезатор: первый ответ ломает балл, второй — верный."""

    name = "mock"
    calls = 0

    def _complete(self, system, user, **opts):
        # Различаем по заголовку роли, а не по слову «синтез»: его упоминают и
        # соседние промпты (диагност передаёт находки в синтез), и мок начинал
        # отвечать за чужого агента.
        if "«Синтезатор оценки»" not in system:
            return json.dumps({"ok": True}, ensure_ascii=False)
        SynthMock.calls += 1
        if SynthMock.calls == 1:
            return json.dumps(report(overall_score=9.9), ensure_ascii=False)
        return json.dumps(report(), ensure_ascii=False)


register("mock", SynthMock)

DATA = S.build_input(LENSES, BALANCE, DIAGNOST, stats=[{"games": 100}],
                     sim_meta={"subjective_actions": []}, attempt_number=1,
                     attempts_left=3)
out = S.run(DATA)
checks["агент отработал"] = out["available"] is True
checks["ретрай был"] = SynthMock.calls == 2
checks["после ретрая замечаний нет"] = out["issues"] == []
checks["балл из ответа верный"] = out["report"]["overall_score"] == REF["total"]["overall"]

# --- сообщение агенту: эталон подан, отсутствие входов названо явно
msg = S.build_message(DATA, REF)
checks["эталонный расчёт подан"] = "ЭТАЛОННЫЙ РАСЧЁТ" in msg
checks["общий балл подан"] = str(REF["total"]["overall"]) in msg
checks["решение о ревизии подано"] = "revision_required" in msg
checks["остаток попыток подан"] = "Попыток авто-редизайна осталось: 3" in msg

msg_nl = S.build_message(
    S.build_input(None, BALANCE, DIAGNOST, stats=None, attempts_left=0), REF_NL)
checks["отсутствие линз названо отсутствием"] = "ОТСУТСТВУЕТ" in msg_nl
checks["запрет обнулять N/A в сообщении"] = "НЕ обнулены" in msg_nl
checks["отсутствие статистики названо"] = "balance_score_preliminary" in msg_nl
checks["исчерпанный бюджет назван"] = "ИСЧЕРПАН" in msg_nl

# ============================================================================
# ЧАСТЬ 11. Промпт на месте
# ============================================================================
from review import prompts  # noqa: E402

PROMPT = prompts.load_synthesizer_prompt()
checks["промпт загружается"] = len(PROMPT) > 2000
checks["дисклеймер в промпте дословно"] = S.DISCLAIMER[:60] in PROMPT
for name in CATS:
    checks[f"категория «{name[:24]}» есть в промпте"] = name in PROMPT

# ============================================================================
# ЧАСТЬ 12. Встраивание в оркестратор: порядок вызова, экран, счётчик попыток
# ============================================================================
import app as A  # noqa: E402
from models import (BalanceReport, DiagnosisReport, Document,  # noqa: E402
                    GameSkeleton, GameSpec, MirrorSession, RedesignAttempt,
                    SynthesisReport, User, db)

cl = A.app.test_client()
cl.get("/dashboard")   # создаёт гостя сессии

SPEC_ROOT = {
    "game_spec": {"core": {"players": {"min": 2, "max": 4}, "mode": "competitive",
                           "turn": {"order": "clockwise", "actions": ["move", "invest"]},
                           "win_condition": {"type": "reach", "metric": "position"},
                           "limits": {"max_rounds": 200}},
                  "text": {"concept": "Гонка.", "full_rules": "правила", "components": []}},
    "diagnostic_meta": {"actions_resolution": {"move": "probabilistic"}},
    "gaps": [], "ambiguities": []}

ESSAY = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with A.app.app_context():
    u = User.query.filter(User.tg_tag.like("@guest-%")).order_by(User.id.desc()).first()
    d = Document(user_id=u.id, filename="синтез.docx", stored_path=ESSAY,
                 file_hash="synth", doc_type="essay", version=1)
    db.session.add(d)
    db.session.commit()
    DOC = d.id
    db.session.add(MirrorSession(document_id=DOC, game_index=1,
                                 phase=MirrorSession.PHASE_CONFIRMED, ready_to_proceed=True,
                                 last_json=json.dumps({"phase": "confirmed"})))
    db.session.add(GameSpec(document_id=DOC, game_index=1, status=GameSpec.STATUS_ACCEPTED,
                            spec_json=json.dumps(SPEC_ROOT, ensure_ascii=False)))
    db.session.add(GameSkeleton(document_id=DOC, game_index=1, simulatable=True,
                                code="# ДВИЖОК\nprint('x')",
                                meta_json=json.dumps({"subjective_actions": []},
                                                     ensure_ascii=False)))
    db.session.commit()

# --- ПОРЯДОК: без вердиктов диагноста синтез закрыт ---------------------------
SynthMock.calls = 0
closed = cl.get(f"/documents/{DOC}/synthesis/1", follow_redirects=True).get_data(as_text=True)
checks["без баланса синтез закрыт"] = "Сначала нужна оценка баланса" in closed
checks["агент не звался до баланса"] = SynthMock.calls == 0

with A.app.app_context():
    br = BalanceReport.query.filter_by(document_id=DOC, game_index=1).first()
    if br is None:
        br = BalanceReport(document_id=DOC, game_index=1)
        db.session.add(br)
    br.stats_json = json.dumps([{"games": 100, "num_players": 3}], ensure_ascii=False)
    br.stats_source = BalanceReport.SOURCE_PASTED
    br.report_json = json.dumps(BALANCE, ensure_ascii=False)
    db.session.commit()

closed = cl.get(f"/documents/{DOC}/synthesis/1", follow_redirects=True).get_data(as_text=True)
checks["без диагноста синтез закрыт"] = "Сначала нужны вердикты диагноста" in closed
checks["агент не звался до диагноста"] = SynthMock.calls == 0

with A.app.app_context():
    dr = DiagnosisReport.query.filter_by(document_id=DOC, game_index=1).first()
    if dr is None:
        dr = DiagnosisReport(document_id=DOC, game_index=1)
        db.session.add(dr)
    dr.phase = DiagnosisReport.PHASE_DONE
    dr.findings_json = json.dumps(DIAGNOST, ensure_ascii=False)
    db.session.commit()

# --- синтез запускается сам при первом заходе --------------------------------
page = cl.get(f"/documents/{DOC}/synthesis/1").get_data(as_text=True)
checks["синтез прогнан при первом заходе"] = SynthMock.calls >= 1
checks["балл показан"] = "Общий балл" in page
checks["таблица «балл × вес» показана"] = "Из чего сложился балл" in page
checks["надёжность показана"] = "Насколько этой оценке можно верить" in page
checks["отчёт автору показан"] = "Отчёт автору" in page
checks["дисклеймер доехал до экрана"] = "не предписания" in page
# Линз нет — и на экране это должно быть видно как «не оценивалась», а не как ноль.
checks["неоценённые категории помечены"] = "не оценивалась (N/A)" in page
checks["разрыв по категориям показан"] = "Не оценено категорий" in page
checks["находки вне балла показаны"] = "на балл не повлияло" in page

n = SynthMock.calls
cl.get(f"/documents/{DOC}/synthesis/1")
checks["повторный заход не зовёт агента"] = SynthMock.calls == n

with A.app.app_context():
    sr = SynthesisReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["итерация первая"] = sr.attempt_number == 1
    checks["балл сохранён в колонку"] = sr.overall_score is not None
    checks["эталонный расчёт сохранён"] = bool(sr.reference())
    checks["решение о ревизии сохранено"] = sr.revision_required in (True, False)
    SCORE_1 = sr.overall_score

dl = cl.get(f"/documents/{DOC}/synthesis/1.json")
checks["отчёт качается с расчётом"] = (dl.status_code == 200
                                       and "_reference" in dl.get_data(as_text=True))

# --- пересчёт не тратит попытку ----------------------------------------------
cl.post(f"/documents/{DOC}/synthesis/1/retry", follow_redirects=True)
with A.app.app_context():
    rows = SynthesisReport.query.filter_by(document_id=DOC, game_index=1).all()
    checks["пересчёт не открыл новую итерацию"] = len(rows) == 1 and rows[0].attempt_number == 1

# --- принятая правка открывает новую итерацию и сбрасывает устаревшее ---------
with A.app.app_context():
    db.session.add(RedesignAttempt(
        document_id=DOC, game_index=1, attempt_number=1, mode=R.MODE_B,
        status=RedesignAttempt.STATUS_ACCEPTED,
        result_json=json.dumps({"changes": [], "not_touched": [
            {"finding": "сговор", "why": "не чинится параметром"}]}, ensure_ascii=False)))
    db.session.commit()
    checks["номер итерации вырос"] = A._synthesis_attempt_number(DOC, 1) == 2

# --- исчерпанный бюджет: отчёт всё равно выдаётся -----------------------------
with A.app.app_context():
    for i in (2, 3):
        db.session.add(RedesignAttempt(
            document_id=DOC, game_index=1, attempt_number=i, mode=R.MODE_B,
            status=RedesignAttempt.STATUS_ACCEPTED,
            result_json=json.dumps({"changes": []}, ensure_ascii=False)))
    db.session.commit()
    sr = SynthesisReport.query.filter_by(document_id=DOC, game_index=1).first()
    sr.revision_required = True
    sr.result_json = json.dumps(report(revision_required=True,
                                       revision_reason=["overall_score = 5.2 < 6"]),
                                ensure_ascii=False)
    db.session.commit()
    rt = A._synthesis_route(DOC, 1, sr)
    checks["бюджет ремонта исчерпан"] = rt["budget_exhausted"] is True
    checks["отчёт выдаётся несмотря на незакрытое"] = rt["report_ready"] is True
    checks["новый круг больше не предлагается"] = rt["can_revise"] is False

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
