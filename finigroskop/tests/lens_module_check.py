# -*- coding: utf-8 -*-
"""Проверка оценки ОДНОГО модуля игры по линзам — вход для «Генератора игр».

Обращений к модели нет: провайдер подменён заглушкой.

Три ошибки этого шага дороже всех остальных, и все три выглядят как нормальная
работа:

1. Балл модуля неотличим от балла игры. Категории целиком в N/A выпадают и из
   числителя, и из знаменателя взвешенного среднего, поэтому 8.4 за один модуль
   механик считается по четырём категориям, а 8.4 за игру — по девяти. Число
   одно, стоит за ним разное. Лечится не формулой, а честно показанным
   непокрытым весом — и здесь проверяется, что он показан.

2. Линзы позвали по модулю, который всё равно уйдёт на перегенерацию. Оценка
   устареет в момент правки, а деньги за вызов уже потрачены.

3. Обучающие линзы 97/98 применены к развлекательной игре. В ФинИгроСкопе им
   запрещён N/A — там всякая игра финансово-образовательная. Генератор делает
   игры любые, и такой запрет обрушил бы балл в самой тяжёлой категории (вес
   1.5) за то, чего игра никогда не обещала.
"""
import io
import json
import os
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import lens_evaluator as L        # noqa: E402
from review import lens_module as M           # noqa: E402
from review import lens_queue                 # noqa: E402
from review import lens_scope as SC           # noqa: E402
from review import synthesizer as S           # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

checks = {}


# ============================================================================
# ЧАСТЬ 1. Область применимости — таблица, а не догадка
# ============================================================================
checks["фазы совпадают с этапами ТЗ генератора"] = SC.known_phases() == [
    "components", "features", "mechanics", "rules", "story"]

for phase in SC.known_phases():
    core = SC.core_for(phase)
    checks[f"[{phase}] категории — подмножество ядра"] = set(core) <= set(L.CORE)
    checks[f"[{phase}] линзы взяты из ядра без выдумок"] = all(
        set(nums) == set(L.CORE[name]) for name, nums in core.items())
    checks[f"[{phase}] область непустая"] = len(core) > 0

# Последний этап собирает описание игры целиком — там область обязана быть полной.
checks["у правил область — всё ядро"] = set(SC.core_for("rules")) == set(L.CORE)
checks["у механик область УЖЕ полной"] = len(SC.core_for("mechanics")) < len(L.CORE)
checks["неизвестная фаза даёт пустую область"] = SC.core_for("нет-такой") == {}

# Имена категорий — те же строки, по которым синтезатор ищет вес. Разъедутся —
# категория молча выпадет из балла вместе со своими находками.
checks["имена категорий области совпадают с весами"] = all(
    name in S.CATEGORY_WEIGHTS
    for phase in SC.known_phases() for name in SC.core_for(phase))

# Обучающие линзы: включаются ответом на вопрос 1, а не жанром.
checks["развлечение → 97/98 без запрета N/A"] = (
    SC.scope_for("story", {"purpose": ["Развлечение"]})["education_lenses"] == ())
checks["обучение → 97/98 с запретом N/A"] = (
    SC.scope_for("story", {"purpose": ["Обучение"]})["education_lenses"] == (97, 98))
checks["развитие навыков тоже обучение"] = (
    SC.scope_for("story", {"purpose": ["Развитие навыков"]})["educational"] is True)
checks["пустые параметры не делают игру обучающей"] = (
    SC.scope_for("story", {})["educational"] is False)


# ============================================================================
# ЧАСТЬ 2. Условие вызова — считает КОД, а не формулировка ответа
# ============================================================================
AUDIT_OK = {"map": [{"item": "genre_match", "status": "ok"},
                    {"item": "catch_up_respected", "status": "concern",
                     "note": "поддержка пассивная"}],
            "issues": [{"checklist_item": "catch_up_respected", "severity": "minor",
                        "explanation": "отстающему нечем догнать"}]}
AUDIT_VIOLATION = {"map": [{"item": "elimination_respected", "status": "violation",
                            "note": "выбывание запрещено автором"}], "issues": []}
AUDIT_CRITICAL = {"map": [{"item": "genre_match", "status": "ok"}],
                  "issues": [{"checklist_item": "genre_match",
                              "severity": "critical", "explanation": "жанр не тот"}]}

