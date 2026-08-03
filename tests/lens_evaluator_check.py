# -*- coding: utf-8 -*-
"""Проверка агента «Оценщик по линзам»: ядро, средние, вопросы, плейтест.

Самая дорогая ошибка этого шага не «неточная оценка» — суждение и не обязано
быть точным. Дороги три молчаливых сдвига, которые в готовом отчёте выглядят
как обычная работа:

1. N/A, посчитанный нулём: категория занижена за то, что её нельзя было
   оценить, и по готовому среднему это не отличить от честного низкого балла;
2. имя категории с лишним словом: синтезатор ищет вес ПО ИМЕНИ и молча
   выкидывает ненайденную категорию из итогового балла вместе с находками;
3. потерянный вопрос диагноста: гибридный тест методички повисает — число
   посчитано, а вердикта никто не вынес.

Плюс проверяется то, ради чего агент вообще нужен: рекомендация о живом
плейтесте при субъективных механиках и невыразимом сговоре.
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import lens_evaluator as L  # noqa: E402
from review import prompts  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

checks = {}


def codes(issues):
    return [i["code"] for i in issues]


# ============================================================================
# ЧАСТЬ 1. Ядро линз и его согласие с промптом
# ============================================================================
checks["категорий девять"] = len(L.CORE) == 9
checks["линз в ядре 47"] = len(L.CORE_LENSES) == 47
checks["номера линз уникальны"] = len(L.CORE_LENSES) == len(set(L.CORE_LENSES))

# Источник истины — таблица ядра в промпте. Копия в коде обязана ей отвечать:
# разъехавшись, они дадут оценку по одному набору линз и проверку по другому.
PROMPT = prompts.load_lenses_prompt()
table = {}
for name, nums in re.findall(r"^\| ([^|]+?) \| ([0-9, ]+) \|$", PROMPT, re.M):
    name = name.strip()
    if name == "Категория":
        continue
    table[re.sub(r"\s*\(.*?\)\s*$", "", name)] = sorted(int(n) for n in nums.split(","))
checks["таблица ядра найдена в промпте"] = len(table) == 9
checks["состав ядра совпадает с промптом"] = {
    k: sorted(v) for k, v in L.CORE.items()} == table

# Имена категорий — те же строки, по которым синтезатор ищет вес.
from review import synthesizer as S  # noqa: E402

checks["имена категорий совпадают с весами синтезатора"] = (
    set(L.CORE) == set(S.CATEGORY_WEIGHTS))
# Скобочные пояснения из таблицы промпта в канонические имена не попали.
checks["в именах нет скобочных пояснений"] = not any("(" in k for k in L.CORE)

checks["линза 54 в структурной целостности"] = (
    54 in L.CORE["Структурная целостность и физический интерфейс"])
checks["линзы обучающей цели в замысле"] = all(
    n in L.CORE["Замысел и образовательная ценность"] for n in L.EDUCATION_LENSES)
checks["категория ищется по номеру линзы"] = (
    L.category_of(32) == "Механика и решения игрока" and L.category_of(999) is None)

names = L.lens_names()
checks["названия линз разобраны из справочника"] = len(names) >= 100
checks["название линзы 54 верное"] = "Физического интерфейса" in (names.get(54) or "")

# ============================================================================
# ЧАСТЬ 2. Условие вызова — считает КОД
# ============================================================================
DIAG_CLEAN = {"results": [{"test": "1.1", "verdict": "ok"}], "critical_flags": [],
              "coverage_summary": {"tests_total": 50, "n_a_breakdown": {}}}
DIAG_CRIT = dict(DIAG_CLEAN, critical_flags=[
    {"code": "exploit_loop", "severity": "critical", "detail": "петля invest→sell"}])

checks["без критичного линзы готовы"] = L.should_run(DIAG_CLEAN)["ready"] is True
checks["критичное блокирует линзы"] = L.should_run(DIAG_CRIT)["ready"] is False
checks["код блокировки назван"] = "exploit_loop" in L.should_run(DIAG_CRIT)["reason"]
checks["без вердиктов диагноста линзы не идут"] = L.should_run({})["ready"] is False
# major и minor линзы не блокируют — они ждут синтеза, а не ремонта.
checks["major не блокирует"] = L.should_run(dict(
    DIAG_CLEAN, critical_flags=[{"code": "x", "severity": "major"}]))["ready"] is True

# ============================================================================
# ЧАСТЬ 3. Объединение заметок из ДВУХ источников
# ============================================================================
STATS_NOTES = ["action_share(pitch)=0.68 — доля высока, но исход смоделирован случайно"]
DIAG_NOTES = [
    {"from_test": "8.2", "observation": "персоны не различаются",
     "question_for_lenses": "оценить обучающую ценность содержательно"},
    {"from_test": "7.1", "observation": "сговор механически невыразим",
     "question_for_lenses": "оценить риск сговора по тексту правил"},
    {"from_test": "11.1", "observation": "смен лидера мало"},   # без вопроса
]
merged = L.merge_notes(STATS_NOTES, DIAG_NOTES)
checks["заметки обоих источников слиты"] = len(merged) == 4
checks["источник заметки сохранён"] = (
    merged[0]["source"] == "stats_evaluator" and merged[1]["source"] == "diagnost")
checks["строка статистик стала записью"] = "action_share" in merged[0]["observation"]
# Ответа требуют только записи с вопросом — иначе агент отвечал бы на наблюдения.
checks["вопросов ровно два"] = len(L.open_questions(merged)) == 2
checks["наблюдение без вопроса ответа не требует"] = "11.1" not in {
    q["from_test"] for q in L.open_questions(merged)}
checks["пустые источники не падают"] = L.merge_notes(None, None) == []

# --- непроверенное: mechanic_absent не в счёт --------------------------------
COV = {"tests_total": 50, "n_a_breakdown": {
    "mechanic_absent": 9, "no_data": 3, "method_blind": 2, "budget_exceeded": 0}}
un = L.unmeasured_areas(COV)
checks["mechanic_absent не считается непроверенным"] = "mechanic_absent" not in un
checks["непроверенное посчитано"] = un == {"no_data": 3, "method_blind": 2}
checks["нулевые причины не засоряют"] = "budget_exceeded" not in un

# ============================================================================
# ЧАСТЬ 4. Средние по категориям — N/A НЕ ноль
# ============================================================================
checks["среднее по оценённым"] = L.category_average(
    [{"n": 1, "score": 8}, {"n": 3, "score": 6}]) == 7.0
# Ключевое: три N/A не тянут среднее вниз.
checks["N/A не входит в среднее"] = L.category_average(
    [{"n": 1, "score": 8}, {"n": 3, "score": 8},
     {"n": 5, "na": True}, {"n": 17, "na": True}, {"n": 97, "score": None}]) == 8.0
checks["ноль вместо N/A дал бы другое"] = L.category_average(
    [{"n": 1, "score": 8}, {"n": 3, "score": 8},
     {"n": 5, "score": 0}, {"n": 17, "score": 0}, {"n": 97, "score": 0}]) != 8.0
checks["категория целиком N/A даёт None"] = L.category_average(
    [{"n": 1, "na": True}, {"n": 3, "score": None}]) is None
checks["пустая категория даёт None"] = L.category_average([]) is None
checks["строковый N/A распознан"] = L.is_na({"n": 1, "score": "N/A"}) is True
checks["оценка не считается N/A"] = L.is_na({"n": 1, "score": 4}) is False

# ============================================================================
# ЧАСТЬ 5. Плейтест — обязателен по фактам, а не по памяти агента
# ============================================================================
checks["субъективные действия требуют плейтеста"] = L.needs_playtest(
    {"subjective_actions": ["pitch"]})["required"] is True
checks["невыразимый сговор требует плейтеста"] = L.needs_playtest(
    {"coalition_expressible": False})["required"] is True
checks["без того и другого плейтест не обязателен"] = L.needs_playtest(
    {"subjective_actions": [], "coalition_expressible": True})["required"] is False
checks["причина плейтеста названа"] = "pitch" in "; ".join(
    L.needs_playtest({"subjective_actions": ["pitch"]})["reasons"])

# ============================================================================
# ЧАСТЬ 6. Валидатор
# ============================================================================
def lens(n, score=7, basis="есть фраза в правилах", **kw):
    d = {"n": n, "name": (names.get(n) or "линза"), "score": score, "basis": basis}
    d.update(kw)
    return d


def full_report(**over):
    """Корректный ответ: все 47 линз ядра оценены, средние посчитаны верно."""
    cats = []
    for name, nums in L.CORE.items():
        lenses = [lens(n) for n in nums]
        cats.append({"name": name, "lenses": lenses,
                     "category_avg_preliminary": L.category_average(lenses)})
    rep = {
        "categories": cats,
        "answers_to_diagnost": [
            {"from_test": "8.2", "question": "…", "verdict": "warning",
             "answer": "учит подаче, а не разбору", "lenses_used": [97, 98]},
            {"from_test": "7.1", "question": "…", "verdict": "ok",
             "answer": "механической опоры для сговора нет", "lenses_used": [36, 37, 38]},
        ],
        "findings": [{"lens": 32, "type": "problem", "text": "выбор не осмыслен"}],
        "unmeasured_noted": [{"area": "убедительность", "why": "субъективно",
                              "lens_view": "оценено по замыслу"}],
        "playtest_recommended": True,
        "playtest_reason": "ключевая механика разрешается субъективным суждением",
        "out_of_core_used": [],
    }
    rep.update(over)
    return rep


DATA = L.build_input(
    {"game_spec": {"core": {"players": {"min": 2, "max": 4}}, "text": {}},
     "diagnostic_meta": {"actions_resolution": {"pitch": "subjective_judgment"}}},
    canonical_text="Правила игры.",
    stats_notes=STATS_NOTES, diagnost_notes=DIAG_NOTES,
    sim_meta={"subjective_actions": ["pitch"], "coalition_expressible": False},
    coverage_summary=COV,
)

checks["корректный отчёт замечаний не даёт"] = L.validate(full_report(), DATA) == []

# --- имена категорий: главная тихая поломка ---------------------------------
bad_name = full_report()
bad_name["categories"][3]["name"] = "Экономика и баланс (ключевая для фин-игры)"
c = codes(L.validate(bad_name, DATA))
checks["скобочное пояснение в имени поймано"] = "category_name_unknown" in c
checks["подсказано, что это за категория"] = "Экономика и баланс" in "".join(
    i["message"] for i in L.validate(bad_name, DATA) if i["code"] == "category_name_unknown")
checks["пропавшая категория поймана"] = "category_missing" in c

dropped = full_report()
dropped["categories"] = dropped["categories"][:-1]
checks["выпавшая категория поймана"] = "category_missing" in codes(
    L.validate(dropped, DATA))

# --- полнота ядра ------------------------------------------------------------
short = full_report()
short["categories"][0]["lenses"] = short["categories"][0]["lenses"][:2]
short["categories"][0]["category_avg_preliminary"] = L.category_average(
    short["categories"][0]["lenses"])
checks["недостающие линзы пойманы"] = "lenses_missing" in codes(L.validate(short, DATA))

extra = full_report()
extra["categories"][0]["lenses"].append(lens(88))
extra["categories"][0]["category_avg_preliminary"] = L.category_average(
    extra["categories"][0]["lenses"])
checks["линза вне ядра поймана"] = "lens_out_of_core" in codes(L.validate(extra, DATA))
declared = full_report(out_of_core_used=[88])
declared["categories"][0]["lenses"].append(lens(88))
declared["categories"][0]["category_avg_preliminary"] = L.category_average(
    declared["categories"][0]["lenses"])
checks["объявленная линза вне ядра пропускается"] = "lens_out_of_core" not in codes(
    L.validate(declared, DATA))

# --- шкала -------------------------------------------------------------------
zero = full_report()
zero["categories"][1]["lenses"][0] = lens(7, score=0)
zero["categories"][1]["category_avg_preliminary"] = L.category_average(
    zero["categories"][1]["lenses"])
checks["ноль вне шкалы пойман"] = "score_out_of_range" in codes(L.validate(zero, DATA))
checks["про ноль сказано, что это не N/A"] = "N/A" in "".join(
    i["message"] for i in L.validate(zero, DATA) if i["code"] == "score_out_of_range")

na_zero = full_report()
na_zero["categories"][1]["lenses"][0] = lens(7, score=0, na=True, na_reason="нет данных")
na_zero["categories"][1]["category_avg_preliminary"] = L.category_average(
    na_zero["categories"][1]["lenses"])
checks["N/A с нулём пойман"] = "na_as_zero" in codes(L.validate(na_zero, DATA))

no_basis = full_report()
no_basis["categories"][1]["lenses"][0] = lens(7, basis="")
checks["оценка без обоснования поймана"] = "basis_missing" in codes(
    L.validate(no_basis, DATA))

na_silent = full_report()
na_silent["categories"][1]["lenses"][0] = {"n": 7, "score": None}
na_silent["categories"][1]["category_avg_preliminary"] = L.category_average(
    na_silent["categories"][1]["lenses"])
checks["N/A без причины пойман"] = "na_without_reason" in codes(
    L.validate(na_silent, DATA))

# --- правило-исключение: обучающая цель --------------------------------------
edu = full_report()
cat0 = edu["categories"][0]
cat0["lenses"] = [lens(n) if n not in L.EDUCATION_LENSES
                  else lens(n, score=None, na=True, na_reason="цель не заявлена")
                  for n in L.CORE["Замысел и образовательная ценность"]]
cat0["category_avg_preliminary"] = L.category_average(cat0["lenses"])
c = codes(L.validate(edu, DATA))
checks["N/A у обучающей цели пойман"] = c.count("education_na") == 2

# --- средние: считает код ----------------------------------------------------
lied = full_report()
lied["categories"][0]["category_avg_preliminary"] = 3.2   # как если бы N/A были нулём
checks["враньё в среднем поймано"] = "avg_recomputed" in codes(L.validate(lied, DATA))
checks["в сообщении названа причина"] = "N/A посчитали нулём" in "".join(
    i["message"] for i in L.validate(lied, DATA) if i["code"] == "avg_recomputed")

empty_avg = full_report()
empty_avg["categories"][6]["lenses"] = [
    lens(n, score=None, na=True, na_reason="одиночная игра") for n in L.CORE["Социальное взаимодействие"]]
empty_avg["categories"][6]["category_avg_preliminary"] = 5.0
checks["среднее у пустой категории поймано"] = "avg_on_empty_category" in codes(
    L.validate(empty_avg, DATA))

# --- вопросы диагноста -------------------------------------------------------
lost_q = full_report()
lost_q["answers_to_diagnost"] = lost_q["answers_to_diagnost"][:1]
c = codes(L.validate(lost_q, DATA))
checks["потерянный вопрос пойман"] = "question_unanswered" in c
checks["сказано про повисший тест"] = "повиснет" in "".join(
    i["message"] for i in L.validate(lost_q, DATA) if i["code"] == "question_unanswered")

ghost = full_report()
ghost["answers_to_diagnost"].append(
    {"from_test": "99.9", "answer": "…", "lenses_used": [1]})
checks["ответ без вопроса пойман"] = "answer_without_question" in codes(
    L.validate(ghost, DATA))

no_lens = full_report()
no_lens["answers_to_diagnost"][0]["lenses_used"] = []
checks["ответ без ссылки на линзу пойман"] = "answer_without_lens" in codes(
    L.validate(no_lens, DATA))

# --- плейтест и непроверенное ------------------------------------------------
c = codes(L.validate(full_report(playtest_recommended=False), DATA))
checks["пропущенный плейтест пойман"] = "playtest_missing" in c
checks["плейтест без причины пойман"] = "playtest_without_reason" in codes(
    L.validate(full_report(playtest_reason=""), DATA))
checks["потерянное непроверенное поймано"] = "unmeasured_not_noted" in codes(
    L.validate(full_report(unmeasured_noted=[]), DATA))

# --- одиночная игра ----------------------------------------------------------
SOLO = L.build_input({"game_spec": {"core": {"players": {"min": 1, "max": 1}}}},
                     diagnost_notes=DIAG_NOTES, sim_meta={"subjective_actions": ["pitch"]},
                     coverage_summary=COV)
checks["социальные линзы в соло пойманы"] = "social_scored_solo" in codes(
    L.validate(full_report(), SOLO))

# ============================================================================
# ЧАСТЬ 7. Пересчёт и срез для синтезатора
# ============================================================================
rep = full_report()
rep["categories"][6]["lenses"] = [
    lens(n, score=None, na=True, na_reason="одиночная игра")
    for n in L.CORE["Социальное взаимодействие"]]
recomputed = L.recompute_categories(rep)
social = next(c for c in recomputed if c["name"] == "Социальное взаимодействие")
checks["категория целиком N/A помечена"] = social["na"] is True
checks["у N/A-категории нет среднего"] = social["category_avg_preliminary"] is None
checks["счётчик оценённых линз верен"] = social["scored"] == 0 and social["total"] == 3

synth = L.to_synthesis(rep)
checks["срез отдаёт девять категорий"] = len(synth["categories"]) == 9
checks["срез несёт плейтест"] = synth["playtest_recommended"] is True
checks["срез несёт ответы диагносту"] = len(synth["answers_to_diagnost"]) == 2
checks["в срезе нет разбора по линзам"] = "lenses" not in synth["categories"][0]

# Ключевое: синтезатор обязан принять этот срез и посчитать по нему балл.
REF = S.compute_reference(synth, {"scores": {"balance": 7}, "flags": []},
                          DIAG_CLEAN, {"subjective_actions": []})
checks["синтезатор принял линзы"] = REF["total"]["overall"] is not None
scored_names = {r["category"] for r in REF["total"]["rows"] if not r["na"]}
checks["восемь категорий вошли в балл"] = len(scored_names) == 8
checks["N/A-категория в балл не вошла"] = "Социальное взаимодействие" not in scored_names
checks["ни одна категория не потерялась по имени"] = all(
    r["category"] in L.CORE for r in REF["total"]["rows"])

# ============================================================================
# ЧАСТЬ 8. Вызов агента и ретрай
# ============================================================================
msg = L.build_message(DATA)
checks["в сообщении есть ядро"] = "РАБОЧЕЕ ЯДРО" in msg and "47 линз" in msg
checks["в сообщении есть вопросы"] = "ТРЕБУЮЩИЕ ТВОЕГО ВЕРДИКТА" in msg
checks["в сообщении есть канонический текст"] = "Правила игры." in msg
checks["в сообщении названы мягкие действия"] = "pitch" in msg
checks["в сообщении есть непроверенное"] = "НЕ ИЗМЕРЕНО" in msg
checks["в сообщении требование к именам"] = "молча выкинет" in msg
# Полные отчёты числовых агентов на вход НЕ подаются.
checks["полного отчёта баланса в сообщении нет"] = "priority_fixes" not in msg
checks["полного отчёта диагноста в сообщении нет"] = "critical_flags" not in msg


class LensMock(LLMProvider):
    """Первый ответ занижает среднее (как если бы N/A был нулём), второй верный."""

    name = "mock"
    calls = 0

    def _complete(self, system, user, **opts):
        if "Оценщик по линзам" not in system:
            return json.dumps({"ok": True}, ensure_ascii=False)
        LensMock.calls += 1
        if LensMock.calls == 1:
            bad = full_report()
            bad["playtest_recommended"] = False        # блокирующее — вызовет ретрай
            bad["categories"][0]["category_avg_preliminary"] = 3.2
            return json.dumps(bad, ensure_ascii=False)
        return json.dumps(full_report(), ensure_ascii=False)


register("mock", LensMock)

out = L.run(DATA)
checks["агент отработал"] = out["available"] is True
checks["ретрай был"] = LensMock.calls == 2
checks["после ретрая замечаний нет"] = out["issues"] == []
checks["категории пересчитаны в ответе"] = all(
    "scored" in c for c in out["report"]["categories"])

# Расхождение в среднем само по себе ретрай НЕ запускает: значение известно точно.
LensMock.calls = 0


class AvgOnlyMock(LLMProvider):
    name = "mock"
    calls = 0

    def _complete(self, system, user, **opts):
        if "Оценщик по линзам" not in system:
            return json.dumps({"ok": True}, ensure_ascii=False)
        AvgOnlyMock.calls += 1
        bad = full_report()
        bad["categories"][0]["category_avg_preliminary"] = 3.2
        return json.dumps(bad, ensure_ascii=False)


register("mock", AvgOnlyMock)
out2 = L.run(DATA)
checks["на расхождение среднего ретрая нет"] = AvgOnlyMock.calls == 1
checks["расхождение осталось замечанием"] = "avg_recomputed" in codes(out2["issues"])
checks["в отчёт ушло посчитанное"] = out2["report"]["categories"][0][
    "category_avg_preliminary"] == L.category_average(
        out2["report"]["categories"][0]["lenses"])

# ============================================================================
# ЧАСТЬ 9. П2 — находки по неоценённым категориям
# ============================================================================
# Без линз находки диагноста по чисто-качественным категориям снижать нечего:
# они уходят в отдельный блок «Найдено, но на балл не повлияло». С линзами тот
# же блок обязан опустеть САМ — категории теперь оценены, и находки снижают их
# баллы по таблице соответствия синтезатора.
DIAG_WITH_FINDINGS = {
    "results": [
        # 2.x → Структурная целостность, 3.x → Навык/шанс, 8.x → Замысел
        {"test": "2.6", "verdict": "problem", "severity": "major",
         "confidence": "measured", "evidence": "путь attrition недостижим"},
        {"test": "3.2", "verdict": "problem", "severity": "major",
         "confidence": "measured", "evidence": "спираль смерти"},
        {"test": "8.1", "verdict": "problem", "severity": "major",
         "confidence": "measured", "evidence": "разрыв мастерства 90 п.п."},
    ],
    "critical_flags": [],
    "coverage_summary": {"tests_total": 50, "n_a_breakdown": {}},
}
BAL = {"scores": {"balance": 8}, "flags": []}

# --- прогон БЕЗ линз: находки не влияют на балл, но не теряются --------------
ref_no = S.compute_reference(None, BAL, DIAG_WITH_FINDINGS, {})
outside_no = {p.get("test") for p in ref_no["penalties_outside_scored"]}
checks["П2: без линз находки не влияют на балл"] = outside_no == {"2.6", "3.2", "8.1"}
checks["П2: без линз оценена одна категория"] = len(
    [r for r in ref_no["total"]["rows"] if not r["na"]]) == 1
checks["П2: без линз доверие низкое"] = ref_no["coverage"]["confidence"] == "low"

# --- прогон С линзами: тот же блок опустел, находки снизили баллы ------------
full_synth = L.to_synthesis(L.recompute_categories(full_report()) and full_report())
ref_yes = S.compute_reference(full_synth, BAL, DIAG_WITH_FINDINGS, {})
checks["П2: с линзами блок «не повлияло» пуст"] = ref_yes["penalties_outside_scored"] == []
applied_tests = {p.get("test") for p in ref_yes["penalties"]}
checks["П2: с линзами все находки применены"] = applied_tests == {"2.6", "3.2", "8.1"}
checks["П2: с линзами оценены все девять"] = len(
    [r for r in ref_yes["total"]["rows"] if not r["na"]]) == 9
checks["П2: с линзами доверие выше низкого"] = ref_yes["coverage"]["confidence"] != "low"

# Находки действительно СНИЗИЛИ соответствующие категории, а не просто «учтены».
scores = ref_yes["category_scores"]
checks["П2: структурная целостность снижена"] = (
    scores["Структурная целостность и физический интерфейс"]["score"] < 7.0)
checks["П2: навык/шанс снижен"] = scores["Навык, шанс, напряжение"]["score"] < 7.0
checks["П2: замысел снижен"] = scores["Замысел и образовательная ценность"]["score"] < 7.0
checks["П2: не задетая категория осталась прежней"] = (
    scores["Реиграбельность и нарратив"]["score"] == 7.0)

# Блок НЕ удалён из кода: он нужен, когда категория честно N/A даже при линзах
# (например, социальные линзы в одиночной игре).
solo_lenses = L.to_synthesis(full_report())
for cat in solo_lenses["categories"]:
    if cat["name"] == "Социальное взаимодействие":
        cat["na"], cat["category_avg_preliminary"] = True, None
DIAG_SOCIAL = dict(DIAG_WITH_FINDINGS, results=[
    {"test": "7.1", "verdict": "problem", "severity": "major",
     "confidence": "measured", "evidence": "сговор поднимает винрейт"}])
ref_solo = S.compute_reference(solo_lenses, BAL, DIAG_SOCIAL, {})
checks["П2: при честном N/A блок снова работает"] = [
    p.get("test") for p in ref_solo["penalties_outside_scored"]] == ["7.1"]

# ============================================================================
# ЧАСТЬ 10. Встраивание в оркестратор: порядок, экран, сброс после правки
# ============================================================================
register("mock", LensMock)
LensMock.calls = 0

import app as A  # noqa: E402
from models import (BalanceReport, DiagnosisReport, Document,  # noqa: E402
                    GameSkeleton, GameSpec, LensReport, MirrorSession,
                    RedesignAttempt, User, db)

cl = A.app.test_client()
cl.get("/dashboard")

SPEC_ROOT = {
    "game_spec": {"core": {"players": {"min": 2, "max": 4}, "mode": "competitive",
                           "turn": {"order": "clockwise", "actions": ["move", "pitch"]},
                           "win_condition": {"type": "most", "metric": "points"},
                           "limits": {"max_rounds": 20}},
                  "text": {"concept": "Гонка идей.", "full_rules": "Правила.",
                           "components": [{"name": "карта", "qty": 40,
                                           "function": "эффект события"}]}},
    "diagnostic_meta": {"actions_resolution": {"move": "deterministic",
                                               "pitch": "subjective_judgment"}},
    "gaps": [], "ambiguities": []}
SK_META = {"subjective_actions": ["pitch"], "coalition_expressible": False,
           "metric_responds_immediately": True, "assumptions": ["питч смоделирован случайно"]}
ESSAY = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"

with A.app.app_context():
    u = User.query.filter(User.tg_tag.like("@guest-%")).order_by(User.id.desc()).first()
    UID = u.id
    d = Document(user_id=UID, filename="линзы.docx", stored_path=ESSAY,
                 file_hash="lenses-flow", doc_type="essay", version=1)
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
                                meta_json=json.dumps(SK_META, ensure_ascii=False)))
    db.session.add(BalanceReport(
        document_id=DOC, game_index=1,
        stats_json=json.dumps([{"games": 100, "num_players": 3}], ensure_ascii=False),
        stats_source=BalanceReport.SOURCE_PASTED,
        report_json=json.dumps(dict(BAL, notes_for_lenses=STATS_NOTES), ensure_ascii=False),
        issues_json=json.dumps([])))
    db.session.commit()

# --- ПОРЯДОК: без вердиктов диагноста линзы закрыты --------------------------
closed = cl.get(f"/documents/{DOC}/lenses/1", follow_redirects=True).get_data(as_text=True)
checks["без диагноста линзы закрыты"] = "Сначала нужны вердикты диагноста" in closed
checks["агент не звался до диагноста"] = LensMock.calls == 0

# --- критичная находка диагноста блокирует оценку ----------------------------
with A.app.app_context():
    dr = DiagnosisReport.query.filter_by(document_id=DOC, game_index=1).first()
    if dr is None:
        dr = DiagnosisReport(document_id=DOC, game_index=1)
        db.session.add(dr)
    dr.phase = DiagnosisReport.PHASE_DONE
    dr.findings_json = json.dumps(dict(DIAG_CRIT, notes_for_lenses=DIAG_NOTES),
                                  ensure_ascii=False)
    db.session.commit()

blocked = cl.get(f"/documents/{DOC}/lenses/1").get_data(as_text=True)
checks["критичное блокирует экран"] = "пока не запускается" in blocked
checks["названа причина блокировки"] = "exploit_loop" in blocked
checks["предложен ремонт"] = "К авто-редизайну" in blocked
checks["при критичном агент не звался"] = LensMock.calls == 0

# --- критичного нет: оценка запускается сама ---------------------------------
with A.app.app_context():
    dr = DiagnosisReport.query.filter_by(document_id=DOC, game_index=1).first()
    dr.findings_json = json.dumps(dict(DIAG_CLEAN, notes_for_lenses=DIAG_NOTES),
                                  ensure_ascii=False)
    db.session.commit()

page = cl.get(f"/documents/{DOC}/lenses/1").get_data(as_text=True)
checks["агент прогнан при первом заходе"] = LensMock.calls >= 1
checks["баллы категорий показаны"] = "Предварительные баллы по категориям" in page
checks["ответы диагносту показаны"] = "Ответы на вопросы диагноста" in page
checks["плейтест показан"] = "Рекомендован живой плейтест" in page
checks["разбор по линзам доступен"] = "Показать разбор по всем" in page
checks["правило про N/A объяснено"] = "не входит" in page

n = LensMock.calls
cl.get(f"/documents/{DOC}/lenses/1")
checks["повторный заход не зовёт агента"] = LensMock.calls == n

with A.app.app_context():
    lr = LensReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["отчёт сохранён"] = bool(lr.result())
    checks["оценены все девять категорий"] = lr.scored_categories == 9
    checks["плейтест сохранён"] = lr.playtest_recommended is True
    checks["самопроверка чиста"] = lr.issues() == []

dl = cl.get(f"/documents/{DOC}/lenses/1.json")
checks["Findings_lenses качается"] = (
    dl.status_code == 200 and "categories" in dl.get_data(as_text=True))

# --- КЛЮЧЕВОЕ: синтезатор теперь получает линзы, а не None -------------------
with A.app.app_context():
    got = A._lenses_findings(DOC, 1)
    checks["синтезатор получает линзы"] = got is not None
    checks["в срезе девять категорий"] = len(got["categories"]) == 9
    checks["срез несёт ответы диагносту"] = len(got["answers_to_diagnost"]) == 2

# --- принятая правка сбрасывает оценку по линзам -----------------------------
with A.app.app_context():
    lr = LensReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["до правки оценка на месте"] = lr.result_json is not None
    # Имитируем принятие правки тем же кодом, что и маршрут решения автора.
    br = BalanceReport.query.filter_by(document_id=DOC, game_index=1).first()
    br.report_json = json.dumps(dict(BAL, flags=[
        {"code": "unreachable_win", "severity": "critical", "confidence": "measured",
         "where": "core.win_condition", "detail": "32% при N=4"}]), ensure_ascii=False)
    db.session.add(RedesignAttempt(
        document_id=DOC, game_index=1, attempt_number=1, mode="A_technical",
        status=RedesignAttempt.STATUS_PROPOSED,
        result_json=json.dumps({"changes": [], "needs_resimulation": True},
                               ensure_ascii=False)))
    db.session.commit()

cl.post(f"/documents/{DOC}/redesign/1/decide", data={"action": "accept"})
with A.app.app_context():
    lr = LensReport.query.filter_by(document_id=DOC, game_index=1).first()
    checks["правка сбросила оценку по линзам"] = lr.result_json is None
    checks["после сброса синтезатор снова без линз"] = A._lenses_findings(DOC, 1) is None

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
