# -*- coding: utf-8 -*-
"""Проверка агента «Диагност»: два прохода, бюджет прогонов, маршрутизация.

Самая дорогая ошибка этого шага — превратить `n/a` в `ok`: в отчёте они
выглядят одинаково, но означают противоположное. Поэтому основная масса
проверок — про полноту перечня тестов и честность причин n/a.

Плюс то, что ломается молча: пересчёт чужих вердиктов, ok по картам при
недостаточной выборке, ok по петле при неполном переборе, социальная находка
в critical_flags и обрезанный бюджетом прогон, тесты которого «потерялись».
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import diagnost as D  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402
from simulation import runner  # noqa: E402

checks = {}
REG = D.load_registry()
IDS = D.registry_ids(REG)


def codes(issues):
    return [i["code"] for i in issues]


# ============================================================================
# ЧАСТЬ 1. Реестр
# ============================================================================
checks["в реестре 50 тестов"] = len(IDS) == 50
checks["идентификаторы уникальны"] = len(set(IDS)) == 50
checks["категорий 14"] = len({t["category_no"] for t in REG}) == 14
checks["персонно-слепых ровно семь"] = D.persona_blind_tests(REG) == [
    "4.1", "4.2", "4.6", "6.1", "8.1", "13.3", "14.1"]
checks["ссылочных шесть"] = D.stats_report_tests(REG) == [
    "2.3", "4.3", "5.1", "5.4", "12.1", "12.2"]
checks["тесты по картам найдены"] = set(D.card_tests(REG)) == {
    "9.1", "9.2", "9.3", "9.4", "13.1", "13.2"}

# --- П19: реестр обязан соответствовать методичке ---------------------------
# Методичка — единственный источник правды о составе тестов. Реестр её лишь
# индексирует, и разъехавшись, они дадут вердикты по несуществующим тестам.
import io  # noqa: E402
import re as _re  # noqa: E402
from review import prompts as _prompts  # noqa: E402

METOD = _prompts.load_balance_methodology()
CANON_CATEGORIES = [
    "Смерть и неиграбельный старт",
    "Недостижимость, софтлоки и бесконечные циклы",
    "Предопределенность исхода",
    "Экономическая устойчивость и баланс стратегий",
    "Временной баланс",
    "Разрушительные и эксплуатирующие стратегии",
    "Психологические и социальные ловушки",
    "Обучающая ценность и доступность для новичков",
    "Баланс артефактов (карт)",
    "Компонентная и физическая достаточность",
    "Реиграбельность и эмоциональная дуга",
    "Масштабируемость и робастность по числу игроков",
    "Баланс контента и эталонная калибровка",
    "Прочие тесты",
]
heads = dict((int(n), t.strip()) for n, t in
             _re.findall(r"^## Категория (\d+)\. (.+)$", METOD, _re.M))
checks["в методичке 14 заголовков категорий"] = len(heads) == 14
checks["названия категорий совпадают посимвольно"] = [
    heads.get(i) for i in range(1, 15)] == CANON_CATEGORIES
# Заголовок категории 5 при переносе слипался с концом абзаца категории 4.
checks["хвоста заголовка в абзаце нет"] = "вердикт не выносится Категория 5" not in METOD

# Идентификаторы тестов: то, что перечислено в методичке, обязано быть в реестре.
metod_ids = set(_re.findall(r"^### Тест (\d+\.\d+)", METOD, _re.M))
if not metod_ids:
    metod_ids = set(_re.findall(r"\b(\d{1,2}\.\d{1,2})\b", METOD))
checks["идентификаторы реестра есть в методичке"] = set(IDS) <= metod_ids
checks["номера категорий реестра в диапазоне 1..14"] = {
    t["category_no"] for t in REG} == set(range(1, 15))

# ============================================================================
# ЧАСТЬ 2. Бюджет прогонов — держит КОД, а не модель
# ============================================================================
REQS = [
    {"request_type": "extra_player_count", "run_id": "n5", "for_tests": ["12.3"]},
    {"request_type": "seeded_deal", "run_id": "deal1", "for_tests": ["1.2"]},
    {"request_type": "catchup_off", "run_id": "cu", "for_tests": ["3.3"]},
    {"request_type": "persona_targeted", "run_id": "p1", "for_tests": ["7.2"]},
    {"request_type": "seeded_deal", "run_id": "deal2", "for_tests": ["1.3"]},
    {"request_type": "extra_player_count", "run_id": "n6", "for_tests": ["12.2"]},
    {"request_type": "extra_player_count", "run_id": "n7", "for_tests": ["12.4"]},
    {"request_type": "seeded_deal", "run_id": "deal3", "for_tests": ["1.1"]},
]
kept, dropped = D.trim_run_requests(REQS)
checks["бюджет не превышен"] = len(kept) == D.BUDGET_LIMIT
checks["обрезанное не потеряно"] = len(dropped) == 2
checks["приоритет соблюдён"] = [r["run_id"] for r in kept[:3]] == ["p1", "cu", "deal1"]
checks["в конец ушли конфигурации игроков"] = {r["run_id"] for r in dropped} == {"n6", "n7"}
checks["малый заказ не режется"] = D.trim_run_requests(REQS[:3])[1] == []

# ============================================================================
# ЧАСТЬ 3. Валидатор прохода 1
# ============================================================================
def triage(items=None, requests=None):
    return {"phase": "triage", "attempt_number": 1,
            "applicability": items if items is not None
            else [{"test": t, "status": "applicable"} for t in IDS],
            "run_requests": requests or [], "budget_used": 0,
            "budget_limit": 6, "ready_to_execute": False}


checks["полный триаж без жалоб"] = D.validate_triage(triage(), REG) == []

part = [{"test": t, "status": "applicable"} for t in IDS[:40]]
c = codes(D.validate_triage(triage(part), REG))
checks["неполный триаж пойман"] = "triage_tests_missing" in c

bad_reason = [{"test": t, "status": "applicable"} for t in IDS[1:]] + [
    {"test": IDS[0], "status": "n/a", "reason": "не подходит"}]
checks["причина n/a вне словаря поймана"] = "triage_na_reason" in codes(
    D.validate_triage(triage(bad_reason), REG))

bad_status = [{"test": t, "status": "applicable"} for t in IDS[1:]] + [
    {"test": IDS[0], "status": "skipped"}]
checks["неизвестный статус пойман"] = "triage_status_unknown" in codes(
    D.validate_triage(triage(bad_status), REG))

orphan = [{"test": t, "status": "applicable"} for t in IDS[1:]] + [
    {"test": IDS[0], "status": "needs_run", "run_id": "нет_такого"}]
checks["ссылка на несуществующий прогон поймана"] = "needs_run_no_request" in codes(
    D.validate_triage(triage(orphan), REG))

checks["перебор бюджета замечен"] = "budget_exceeded" in codes(
    D.validate_triage(triage(requests=REQS), REG))
checks["заказ прогона base отбит"] = "run_id_base" in codes(
    D.validate_triage(triage(requests=[{"request_type": "seeded_deal", "run_id": "base"}]), REG))

# ============================================================================
# ЧАСТЬ 4. Валидатор прохода 2 — тут живут дорогие ошибки
# ============================================================================
SIM_META = {"metric_responds_immediately": True, "coalition_expressible": True,
            "subjective_actions": []}
FB = {"covered_tests": [{"test": "2.3", "coverage": "full", "note": "ок"}]}
DIAG = {"runs": [{"run_id": "base", "diagnostics": {
            "card_coverage": {"sufficient": True, "pair_sufficient": True}}}],
        "exploit_search": {"3": {"search_exhausted": True}}}


def result_for(tid, **over):
    r = {"test": tid, "name": "тест", "category": "к", "verdict": "ok",
         "confidence": "measured", "severity": "n/a",
         "evidence": "поле = 0.0 при N=3", "source": "DIAG_JSON.base"}
    if tid in D.stats_report_tests(REG):
        r["source"] = "ОТЧЕТ_СТАТИСТИК, covered_tests"
    r.update(over)
    return r


def findings(results=None, **over):
    res = results if results is not None else [result_for(t) for t in IDS]
    rep = {"phase": "findings", "attempt_number": 1, "results": res,
           "critical_flags": [], "notes_for_lenses": [],
           "handed_to_recommendations": [],
           "coverage_summary": {"tests_total": 50, "ok": 50, "warning": 0,
                                "problem": 0, "n_a": 0, "n_a_breakdown": {}}}
    rep.update(over)
    return rep


def val(f, sim=None, fb=None, diag=None, dropped=None):
    return D.validate_findings(f, REG, sim or SIM_META, fb if fb is not None else FB,
                               diag if diag is not None else DIAG, dropped)


# гибридные тесты требуют вопроса линзам — добавим их в базовый отчёт
HYBRID = [t["test"] for t in REG if t["evidence_type"] == "HYBRID"]
NOTES = [{"from_test": t, "observation": "o", "question_for_lenses": "q"} for t in HYBRID]
checks["корректный отчёт без жалоб"] = val(findings(notes_for_lenses=NOTES)) == []

checks["неполный список тестов пойман"] = "results_missing" in codes(
    val(findings([result_for(t) for t in IDS[:30]], notes_for_lenses=NOTES)))

# --- n/a без причины из словаря
r = [result_for(t) for t in IDS]
r[0] = result_for(IDS[0], verdict="n/a", reason="просто так")
checks["причина n/a вне словаря поймана"] = "na_reason_unknown" in codes(
    val(findings(r, notes_for_lenses=NOTES)))

# --- вердикт без доказательства
r = [result_for(t) for t in IDS]
r[0] = result_for(IDS[0], verdict="problem", evidence="", severity="major")
checks["вердикт без числа пойман"] = "evidence_missing" in codes(
    val(findings(r, notes_for_lenses=NOTES)))

# --- problem на мягком числе
r = [result_for(t) for t in IDS]
r[0] = result_for(IDS[0], verdict="problem", confidence="assumed", severity="major")
checks["problem на мягком числе пойман"] = "problem_on_assumed" in codes(
    val(findings(r, notes_for_lenses=NOTES)))

# --- ПЕРСОННАЯ СЛЕПОТА: при metric_responds_immediately=false все семь -> n/a
blind_sim = dict(SIM_META, metric_responds_immediately=False)
c = codes(val(findings(notes_for_lenses=NOTES), sim=blind_sim))
checks["персонные тесты не помечены слепыми — поймано"] = c.count("persona_not_blind") == 7

r = [result_for(t) for t in IDS]
for i, t in enumerate(IDS):
    if t in D.persona_blind_tests(REG):
        r[i] = result_for(t, verdict="n/a", reason="method_blind")
# note обязателен: семь тестов ушли в method_blind, и это «не измерено».
# Раньше проверка проходила без него, потому что валидатор верил пустой
# n_a_breakdown из ответа модели вместо того, чтобы считать по results.
checks["корректная слепота без жалоб"] = val(
    findings(r, notes_for_lenses=NOTES,
             coverage_summary={"note": "7 персонных тестов слепы: метрика не отзывчива"}),
    sim=blind_sim) == []

# --- ПЕРЕСЧЁТ ЧУЖОГО: covered_tests full обязан быть ссылкой
r = [result_for(t) for t in IDS]
idx = IDS.index("2.3")
r[idx] = result_for("2.3", source="DIAG_JSON.runs[base].diagnostics")
checks["пересчёт чужого вердикта пойман"] = "recomputed_covered_test" in codes(
    val(findings(r, notes_for_lenses=NOTES)))

# --- КАРТЫ: ok при недостаточной выборке
poor = {"runs": [{"run_id": "base", "diagnostics": {
            "card_coverage": {"sufficient": False, "pair_sufficient": False}}}],
        "exploit_search": {"3": {"search_exhausted": True}}}
c = codes(val(findings(notes_for_lenses=NOTES), diag=poor))
checks["ok по картам без выборки пойман"] = c.count("card_ok_without_coverage") == 6

# --- ПЕТЛЯ: ok при неполном переборе
half = {"runs": DIAG["runs"], "exploit_search": {"3": {"search_exhausted": False}}}
checks["ok по петле при неполном поиске пойман"] = "exploit_ok_incomplete" in codes(
    val(findings(notes_for_lenses=NOTES), diag=half))

# --- БЮДЖЕТ: тест обрезанного прогона обязан быть виден
drp = [{"run_id": "n7", "for_tests": ["12.4"]}]
checks["потерянный тест обрезанного прогона пойман"] = "budget_test_not_marked" in codes(
    val(findings(notes_for_lenses=NOTES), dropped=drp))

r = [result_for(t) for t in IDS]
r[IDS.index("12.4")] = result_for("12.4", verdict="n/a", reason="budget_exceeded")
checks["корректная пометка бюджета без жалоб"] = val(
    findings(r, notes_for_lenses=NOTES,
             coverage_summary={"tests_total": 50, "ok": 49, "warning": 0, "problem": 0,
                               "n_a": 1, "n_a_breakdown": {"budget_exceeded": 1},
                               "note": "1 тест не выполнен: кончился бюджет прогонов"}),
    dropped=drp) == []

# --- КРИТИЧНЫЕ ФЛАГИ: только critical и ни одной социальной
c = codes(val(findings(notes_for_lenses=NOTES, critical_flags=[
    {"code": "death_spiral", "severity": "major", "confidence": "measured"}])))
checks["major в critical_flags пойман"] = "flag_not_critical" in c

c = codes(val(findings(notes_for_lenses=NOTES, critical_flags=[
    {"code": "coalition_effect", "severity": "critical", "confidence": "measured"}])))
checks["социальная находка в critical_flags поймана"] = "social_in_critical" in c

# --- СВОДКА: расхождение с пересчётом и обязательная пометка о непроверенном
c = codes(val(findings(notes_for_lenses=NOTES,
                       coverage_summary={"tests_total": 40, "ok": 40, "warning": 0,
                                         "problem": 0, "n_a": 0})))
checks["несовпадение tests_total поймано"] = "coverage_recomputed" in c

rr_na = [result_for(t) for t in IDS]
for i in range(5):
    rr_na[i] = result_for(IDS[i], verdict="n/a", reason="no_data")
c = codes(val(findings(rr_na, notes_for_lenses=NOTES,
                       coverage_summary={"tests_total": 50, "ok": 45, "warning": 0,
                                         "problem": 0, "n_a": 5,
                                         "n_a_breakdown": {"no_data": 5}})))
checks["отсутствие note о непроверенном поймано"] = "coverage_note_missing" in c

# ============================================================================
# П3. Счётчики сводки СЧИТАЕТ КОД, а не сообщает модель
# ============================================================================
# Раньше проверялась только сумма. Живой прогон дал «ok 24 / warning 7 /
# problem 6» при фактических 27/5/5: сумма сошлась, числа врали, а подложная
# n_a_breakdown через score_confidence определяла подачу балла автору.
rr = [result_for(t) for t in IDS]
rr[0] = result_for(IDS[0], verdict="warning", severity="minor")
rr[1] = result_for(IDS[1], verdict="problem", severity="major")
rr[2] = result_for(IDS[2], verdict="n/a", reason="mechanic_absent")
rr[3] = result_for(IDS[3], verdict="n/a", reason="no_data")

computed = D.recompute_coverage(rr, REG)
checks["П3: вердикты пересчитаны по results"] = (
    computed["ok"] == 46 and computed["warning"] == 1
    and computed["problem"] == 1 and computed["n_a"] == 2)
checks["П3: разбивка причин пересчитана"] = (
    computed["n_a_breakdown"] == {"mechanic_absent": 1, "no_data": 1})
checks["П3: tests_total из реестра"] = computed["tests_total"] == 50
checks["П3: нулевые причины не засоряют разбивку"] = (
    "budget_exceeded" not in computed["n_a_breakdown"])

# note — единственное поле сводки, которое остаётся за моделью: это связный
# текст о причинах, из массива его не вывести.
kept_note = D.recompute_coverage(rr, REG, {"note": "два теста не выполнены"})
checks["П3: note модели сохраняется"] = kept_note["note"] == "два теста не выполнены"

LIE = {"tests_total": 50, "ok": 24, "warning": 7, "problem": 6, "n_a": 13,
       "n_a_breakdown": {"mechanic_absent": 13}, "note": "n/a по разным причинам"}
c = codes(val(findings(rr, notes_for_lenses=NOTES, coverage_summary=LIE)))
checks["П3: расхождение счётчиков поймано"] = "coverage_recomputed" in c
checks["П3: расхождение разбивки поймано"] = "coverage_breakdown_recomputed" in c
checks["П3: сумма больше не единственная проверка"] = (
    "coverage_sum_mismatch" not in c)

disc = D.coverage_discrepancies(LIE, computed)
checks["П3: расхождение по каждому счётчику"] = len(
    [i for i in disc if i["code"] == "coverage_recomputed"]) == 4
checks["П3: все расхождения блокирующие"] = all(i["severity"] == "error" for i in disc)
checks["П3: верная сводка расхождений не даёт"] = D.coverage_discrepancies(
    computed, computed) == []

# Ключевое: синтезатор должен получить ПОСЧИТАННОЕ, а не сообщённое.
from review import synthesizer as SY  # noqa: E402
lie_conf = SY.coverage_share(LIE)
real_conf = SY.coverage_share(computed)
checks["П3: подложная сводка искажала применимые тесты"] = (
    lie_conf["applicable"] != real_conf["applicable"])
checks["П3: по посчитанной сводке применимых 49"] = real_conf["applicable"] == 49

# --- гибридный тест без вопроса линзам
checks["гибрид без вопроса линзам пойман"] = "hybrid_without_note" in codes(
    val(findings()))

# --- ВСЕ ПЯТЬ ПРИЧИН n/a должны приниматься как законные -----------------------
for reason in D.NA_REASONS:
    rr = [result_for(t) for t in IDS]
    rr[0] = result_for(IDS[0], verdict="n/a", reason=reason)
    cov = {"tests_total": 50, "ok": 49, "warning": 0, "problem": 0, "n_a": 1,
           "n_a_breakdown": {reason: 1}}
    # note обязателен только для причин, которые значат «не измерено»
    if reason != "mechanic_absent":
        cov["note"] = f"1 тест не выполнен: {reason}"
    drp = [{"run_id": "x", "for_tests": [IDS[0]]}] if reason == "budget_exceeded" else None
    checks[f"причина n/a «{reason}» принимается"] = val(
        findings(rr, notes_for_lenses=NOTES, coverage_summary=cov), dropped=drp) == []

# mechanic_absent — «спокойный» n/a, note по нему не требуется
rr = [result_for(t) for t in IDS]
rr[0] = result_for(IDS[0], verdict="n/a", reason="mechanic_absent")
checks["mechanic_absent не требует note"] = "coverage_note_missing" not in codes(val(
    findings(rr, notes_for_lenses=NOTES,
             coverage_summary={"tests_total": 50, "ok": 49, "warning": 0, "problem": 0,
                               "n_a": 1, "n_a_breakdown": {"mechanic_absent": 1}})))

# --- разбор ответа: обёртка ```json ------------------------------------------
wrapped = "Вот отчёт:\n\n```json\n" + json.dumps(triage(), ensure_ascii=False) + "\n```\n"
checks["обёртка ```json разбирается"] = (D._extract_json(wrapped) or {}).get("phase") == "triage"
checks["мусор вместо JSON отбивается"] = D._extract_json("не json вовсе") is None

# --- заказ прогона для несуществующего теста ---------------------------------
checks["прогон для чужого теста пойман"] = "run_for_unknown_test" in codes(
    D.validate_triage(triage(requests=[{"request_type": "seeded_deal", "run_id": "r1",
                                        "for_tests": ["99.9"]}]), REG))
checks["дубль run_id в заказе пойман"] = "run_id_duplicate" in codes(
    D.validate_triage(triage(requests=[
        {"request_type": "seeded_deal", "run_id": "r1"},
        {"request_type": "catchup_off", "run_id": "r1"}]), REG))

# ============================================================================
# ЧАСТЬ 5. Маршрутизация — n/a нигде не схлопывается в «проблем нет»
# ============================================================================
CS = {"tests_total": 50, "ok": 30, "warning": 3, "problem": 2, "n_a": 15,
      "n_a_breakdown": {"mechanic_absent": 9, "no_data": 3, "method_blind": 2,
                        "search_incomplete": 1, "budget_exceeded": 0},
      "note": "6 тестов не выполнены"}
rt = D.route({"critical_flags": [
                  {"code": "exploit_loop", "severity": "critical"},
                  {"code": "coalition_effect", "severity": "critical"}],
              "notes_for_lenses": [{"from_test": "8.2"}],
              "coverage_summary": CS, "results": [], "handed_to_recommendations": []})
checks["критичное уходит редизайнеру"] = [f["code"] for f in rt["to_redesigner_mode_d"]] == ["exploit_loop"]
checks["социальное не уходит редизайнеру"] = "coalition_effect" not in str(rt["to_redesigner_mode_d"])
checks["линзы ждут ремонта"] = rt["lenses_ready"] is False
checks["сводка всегда идёт в синтез"] = rt["to_synthesis"]["coverage_summary"] == CS
checks["непроверенное посчитано без mechanic_absent"] = rt["unmeasured_count"] == 6

rt2 = D.route({"critical_flags": [], "notes_for_lenses": [{"from_test": "7.1"}],
               "coverage_summary": CS})
checks["без критичного линзы готовы"] = rt2["lenses_ready"] is True
checks["заметки уходят линзам"] = len(rt2["to_lenses"]) == 1

# ============================================================================
# ЧАСТЬ 6. Исполнитель доп. прогонов
# ============================================================================
CODE = io.open("simulation/templates/game_skeleton.py", encoding="utf-8").read()
FAST = CODE.replace("GAMES_PER_CONFIG = 2000", "GAMES_PER_CONFIG = 30") \
           .replace("GAMES_PER_EXTRA_RUN = 600", "GAMES_PER_EXTRA_RUN = 15")

entry = runner.run_plan_entry({"request_type": "catchup_off", "run_id": "cu",
                               "config": {}, "player_counts": [3]})
checks["catchup_off получает обязательный ключ"] = entry["config"]["disable_catchup"] is True

patched, added = runner.add_run_plan_entries(FAST, [entry])
plan = patched.split("RUN_PLAN = [", 1)[1].split("\n]", 1)[0]
ids_in_plan = re.findall(r'"id"\s*:\s*"([^"]+)"|\'id\'\s*:\s*\'([^\']+)\'', plan)
flat = [a or b for a, b in ids_in_plan]
checks["базовый прогон остался первым"] = flat[0] == "base"
checks["новый прогон дописан в конец"] = flat[-1] == "cu"
checks["код остался валидным Python"] = __import__("ast").parse(patched) is not None
checks["дубль run_id не добавляется"] = runner.add_run_plan_entries(patched, [entry])[1] == []
checks["base перезаписать нельзя"] = runner.add_run_plan_entries(
    FAST, [{"id": "base", "config": {"x": 1}}])[1] == []
try:
    runner.add_run_plan_entries("PLAYER_COUNTS = [2]\n", [entry])
    checks["скелет без RUN_PLAN отбит"] = False
except ValueError:
    checks["скелет без RUN_PLAN отбит"] = True

res = runner.run_extra_runs(FAST, [
    {"request_type": "catchup_off", "run_id": "cu", "config": {}, "player_counts": [3]},
    {"request_type": "extra_player_count", "run_id": "n5", "config": {}, "player_counts": [5]},
], timeout=300)
checks["доп. прогоны выполнились"] = res["ok"] is True
if res["ok"]:
    checks["run_id совпали на возврате"] = sorted(res["runs"]) == ["cu", "n5"]
    checks["ни один прогон не потерян"] = res["missing_runs"] == []
    checks["заказанная конфигурация исполнена"] = (
        res["runs"]["n5"][0]["diagnostics"]["num_players"] == 5)
    checks["конфиг доехал до прогона"] = (
        res["runs"]["cu"][0]["config"].get("disable_catchup") is True)

# ============================================================================
# ЧАСТЬ 7. Встраивание в оркестратор: порядок вызова и экран
# ============================================================================
class DiagMock(LLMProvider):
    """Подставной диагност: считает вызовы и отдаёт валидные оба прохода."""

    name = "mock"
    calls = {"triage": 0, "findings": 0}

    def _complete(self, system, user, **opts):
        if "диагност игрового баланса" not in system:
            return json.dumps({"ok": True}, ensure_ascii=False)
        if "ПРОХОД 1 — ТРИАЖ" in user:
            DiagMock.calls["triage"] += 1
            items = [{"test": t, "status": "applicable"} for t in IDS]
            items[IDS.index("3.3")] = {"test": "3.3", "status": "needs_run",
                                       "run_id": "catchup_off"}
            return json.dumps({
                "phase": "triage", "attempt_number": 1, "applicability": items,
                "run_requests": [{"request_type": "catchup_off", "run_id": "catchup_off",
                                  "for_tests": ["3.3"], "config": {},
                                  "player_counts": [3], "games": 600,
                                  "why": "catch_up заявлен"}],
                "budget_used": 1, "budget_limit": 6, "ready_to_execute": False},
                ensure_ascii=False)
        DiagMock.calls["findings"] += 1
        res = [result_for(t) for t in IDS]
        res[IDS.index("2.1")] = result_for("2.1", verdict="problem", severity="critical",
                                           evidence="softlock_rate = 0.12 при N=4")
        return json.dumps({
            "phase": "findings", "attempt_number": 1, "results": res,
            "critical_flags": [{"code": "softlock", "where": "turn",
                                "detail": "0.12 при N=4", "severity": "critical",
                                "confidence": "measured", "affected_configs": [4]}],
            "notes_for_lenses": NOTES, "handed_to_recommendations": [],
            # НАМЕРЕННО ВРУЩАЯ сводка: фактически 49 ok и 1 problem. Проверяем,
            # что в базу и на экран уйдут посчитанные числа, а не эти.
            "coverage_summary": {"tests_total": 50, "ok": 24, "warning": 7, "problem": 6,
                                 "n_a": 13, "n_a_breakdown": {"mechanic_absent": 13},
                                 "note": "часть тестов не выполнена"}},
            ensure_ascii=False)


register("mock", DiagMock)

import app as A  # noqa: E402
from models import (BalanceReport, DiagnosisReport, Document,  # noqa: E402
                    GameSkeleton, GameSpec, MirrorSession, User, db)

cl = A.app.test_client()
cl.get("/dashboard")   # создаёт гостя сессии

SPEC_ROOT = {
    "game_spec": {"core": {"players": {"min": 2, "max": 4}, "mode": "competitive",
                           "elimination": True,
                           "turn": {"order": "clockwise", "actions": ["move", "invest"]},
                           "win_condition": {"type": "reach", "metric": "position"},
                           "catch_up": {"enabled": True}, "limits": {"max_rounds": 200}},
                  "text": {"concept": "Гонка.", "full_rules": "правила", "components": []}},
    "diagnostic_meta": {"actions_resolution": {"move": "probabilistic"},
                        "hand_exists": True, "tie_breaker": {"applicable": True}},
    "gaps": [], "ambiguities": []}
SK_META = {"metric_responds_immediately": True, "coalition_expressible": True,
           "subjective_actions": [], "assumptions": [], "hooks_filled": {}}
STATS_UI = [{"games": 100, "num_players": 3, "win_rate_by_seat": {"0": 0.34},
             "no_winner_rate": 0.0, "end_reason_share": {"goal_reached": 1.0},
             "rounds": {"mean": 9.0}, "action_share": {"move": 0.5},
             "avg_eliminated_per_game": 0.0}]
DIAG_UI = {"runs": [{"run_id": "base", "diagnostics": {
               "card_coverage": {"sufficient": True, "pair_sufficient": True}}}],
           "exploit_search": {"3": {"search_exhausted": True}}}

ESSAY = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with A.app.app_context():
    u = User.query.filter(User.tg_tag.like("@guest-%")).order_by(User.id.desc()).first()
    d = Document(user_id=u.id, filename="диагност.docx", stored_path=ESSAY,
                 file_hash="diag", doc_type="essay", version=1)
    db.session.add(d)
    db.session.commit()
    DOC = d.id
    db.session.add(MirrorSession(document_id=DOC, game_index=1,
                                 phase=MirrorSession.PHASE_CONFIRMED, ready_to_proceed=True,
                                 last_json=json.dumps({"phase": "confirmed"})))
    db.session.add(GameSpec(document_id=DOC, game_index=1, status=GameSpec.STATUS_ACCEPTED,
                            spec_json=json.dumps(SPEC_ROOT, ensure_ascii=False)))
    db.session.add(GameSkeleton(document_id=DOC, game_index=1, simulatable=True, code=FAST,
                                meta_json=json.dumps(SK_META, ensure_ascii=False),
                                player_counts_json=json.dumps([2, 3, 4])))
    db.session.commit()

# --- ПОРЯДОК ВЫЗОВА: без оценки баланса диагност закрыт -----------------------
closed = cl.get(f"/documents/{DOC}/diagnost/1", follow_redirects=True).get_data(as_text=True)
checks["без оценки баланса диагност закрыт"] = "Сначала нужна оценка баланса" in closed
checks["агент не звался до баланса"] = DiagMock.calls["triage"] == 0

with A.app.app_context():
    # Запись могла уже появиться: редирект с диагноста ведёт на страницу баланса,
    # а она заводит свою пустую запись. Поэтому обновляем, а не вставляем.
    br = BalanceReport.query.filter_by(document_id=DOC, game_index=1).first()
    if br is None:
        br = BalanceReport(document_id=DOC, game_index=1)
        db.session.add(br)
    br.stats_json = json.dumps(STATS_UI, ensure_ascii=False)
    br.stats_source = BalanceReport.SOURCE_PASTED
    br.diag_json = json.dumps(DIAG_UI, ensure_ascii=False)
    br.report_json = json.dumps({"flags": [], "covered_tests": [],
                                 "scores": {"balance": 7}}, ensure_ascii=False)
    db.session.commit()

# --- ПРОХОД 1 запускается сам при первом заходе, вердиктов не выносит --------
page = cl.get(f"/documents/{DOC}/diagnost/1").get_data(as_text=True)
checks["триаж прогнан при первом заходе"] = DiagMock.calls["triage"] == 1
checks["вердиктов на первом проходе нет"] = DiagMock.calls["findings"] == 0
checks["триаж показан"] = "Триаж применимости" in page
checks["заказанный прогон показан"] = "catchup_off" in page
checks["кнопка исполнения есть"] = "Выполнить прогоны и вынести вердикты" in page

with A.app.app_context():
    dr = DiagnosisReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["фаза triage сохранена"] = dr.phase == DiagnosisReport.PHASE_TRIAGE
    checks["заказ сохранён"] = [r["run_id"] for r in dr.kept_requests()] == ["catchup_off"]

cl.get(f"/documents/{DOC}/diagnost/1")
checks["повторный заход не зовёт агента"] = DiagMock.calls["triage"] == 1

# --- ПРОХОД 2: доп. прогоны + вердикты ---------------------------------------
done = cl.post(f"/documents/{DOC}/diagnost/1/execute",
               follow_redirects=True).get_data(as_text=True)
checks["вердикты вынесены"] = DiagMock.calls["findings"] == 1
checks["критичное показано"] = "softlock" in done
checks["сводка показана"] = "Что проверено, а что нет" in done
checks["переход в режим D предложен"] = "режим D" in done

with A.app.app_context():
    dr = DiagnosisReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["фаза done"] = dr.phase == DiagnosisReport.PHASE_DONE
    checks["доп. прогон реально исполнен"] = bool(dr.extra_runs())
    checks["результат под тем же run_id"] = "catchup_off" in (dr.extra_runs() or {})
    checks["критичный флаг сохранён"] = len(dr.critical_flags) == 1

    # П3 сквозным путём: модель соврала в сводке, в базу ушло посчитанное.
    cov = dr.coverage
    checks["П3: в базу ушли верные счётчики"] = (
        cov["ok"] == 49 and cov["problem"] == 1
        and cov["warning"] == 0 and cov["n_a"] == 0)
    checks["П3: враньё модели не сохранилось"] = cov["ok"] != 24
    checks["П3: разбивка пересчитана"] = cov["n_a_breakdown"] == {}
    checks["П3: note модели сохранён"] = cov.get("note") == "часть тестов не выполнена"
    checks["П3: расхождение записано в замечания"] = "coverage_recomputed" in [
        i["code"] for i in dr.issues()]
    # Непроверенных нет — синтезатор получит честную картину.
    checks["П3: непроверенных ноль"] = dr.unmeasured_count == 0
    # Бюджет прогонов — по факту исполненного.
    checks["П3: budget_used считает код"] = dr.budget_used == 1
    checks["П3: лимит бюджета доступен"] = dr.budget_limit == D.BUDGET_LIMIT

dl = cl.get(f"/documents/{DOC}/diagnost/1.json")
checks["Findings_diagnost качается"] = (
    dl.status_code == 200 and "critical_flags" in dl.get_data(as_text=True))

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