checks["чистый аудит пропускает к линзам"] = M.should_run(AUDIT_OK)["ready"] is True
checks["нарушение чек-листа не пропускает"] = (
    M.should_run(AUDIT_VIOLATION)["ready"] is False)
checks["находка critical не пропускает"] = (
    M.should_run(AUDIT_CRITICAL)["ready"] is False)
checks["без аудита линзы не зовутся"] = M.should_run({})["ready"] is False
checks["в причине отказа назван пункт чек-листа"] = (
    "elimination_respected" in M.should_run(AUDIT_VIOLATION)["reason"])
checks["в отказе посчитано заблокированное"] = (
    M.should_run(AUDIT_CRITICAL)["blocking"]["critical"] == 1)
# «passed: true» от модели ничего не решает — смотрим на находки, а не на вердикт.
checks["вердикт модели не перебивает находки"] = M.should_run(
    dict(AUDIT_CRITICAL, passed=True, summary="всё хорошо"))["ready"] is False


# ============================================================================
# ЧАСТЬ 3. Замечания аудитора становятся заметками для линз
# ============================================================================
notes = M.notes_from_audit(AUDIT_OK)
checks["concern превратился в заметку"] = any(
    n["from_test"] == "catch_up_respected" for n in notes)
checks["нестрогая находка тоже заметка"] = len(notes) == 2
checks["заметки помечены источником"] = all(
    n["source"] == "module_auditor" for n in notes)
checks["вердикта по заметкам не требуем"] = all(
    n["question_for_lenses"] is None for n in notes)
checks["ok-пункты в заметки не попали"] = not any(
    n["from_test"] == "genre_match" for n in notes)
# critical сюда не попадает: с ним линзы не запускаются вовсе.
checks["critical в заметки не попал"] = M.notes_from_audit(AUDIT_CRITICAL) == []


# ============================================================================
# ЧАСТЬ 4. Сообщение агенту
# ============================================================================
PARAMS = {"purpose": ["Развлечение"], "age_group": {"min": 12, "max": 18},
          "player_count": {"min": 2, "max": 4}, "interaction": "кооперативное",
          "elimination": False, "catch_up": True, "complexity": "средняя",
          "genre": ["приключение"], "play_time": {"min": 30, "max": 60}}
MODULE = {"title": "Исследование локаций", "game_loop": {"turn_structure": ["ход"]},
          "win_condition": {"description": "собрать 10 предметов"}}

scope_mech = SC.scope_for("mechanics", PARAMS)
data = M.build_input("mechanics", MODULE, PARAMS, AUDIT_OK)
msg = M.build_message(data, scope_mech)

checks["в сообщении сказано, что это модуль"] = "оценивается модуль" in msg
checks["в сообщении названа фаза"] = "mechanics" in msg
checks["в сообщении есть параметры опросника"] = "ПАРАМЕТРЫ ИГРЫ ИЗ ОПРОСНИКА" in msg
checks["в сообщении запрет достраивать игру"] = "не достраивай" in msg.lower()
checks["развлекательной игре разрешён N/A по 97/98"] = "НЕ заявлена как обучающая" in msg
checks["требование про один JSON осталось последним"] = msg.rstrip().endswith(
    "Верни один JSON без текста вокруг.")
checks["в рабочем ядре только линзы области"] = (
    f"{len(scope_mech['lenses'])} линз" in msg)
checks["линз у механик меньше полного ядра"] = (
    len(scope_mech["lenses"]) < len(L.CORE_LENSES))
# Обучающей игре предупреждения быть не должно — там N/A действительно запрещён.
msg_edu = M.build_message(
    M.build_input("story", MODULE, dict(PARAMS, purpose=["Обучение"]), AUDIT_OK),
    SC.scope_for("story", {"purpose": ["Обучение"]}))
checks["обучающей игре разрешения на N/A нет"] = "НЕ заявлена как обучающая" not in msg_edu
checks["в заметках виден concern аудитора"] = "поддержка пассивная" in msg


# ============================================================================
# ЧАСТЬ 5. Валидатор считается с областью
# ============================================================================
def scoped_report(scope, value=8, na_categories=(), low=None):
    """Полный, ВАЛИДНЫЙ отчёт по области — такой, каким его обязан вернуть агент.

    Среднее по категории проставляется здесь же: без него валидатор справедливо
    ругается `avg_missing`, и по такому отчёту нельзя отличить «проверка нашла
    настоящую проблему» от «фикстура собрана небрежно».

    `low` — {категория: балл} для проверки взвешивания.
    """
    low = low or {}
    cats = []
    for name, nums in scope["core"].items():
        if name in na_categories:
            cats.append({"name": name, "na": True,
                         "lenses": [{"n": n, "na": True, "na_reason": "нет материала"}
                                    for n in nums]})
            continue
        mark = low.get(name, value)
        cats.append({"name": name,
                     "category_avg_preliminary": float(mark),
                     "lenses": [{"n": n, "score": mark, "basis": "по тексту"}
                                for n in nums]})
    return {"categories": cats, "findings": [], "playtest_recommended": True,
            "playtest_reason": "субъективные механики проверяются только живой игрой",
            "answers_to_open_questions": []}

report_mech = scoped_report(scope_mech)
issues = L.validate(report_mech, data, scope_mech)
codes = [i["code"] for i in issues]
checks["категорий вне области валидатор не требует"] = "category_missing" not in codes
checks["линз вне области валидатор не требует"] = "lenses_missing" not in codes

# А без области тот же отчёт обязан считаться неполным — иначе проверка ослабла
# бы для самого ФинИгроСкопа, и это осталось бы незамеченным.
codes_full = [i["code"] for i in L.validate(report_mech, data)]
checks["без области тот же отчёт неполон"] = "lenses_missing" in codes_full
checks["без области видно нехватку категорий"] = "category_missing" in codes_full

# Линза вне области — ошибка: агент вышел за рамки модуля.
extra = scoped_report(scope_mech)
extra["categories"][0]["lenses"].append({"n": 87, "score": 9, "basis": "выдумка"})
checks["лишняя линза замечена"] = "lens_out_of_core" in [
    i["code"] for i in L.validate(extra, data, scope_mech)]


# ============================================================================
# ЧАСТЬ 6. Балл модуля: арифметика и честность
# ============================================================================
sc = M.score(report_mech, scope_mech)
checks["балл посчитан"] = sc["overall"] == 8.0
checks["балл с тремя знаками"] = isinstance(sc["overall"], float)
checks["порог тот же, что у синтезатора"] = sc["passing_score"] == S.PASSING_SCORE
checks["8.0 выше порога"] = sc["passed"] is True
checks["покрытый вес меньше единицы"] = 0 < sc["weight_covered"] < 1
checks["в строках все девять категорий"] = len(sc["rows"]) == len(S.CATEGORY_WEIGHTS)
checks["вне области помечено причиной"] = any(
    r["na_reason"] == "вне области модуля" for r in sc["rows"] if not r["in_scope"])
checks["категории вне области не оценены"] = all(
    r["score"] is None for r in sc["rows"] if not r["in_scope"])

# Главное: N/A не ноль. Категория целиком в N/A выпадает из расчёта, а не тянет
# балл вниз, — и покрытый вес это показывает.
na_name = "Социальное взаимодействие"
sc_na = M.score(scoped_report(scope_mech, na_categories=(na_name,)), scope_mech)
checks["N/A не занизил балл"] = sc_na["overall"] == 8.0
checks["N/A уменьшил покрытый вес"] = sc_na["weight_covered"] < sc["weight_covered"]
checks["у N/A-категории названа причина"] = any(
    r["category"] == na_name and r["score"] is None for r in sc_na["rows"])

# Взвешивание работает: одна и та же двойка в тяжёлой категории (вес 1.5) роняет
# балл сильнее, чем в лёгкой (0.75). Это и есть смысл весов.
heavy = M.score(scoped_report(scope_mech, low={"Экономика и баланс": 2}), scope_mech)
light = M.score(scoped_report(scope_mech, low={"Социальное взаимодействие": 2}),
                scope_mech)
checks["двойка в тяжёлой категории роняет сильнее"] = heavy["overall"] < light["overall"]
checks["взвешенное среднее посчитано точно"] = heavy["overall"] == 6.286
checks["балл округлён до трёх знаков"] = (
    len(str(heavy["overall"]).split(".")[1]) <= 3)

# Порог: 6.0 — проходной, ниже — нет.
checks["ровно порог считается пройденным"] = (
    M.score(scoped_report(scope_mech, value=6), scope_mech)["passed"] is True)
checks["ниже порога не проходит"] = (
    M.score(scoped_report(scope_mech, value=5), scope_mech)["passed"] is False)

# Балл полной игры считается по всему ядру — покрытие обязано быть полным.
scope_rules = SC.scope_for("rules", PARAMS)
sc_rules = M.score(scoped_report(scope_rules), scope_rules)
checks["у правил покрыт весь вес"] = sc_rules["weight_covered"] == 1.0
checks["балл правил равен баллу синтезатора"] = sc_rules["overall"] == 8.0

# Ни одной оценённой категории — балла нет, а не ноль.
empty = M.score({"categories": []}, scope_mech)
checks["без оценок балла нет, а не ноль"] = empty["overall"] is None
checks["без оценок нет и вердикта"] = empty["passed"] is None


# ============================================================================
# ЧАСТЬ 7. Полный проход с подставным провайдером
# ============================================================================
class ModuleMock(LLMProvider):
    """Отвечает ровно по области, которую попросили."""

    name = "mock"
    calls = 0
    last_user = ""

    def _complete(self, system, user, **opts):
        ModuleMock.calls += 1
        ModuleMock.last_user = user
        if "Оценщик по линзам" not in system:
            return json.dumps({"ok": True}, ensure_ascii=False)
        return json.dumps(scoped_report(scope_mech), ensure_ascii=False)


register("mock", ModuleMock)

out = M.evaluate("mechanics", MODULE, PARAMS, AUDIT_OK)
checks["проход выполнен"] = out["ready"] and out["available"]
checks["модель позвали один раз"] = ModuleMock.calls == 1
checks["в ответе есть балл"] = out["score"]["overall"] == 8.0
checks["в ответе описана область"] = len(out["scope"]["categories"]) == len(
    scope_mech["core"])
checks["в ответе сказано, обучающая ли игра"] = out["scope"]["educational"] is False
checks["замечаний валидатора нет"] = out["issues"] == []
checks["в ответе назван модуль"] = out["phase"] == "mechanics"

# Модуль с нарушением до модели не доходит вовсе.
ModuleMock.calls = 0
blocked = M.evaluate("mechanics", MODULE, PARAMS, AUDIT_VIOLATION)
checks["заблокированный модуль не идёт к модели"] = ModuleMock.calls == 0
checks["у заблокированного ready=False"] = blocked["ready"] is False

# Неизвестная фаза — отказ, а не молчаливая оценка по пустому ядру.
unknown = M.evaluate("нет-такой", MODULE, PARAMS, AUDIT_OK)
checks["неизвестная фаза отклонена"] = unknown["ready"] is False
checks["в отказе перечислены известные фазы"] = "mechanics" in unknown["reason"]


# ============================================================================
# ЧАСТЬ 8. Очередь
# ============================================================================
lens_queue._reset_for_tests()


# Задача получает Progress собственной задачи: коротким он не нужен, длинным —
# необходим, и контракт у обеих один.
def работа(progress):
    progress.say("считаю", detail="проверочный шаг")
    return {"итог": "готово"}


job_id = lens_queue.submit(работа)
for _ in range(200):
    state = lens_queue.status(job_id)
    if state["status"] in (lens_queue.DONE, lens_queue.FAILED):
        break
    __import__("time").sleep(0.02)
checks["задача очереди выполнилась"] = state["status"] == lens_queue.DONE
checks["результат вернулся"] = state["result"] == {"итог": "готово"}
checks["ход работы виден"] = state["step"] == "считаю"
checks["подробность шага виден"] = state["detail"] == "проверочный шаг"
checks["время работы считается"] = isinstance(state["elapsed"], float)
checks["несуществующая задача даёт None"] = lens_queue.status("нет-такой") is None


# Ниже очередь НАПЕЧАТАЕТ трассировку RuntimeError — так и задумано: полный текст
# ошибки уходит в журнал, а наружу отдаётся только тип. Это не сбой проверки.
def падает(progress):
    raise RuntimeError("секрет-из-промпта")


job_bad = lens_queue.submit(падает)
for _ in range(200):
    state_bad = lens_queue.status(job_bad)
    if state_bad["status"] in (lens_queue.DONE, lens_queue.FAILED):
        break
    __import__("time").sleep(0.02)
checks["упавшая задача помечена failed"] = state_bad["status"] == lens_queue.FAILED
# Текст исключения наружу не отдаём: в нём может оказаться кусок промпта.
checks["текст исключения наружу не ушёл"] = "секрет-из-промпта" not in (
    state_bad["error"] or "")
checks["но тип ошибки назван"] = "RuntimeError" in state_bad["error"]


# --- полосы очереди ---------------------------------------------------------
# Через очередь идут две работы с несопоставимой длительностью: оценка модуля
# (минуты) и итоговый разбор игры (десятки минут). Пока пул был общим и
# двухместным, двух разборов хватало, чтобы КАЖДАЯ оценка встала намертво, а
# генератор сообщал «не уложилась» при исправном сервисе.
checks["полос две"] = set(lens_queue.LANE_WORKERS) == {lens_queue.SHORT,
                                                       lens_queue.LONG}
checks["у короткой полосы больше одного места"] = (
    lens_queue.LANE_WORKERS[lens_queue.SHORT] >= 2)
checks["длинная полоса не занимает места короткой"] = (
    lens_queue.LANE_WORKERS[lens_queue.LONG]
    < lens_queue.LANE_WORKERS[lens_queue.SHORT] + 1)
checks["неизвестная полоса отвергается"] = False
try:
    lens_queue.submit(работа, lane="какая-то")
except ValueError:
    checks["неизвестная полоса отвергается"] = True

# Главное: короткая задача НЕ ждёт длинную. Занимаем всю длинную полосу и
# смотрим, что оценка модуля проходит мимо неё.
import threading as _threading  # noqa: E402

_держим = _threading.Event()
lens_queue._reset_for_tests()


def долгая(progress):
    progress.say("разбор игры")
    _держим.wait(10)
    return {"долгая": True}


def короткая(progress):
    return {"короткая": True}


long_ids = [lens_queue.submit(долгая, lane=lens_queue.LONG)
            for _ in range(lens_queue.LANE_WORKERS[lens_queue.LONG] + 1)]
short_id = lens_queue.submit(короткая, lane=lens_queue.SHORT)

for _ in range(300):
    short_state = lens_queue.status(short_id)
    if short_state["status"] in (lens_queue.DONE, lens_queue.FAILED):
        break
    __import__("time").sleep(0.02)

checks["короткая задача не ждёт длинную"] = short_state["status"] == lens_queue.DONE
checks["длинная в это время ещё идёт"] = any(
    lens_queue.status(j)["status"] in (lens_queue.QUEUED, lens_queue.RUNNING)
    for j in long_ids)
# Задача, стоящая за чужой работой, должна об этом СКАЗАТЬ: «в очереди» без
# числа не отличить от «висит».
queued = [lens_queue.status(j) for j in long_ids]
queued = [s for s in queued if s["status"] == lens_queue.QUEUED]
checks["стоящая в очереди знает, сколько впереди"] = bool(queued) and all(
    s.get("waiting_ahead", 0) >= 1 for s in queued)
checks["полоса видна в состоянии"] = short_state["lane"] == lens_queue.SHORT

_держим.set()
lens_queue._reset_for_tests()


# --- текст ошибки ------------------------------------------------------------
# Часть ошибок пишется ДЛЯ ЧЕЛОВЕКА и помечает себя user_facing. Пока метка не
# читалась, объяснение «Прогон скелета не разрешён… SIM_API_ALLOW_RUN=1»
# подменялось строкой «не выполнена: HeadlessError» — единственная подсказка,
# как это починить, до автора не доезжала.
class _Человеческая(Exception):
    user_facing = True


человеческий_текст = ("Прогон скелета не разрешён. SIM_API_ALLOW_RUN=1 в "
                      "окружении ФинИгроСкопа.")
checks["помеченная ошибка доходит целиком"] = (
    lens_queue.describe_failure(_Человеческая(человеческий_текст))
    == человеческий_текст)
checks["пустая помеченная ошибка не оставляет пусто"] = (
    "_Человеческая" in lens_queue.describe_failure(_Человеческая("")))
checks["непомеченная ошибка прячет текст"] = (
    "секрет" not in lens_queue.describe_failure(RuntimeError("секрет-из-промпта")))
# Про разбор игры нельзя говорить «оценка по линзам»: это разные работы.
checks["упавший разбор назван разбором"] = (
    "разбор" in lens_queue.describe_failure(RuntimeError("x"), lens_queue.LONG).lower())
checks["упавшая оценка названа оценкой"] = (
    "оценка" in lens_queue.describe_failure(RuntimeError("x"), lens_queue.SHORT).lower())


# ============================================================================
# ЧАСТЬ 9. HTTP-эндпоинты
# ============================================================================
import app as A  # noqa: E402

A.app.config["TESTING"] = True
client = A.app.test_client()

# Дев-сервер: LENS_API_TOKEN не задан, и эндпоинт работает по признаку debug.
# Именно в этом режиме идёт разработка, поэтому проверяем сперва его; закрытость
# на боевом проверяется в конце этой же части.
A.app.debug = True

resp = client.post("/api/lenses/module", json={"phase": "mechanics",
                                               "module": MODULE,
                                               "params": PARAMS,
                                               "audit": AUDIT_VIOLATION})
checks["заблокированный модуль: ответ 200"] = resp.status_code == 200
checks["заблокированный модуль: ready=False"] = resp.get_json()["ready"] is False

resp = client.post("/api/lenses/module", json={"module": MODULE})
checks["без phase — 400"] = resp.status_code == 400
resp = client.post("/api/lenses/module", json={"phase": "mechanics"})
checks["без module — 400"] = resp.status_code == 400
resp = client.post("/api/lenses/module", data="не json",
                   content_type="application/json")
checks["не JSON — 400"] = resp.status_code == 400

resp = client.post("/api/lenses/module", json={"phase": "mechanics",
                                               "module": MODULE,
                                               "params": PARAMS,
                                               "audit": AUDIT_OK})
checks["принятая заявка: 202"] = resp.status_code == 202
body = resp.get_json()
checks["в ответе номер задачи"] = bool(body.get("job_id"))
checks["в ответе адрес статуса"] = body["status_url"].endswith(".json")

for _ in range(300):
    state = client.get(body["status_url"]).get_json()
    if state["status"] in ("done", "failed"):
        break
    __import__("time").sleep(0.02)
checks["оценка через HTTP выполнилась"] = state["status"] == "done"
checks["через HTTP пришёл балл"] = state["result"]["score"]["overall"] == 8.0

checks["неизвестная задача — 404"] = client.get(
    "/api/lenses/module/нетакой.json").status_code == 404

# Защита эндпоинта: он тратит деньги, поэтому на боевом закрыт по умолчанию.
A.app.debug = False
resp = client.post("/api/lenses/module", json={"phase": "mechanics",
                                               "module": MODULE,
                                               "audit": AUDIT_OK})
checks["без токена вне разработки — 403"] = resp.status_code == 403
checks["в отказе сказано про LENS_API_TOKEN"] = "LENS_API_TOKEN" in resp.get_json()["error"]

A.LENS_API_TOKEN = "s3cret-token"
resp = client.post("/api/lenses/module",
                   json={"phase": "mechanics", "module": MODULE, "audit": AUDIT_OK},
                   headers={"X-Lens-Token": "wrong"})
checks["неверный токен — 403"] = resp.status_code == 403
resp = client.post("/api/lenses/module",
                   json={"phase": "mechanics", "module": MODULE, "audit": AUDIT_OK})
checks["отсутствующий токен — 403"] = resp.status_code == 403
resp = client.post("/api/lenses/module",
                   json={"phase": "mechanics", "module": MODULE,
                         "params": PARAMS, "audit": AUDIT_OK},
                   headers={"X-Lens-Token": "s3cret-token"})
checks["верный токен пропускает"] = resp.status_code == 202

# Токен с кириллицей: compare_digest на строках вне ASCII бросает TypeError, и
# эндпоинт отвечал бы 500 вместо отказа. Отказ обязан быть отказом при любом
# значении, которое кто-то впишет в .env.
A.LENS_API_TOKEN = "секретный-ключ"
resp = client.post("/api/lenses/module",
                   json={"phase": "mechanics", "module": MODULE, "audit": AUDIT_OK},
                   headers={"X-Lens-Token": "wrong"})
checks["токен вне ASCII не роняет эндпоинт"] = resp.status_code == 403

A.LENS_API_TOKEN = ""
A.app.debug = True


for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
