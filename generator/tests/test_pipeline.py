# -*- coding: utf-8 -*-
"""Оркестратор модуля: до трёх попыток и отбор лучшей.

Обращений к моделям нет: генератор механик, аудитор и клиент линз подменяются
заглушками, которые отдают заготовленную последовательность исходов.

Проверяется то, за что отвечает оркестратор, а не агенты: сколько попыток он
делает, когда останавливается досрочно, какую попытку объявляет лучшей и что
сообщает, если порог не взят ни разу. Это и есть его единственная работа —
качество самих модулей определяют другие.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import pipeline  # noqa: E402


PARAMS = {"purpose": ["Развлечение"], "player_count": {"min": 2, "max": 4}}


class Progress(object):
    """Заглушка отчёта о ходе работы: запоминает, о чём ей рассказали.

    Оркестратор не знает про очередь: и сообщения о ходе, и отмену он получает
    через этот объект. Поэтому подменить его хватает — сама отмена проверяется
    там, где живёт, в tests/test_jobs.py.
    """

    def __init__(self):
        self.steps = []
        self.attempts = []

    def say(self, step, detail=None, attempt=None, attempts_total=None):
        self.steps.append({"step": step, "attempt": attempt})

    def add_attempt(self, row):
        self.attempts.append(row)

    def check_cancelled(self):
        return None


def variant(number, title="вариант"):
    return {"variant_id": number, "title": "%s %d" % (title, number),
            "game_loop": {"turn_structure": ["ход"]}}


def generation(number):
    return {"ok": True, "data": {"variants": [variant(number)],
                                 "recommended_variant_id": number}}


def audit(clean=True):
    if clean:
        return {"map": [{"item": "genre_match", "status": "ok"}], "issues": [],
                "passed": True}
    return {"map": [{"item": "elimination_respected", "status": "violation",
                     "note": "выбывание запрещено"}], "issues": [], "passed": False}


def lens(score, passed=None):
    return {"ready": True, "available": True,
            "score": {"overall": score,
                      "passed": passed if passed is not None else score >= 6.0,
                      "passing_score": 6.0, "weight_covered": 0.525}}


class Fakes(object):
    """Подменяет агентов разом и считает вызовы.

    Заодно запоминает, с какой фазой и с какими предыдущими модулями позвали
    аудитора: для второго и следующих проходов это не деталь, а суть — сверка с
    принятыми модулями и есть то, ради чего цепочка выстроена.
    """

    def __init__(self, monkeypatch, generations, audits, lenses):
        self.generations = list(generations)
        self.audits = list(audits)
        self.lenses = list(lenses)
        self.calls = {"generate": 0, "audit": 0, "lens": 0}
        self.audit_calls = []
        self.lens_calls = []

        def fake_generate(params, *args, **kwargs):
            self.calls["generate"] += 1
            return self._next(self.generations)

        def fake_audit(phase, module, params, **kwargs):
            self.calls["audit"] += 1
            self.audit_calls.append({"phase": phase,
                                     "previous": kwargs.get("previous_modules")})
            outcome = self._next(self.audits)
            return type("R", (), {"to_dict": lambda _self, o=outcome: o})()

        def fake_lens(phase, module, params, audit_dict, **kwargs):
            self.calls["lens"] += 1
            self.lens_calls.append(phase)
            return self._next(self.lenses)

        monkeypatch.setattr(pipeline.mechanics, "generate", fake_generate)
        monkeypatch.setattr(pipeline.story, "generate", fake_generate)
        monkeypatch.setattr(pipeline.features, "generate", fake_generate)
        monkeypatch.setattr(pipeline.rules, "generate", fake_generate)
        monkeypatch.setattr(pipeline.module_auditor, "audit_module", fake_audit)
        monkeypatch.setattr(pipeline.lens_review, "evaluate", fake_lens)

    @staticmethod
    def _next(queue):
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --------------------------------------------------------------------------
# Досрочная остановка
# --------------------------------------------------------------------------

def test_балл_выше_порога_останавливает_проход(monkeypatch):
    """Платить за две лишние попытки, когда результат уже годный, незачем."""
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.5)])
    progress = Progress()

    out = pipeline.run(PARAMS, progress)

    assert out["passed"] is True
    assert out["attempts_made"] == 1
    assert fakes.calls == {"generate": 1, "audit": 1, "lens": 1}
    assert out["best"]["score"] == 7.5


def test_ровно_порог_считается_принятым(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(6.0)])
    out = pipeline.run(PARAMS, Progress())
    assert out["passed"] is True
    assert out["attempts_made"] == 1


# --------------------------------------------------------------------------
# Отбор лучшей попытки
# --------------------------------------------------------------------------

def test_три_попытки_ниже_порога_дают_лучшую(monkeypatch):
    fakes = Fakes(monkeypatch,
                  [generation(1), generation(2), generation(3)],
                  [audit()],
                  [lens(4.2), lens(5.9), lens(3.1)])
    out = pipeline.run(PARAMS, Progress())

    assert out["passed"] is False
    assert out["attempts_made"] == 3
    assert fakes.calls["lens"] == 3
    assert out["best"]["score"] == 5.9
    assert out["best"]["attempt"] == 2
    # итог обязан прямо говорить, что порог не взят: разница между «прошло» и
    # «взяли лучшее из непрошедших» принципиальна
    assert "Порог не взят" in out["verdict"]
    assert "5.9" in out["verdict"]


def test_при_равных_баллах_берётся_ранняя_попытка(monkeypatch):
    Fakes(monkeypatch,
          [generation(1), generation(2), generation(3)],
          [audit()],
          [lens(4.5), lens(4.5), lens(4.5)])
    out = pipeline.run(PARAMS, Progress())
    assert out["best"]["attempt"] == 1


def test_последняя_попытка_может_оказаться_лучшей(monkeypatch):
    Fakes(monkeypatch,
          [generation(1), generation(2), generation(3)],
          [audit()],
          [lens(2.0), lens(3.0), lens(5.5)])
    out = pipeline.run(PARAMS, Progress())
    assert out["best"]["attempt"] == 3
    assert out["best"]["score"] == 5.5


# --------------------------------------------------------------------------
# Сорванные попытки
# --------------------------------------------------------------------------

def test_провал_аудита_не_доходит_до_линз(monkeypatch):
    """Оценивать модуль, который всё равно уйдёт на перегенерацию, — трата денег."""
    fakes = Fakes(monkeypatch,
                  [generation(1), generation(2)],
                  [audit(clean=False), audit(clean=True)],
                  [lens(7.0)])
    out = pipeline.run(PARAMS, Progress())

    assert fakes.calls["lens"] == 1, "линзы позвали по забракованному модулю"
    assert out["attempts_made"] == 2
    assert out["attempts"][0]["ok"] is False
    assert out["attempts"][0]["stage"] == "аудит"
    assert out["passed"] is True


def test_сорванные_попытки_видны_в_отчёте(monkeypatch):
    """«Сделано три попытки, показываем одну» выглядит как потеря."""
    Fakes(monkeypatch,
          [generation(1), generation(2), generation(3)],
          [audit(clean=False), audit(clean=False), audit(clean=True)],
          [lens(4.0)])
    out = pipeline.run(PARAMS, Progress())

    assert len(out["attempts"]) == 3
    assert [a["ok"] for a in out["attempts"]] == [False, False, True]
    assert "elimination_respected" in out["attempts"][0]["reason"]


def test_ни_одной_оценки_это_ошибка_с_причинами(monkeypatch):
    Fakes(monkeypatch,
          [generation(1)], [audit(clean=False)], [lens(9.0)])
    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run(PARAMS, Progress())
    text = str(error.value)
    assert "не дошла до оценки" in text
    # причина каждой попытки названа: иначе чинить нечего
    assert "elimination_respected" in text


def test_нехватка_библиотеки_не_повторяется(monkeypatch):
    """Следующая попытка упрётся в то же самое, только за деньги."""
    error = pipeline.mechanics.NotEnoughMechanics("не хватает механик", [])
    fakes = Fakes(monkeypatch, [error], [audit()], [lens(7.0)])

    with pytest.raises(pipeline.PipelineError) as raised:
        pipeline.run(PARAMS, Progress())

    assert fakes.calls["generate"] == 1
    assert "Библиотека механик" in str(raised.value)


# --------------------------------------------------------------------------
# Согласие с оценщиком
# --------------------------------------------------------------------------

def test_разошедшийся_порог_попадает_в_предупреждения(monkeypatch):
    """Молчаливое расхождение хуже любого из двух значений."""
    strange = lens(5.5)
    strange["score"]["passing_score"] = 7.0
    Fakes(monkeypatch, [generation(1)], [audit()], [strange])

    out = pipeline.run(PARAMS, Progress())
    assert out["warnings"], "расхождение порога прошло молча"
    assert "разошёлся" in out["warnings"][0]


def test_совпадающий_порог_молчит(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])
    out = pipeline.run(PARAMS, Progress())
    assert out["warnings"] == []


# --------------------------------------------------------------------------
# Ход работы
# --------------------------------------------------------------------------

def test_о_каждом_шаге_сообщается(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])
    progress = Progress()
    pipeline.run(PARAMS, progress)

    steps = [s["step"] for s in progress.steps]
    assert steps == ["генерация механик", "аудит модуля", "оценка по линзам"]
    assert progress.steps[0]["attempt"] == 1


# --------------------------------------------------------------------------
# Проход сюжета — второй в цепочке
# --------------------------------------------------------------------------

MECHANICS_MODULE = {"title": "Совместный поиск",
                    "required_component_types": ["карты", "жетоны"]}
STORY_MODULE = {"title": "Ключи старой библиотеки",
                "artifacts": [{"component": "карты", "name": "Комнаты"}]}

STORY_PARAMS = dict(PARAMS, story="полноценный сюжет", world=["Фэнтези"])


def base(phase, accepted=True, score=7.0):
    """Звено цепочки оснований — то, что кладёт в проход сервер."""
    return {"phase": phase, "accepted": accepted, "override": not accepted,
            "score": score, "threshold": 6.0}


def test_сюжет_сверяется_с_принятыми_механиками(monkeypatch):
    """Ради этого этап и стоит вторым: сюжет обязан уважать готовый цикл."""
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])

    out = pipeline.run_story(STORY_PARAMS, Progress(), MECHANICS_MODULE)

    assert out["passed"] is True
    assert out["phase"] == "story"
    assert fakes.audit_calls[0]["phase"] == "story"
    assert fakes.audit_calls[0]["previous"] == [
        {"phase": "mechanics", "module": MECHANICS_MODULE}]
    assert fakes.lens_calls == ["story"]


def test_шаг_сюжета_называется_своим_именем(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])
    progress = Progress()
    pipeline.run_story(STORY_PARAMS, progress, MECHANICS_MODULE)

    steps = [s["step"] for s in progress.steps]
    assert steps == ["генерация сюжета", "аудит модуля", "оценка по линзам"]


def test_абстрактную_игру_линзы_не_оценивают(monkeypatch):
    """Наказывать модуль за отсутствие сюжета, от которого отказались, нельзя."""
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(9.9)])
    params = dict(STORY_PARAMS, story="нет")

    out = pipeline.run_story(params, Progress(), MECHANICS_MODULE)

    assert fakes.calls["lens"] == 0, "линзы позвали у игры без сюжета"
    assert out["scored"] is False
    assert out["passed"] is True
    assert out["best"]["score"] is None
    assert "абстрактная" in out["verdict"]


def test_абстрактная_игра_всё_равно_проходит_аудит(monkeypatch):
    """Название и имена артефактов проверять есть кому и есть зачем."""
    fakes = Fakes(monkeypatch, [generation(1), generation(2)],
                  [audit(clean=False), audit(clean=True)], [lens(9.9)])
    params = dict(STORY_PARAMS, story="нет")

    out = pipeline.run_story(params, Progress(), MECHANICS_MODULE)

    assert fakes.calls["audit"] == 2
    assert out["attempts_made"] == 2
    assert out["attempts"][0]["ok"] is False
    assert out["scored"] is False


def test_абстрактная_игра_без_чистого_аудита_это_ошибка(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit(clean=False)], [lens(9.9)])
    params = dict(STORY_PARAMS, story="нет")

    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run_story(params, Progress(), MECHANICS_MODULE)
    assert "не прошла аудит" in str(error.value)


def test_сюжет_на_непринятых_механиках_не_молчит_об_этом(monkeypatch):
    """Сюжет может взять свои 8 из 10, стоя на механиках с баллом 4.

    По готовой карточке этого не видно никак — значит, сказать обязан итог.
    """
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(8.0)])
    built_on = [base("mechanics", accepted=False, score=4.7)]

    out = pipeline.run_story(STORY_PARAMS, Progress(), MECHANICS_MODULE,
                             built_on=built_on)

    assert out["passed"] is True
    assert out["built_on"] == built_on
    assert out["warnings"], "проход промолчал о непринятом основании"
    first = out["warnings"][0]
    assert "НЕПРИНЯТОМ" in first
    assert "4.7" in first and "6.0" in first


def test_у_принятого_основания_предупреждения_нет(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(8.0)])
    built_on = [base("mechanics", accepted=True, score=7.1)]

    out = pipeline.run_story(STORY_PARAMS, Progress(), MECHANICS_MODULE,
                             built_on=built_on)
    assert out["warnings"] == []
    assert out["built_on"][0]["accepted"] is True


def test_абстрактная_игра_тоже_сообщает_об_основании(monkeypatch):
    """У прохода без балла предупреждение так же обязательно."""
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(8.0)])
    params = dict(STORY_PARAMS, story="нет")
    built_on = [base("mechanics", accepted=False, score=3.0)]

    out = pipeline.run_story(params, Progress(), MECHANICS_MODULE,
                             built_on=built_on)
    assert out["scored"] is False
    assert out["built_on"] == built_on
    assert any("НЕПРИНЯТОМ" in w for w in out["warnings"])


def test_нехватка_библиотеки_сюжетов_не_повторяется(monkeypatch):
    error = pipeline.story.NotEnoughSeeds("не хватает завязок", [])
    fakes = Fakes(monkeypatch, [error], [audit()], [lens(7.0)])

    with pytest.raises(pipeline.PipelineError) as raised:
        pipeline.run_story(STORY_PARAMS, Progress(), MECHANICS_MODULE)

    assert fakes.calls["generate"] == 1
    assert "Библиотека сюжетов" in str(raised.value)


# --------------------------------------------------------------------------
# Проход особенностей — третий в цепочке
# --------------------------------------------------------------------------

def test_особенности_сверяются_с_обоими_принятыми_модулями(monkeypatch):
    """Порядок значим: аудитор читает список как последовательность этапов."""
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.2)])

    out = pipeline.run_features(PARAMS, Progress(), MECHANICS_MODULE, STORY_MODULE)

    assert out["passed"] is True
    assert out["phase"] == "features"
    assert fakes.audit_calls[0]["phase"] == "features"
    assert fakes.audit_calls[0]["previous"] == [
        {"phase": "mechanics", "module": MECHANICS_MODULE},
        {"phase": "story", "module": STORY_MODULE}]
    assert fakes.lens_calls == ["features"]


def test_шаг_особенностей_называется_своим_именем(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.2)])
    progress = Progress()
    pipeline.run_features(PARAMS, Progress(), MECHANICS_MODULE, STORY_MODULE)
    pipeline.run_features(PARAMS, progress, MECHANICS_MODULE, STORY_MODULE)

    steps = [s["step"] for s in progress.steps]
    assert steps == ["генерация особенностей", "аудит модуля", "оценка по линзам"]


def test_особенности_оцениваются_всегда(monkeypatch):
    """Особенности есть при любых ответах — прохода без балла тут не бывает."""
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(6.5)])
    params = dict(PARAMS, story="нет", catch_up=False, elimination=True)

    out = pipeline.run_features(params, Progress(), MECHANICS_MODULE, STORY_MODULE)

    assert out["scored"] is True
    assert fakes.calls["lens"] == 1


def test_оба_непринятых_основания_названы(monkeypatch):
    """Сказать про одно, умолчав о втором, хуже, чем промолчать про оба."""
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(8.0)])
    built_on = [base("mechanics", accepted=False, score=4.1),
                base("story", accepted=False, score=5.2)]

    out = pipeline.run_features(PARAMS, Progress(), MECHANICS_MODULE,
                                STORY_MODULE, built_on=built_on)

    assert len(out["warnings"]) == 2
    assert "mechanics" in out["warnings"][0] and "4.1" in out["warnings"][0]
    assert "story" in out["warnings"][1] and "5.2" in out["warnings"][1]


def test_одно_непринятое_основание_из_двух(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(8.0)])
    built_on = [base("mechanics", accepted=True, score=7.7),
                base("story", accepted=False, score=5.2)]

    out = pipeline.run_features(PARAMS, Progress(), MECHANICS_MODULE,
                                STORY_MODULE, built_on=built_on)

    assert len(out["warnings"]) == 1
    assert "story" in out["warnings"][0]


def test_нехватка_библиотеки_особенностей_не_повторяется(monkeypatch):
    error = pipeline.features.NotEnoughFeatures("не хватает приёмов", [])
    fakes = Fakes(monkeypatch, [error], [audit()], [lens(7.0)])

    with pytest.raises(pipeline.PipelineError) as raised:
        pipeline.run_features(PARAMS, Progress(), MECHANICS_MODULE, STORY_MODULE)

    assert fakes.calls["generate"] == 1
    assert "Библиотека особенностей" in str(raised.value)


# --------------------------------------------------------------------------
# Проход правил — четвёртый и последний
# --------------------------------------------------------------------------

FEATURES_MODULE = {"concept": "Кооперативный поиск для младших школьников."}
ALL_MODULES = {"mechanics": MECHANICS_MODULE, "story": STORY_MODULE,
               "features": FEATURES_MODULE}
COMPONENTS = [{"component": "карты", "quantity": 35}]


def test_правила_сверяются_со_всеми_тремя_модулями(monkeypatch):
    fakes = Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.8)])

    out = pipeline.run_rules(PARAMS, Progress(), ALL_MODULES, COMPONENTS)

    assert out["passed"] is True
    assert out["phase"] == "rules"
    assert fakes.audit_calls[0]["phase"] == "rules"
    assert [p["phase"] for p in fakes.audit_calls[0]["previous"]] == [
        "mechanics", "story", "features"]
    assert fakes.lens_calls == ["rules"]


def test_шаг_правил_называется_своим_именем(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.8)])
    progress = Progress()
    pipeline.run_rules(PARAMS, progress, ALL_MODULES, COMPONENTS)

    steps = [s["step"] for s in progress.steps]
    assert steps == ["сборка правил", "аудит модуля", "оценка по линзам"]


def test_компоненты_не_становятся_основанием(monkeypatch):
    """У расчёта по таблицам нет балла, который можно было бы не взять."""
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.8)])
    built_on = [base("mechanics"), base("story"), base("features")]

    out = pipeline.run_rules(PARAMS, Progress(), ALL_MODULES, COMPONENTS,
                             built_on=built_on)

    assert [b["phase"] for b in out["built_on"]] == ["mechanics", "story", "features"]
    assert out["warnings"] == []


def test_три_непринятых_основания_названы_все(monkeypatch):
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.8)])
    built_on = [base("mechanics", accepted=False, score=4.1),
                base("story", accepted=False, score=5.2),
                base("features", accepted=False, score=5.9)]

    out = pipeline.run_rules(PARAMS, Progress(), ALL_MODULES, COMPONENTS,
                             built_on=built_on)
    assert len(out["warnings"]) == 3


def test_нехватка_модулей_не_повторяется(monkeypatch):
    """Следующая попытка упрётся в то же самое, только за деньги."""
    error = pipeline.rules.RulesError("не хватает принятых модулей: features")
    fakes = Fakes(monkeypatch, [error], [audit()], [lens(7.0)])

    with pytest.raises(pipeline.PipelineError) as raised:
        pipeline.run_rules(PARAMS, Progress(), ALL_MODULES, COMPONENTS)

    assert fakes.calls["generate"] == 1
    assert "Правила собрать не удалось" in str(raised.value)


# --------------------------------------------------------------------------
# Итоговый разбор: о чём проход обязан сказать вслух
# --------------------------------------------------------------------------

def verdict_result(**over):
    base = {"ok": True, "passed": True, "score": 7.4, "threshold": 6.0,
            "rounds_made": 1, "rounds_allowed": 3, "code_execution": True,
            "verdict": "Игра принята.",
            "rounds": [{"attempt": 1, "score": 7.4, "extra_runs_requested": 0,
                        "extra_runs_made": False, "extra_runs_skipped": False}],
            "best": {"attempt": 1, "score": 7.4}}
    base.update(over)
    return base


def run_verdict(monkeypatch, result, built_on=None):
    monkeypatch.setattr(pipeline.verdict, "evaluate",
                        lambda spec, progress=None, wait=None: result)
    return pipeline.run_verdict(PARAMS, Progress(), {"game_spec": {"core": {}}},
                                built_on=built_on)


def test_незаказанные_прогоны_не_повод_предупреждать(monkeypatch):
    """Диагносту хватило основного прогона — это норма, а не находка."""
    out = run_verdict(monkeypatch, verdict_result())
    assert out["warnings"] == [], out["warnings"]


def test_заказанные_но_невыполненные_прогоны_названы(monkeypatch):
    result = verdict_result(rounds=[{"attempt": 1, "score": 7.4,
                                     "extra_runs_requested": 4,
                                     "extra_runs_made": False,
                                     "extra_runs_skipped": True}])
    out = run_verdict(monkeypatch, result)
    assert any("заказано 4" in w for w in out["warnings"])


def test_выключенный_прогон_кода_назван(monkeypatch):
    out = run_verdict(monkeypatch, verdict_result(code_execution=False))
    assert any("SIM_API_ALLOW_RUN" in w for w in out["warnings"])


def test_оборвавшийся_круг_назван(monkeypatch):
    result = verdict_result(passed=False, score=4.2,
                            broke_on={"round": 2, "reason": "ядро не сходится"})
    out = run_verdict(monkeypatch, result)
    assert any("Круг 2 оборвался" in w for w in out["warnings"])


def test_круги_соседа_показаны_попытками(monkeypatch):
    result = verdict_result(
        rounds_made=2,
        rounds=[{"attempt": 1, "score": 4.2, "redesign": {"changes": [1]}},
                {"attempt": 2, "score": 7.4}])
    out = run_verdict(monkeypatch, result)
    assert [a["score"] for a in out["attempts"]] == [4.2, 7.4]
    assert out["attempts"][0]["title"] == "правка применена"
    assert out["attempts"][0]["passed"] is False
    assert out["attempts"][1]["passed"] is True


def test_отказ_клиента_доходит_словами(monkeypatch):
    def broken(spec, progress=None, wait=None):
        raise pipeline.verdict.VerdictError("ФинИгроСкоп недоступен")

    monkeypatch.setattr(pipeline.verdict, "evaluate", broken)
    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run_verdict(PARAMS, Progress(), {"game_spec": {"core": {}}})
    assert "недоступен" in str(error.value)


def test_проход_механик_помечен_своей_фазой(monkeypatch):
    """По результату должно быть видно, какого этапа он: цепочку строит сервер."""
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])
    out = pipeline.run(PARAMS, Progress())
    assert out["phase"] == "mechanics"
    assert out["scored"] is True


def test_итоги_попыток_видны_до_конца_прохода(monkeypatch):
    """Иначе при трёх попытках пять минут не на что смотреть."""
    Fakes(monkeypatch,
          [generation(1), generation(2)], [audit()], [lens(4.0), lens(7.0)])
    progress = Progress()
    pipeline.run(PARAMS, progress)

    assert len(progress.attempts) == 2
    assert progress.attempts[0]["score"] == 4.0
    # в ход работы уходит краткая строка, без тяжёлых вложенных отчётов
    assert "lens" not in progress.attempts[0]
    assert "variant" not in progress.attempts[0]


# --------------------------------------------------------------------------
# Почему попытка не прошла: причины обязаны доезжать до экрана
# --------------------------------------------------------------------------

def test_причины_провала_генерации_не_теряются(monkeypatch):
    """Валидатор их уже вычислил. Выбросить их перед самым показом значит
    оставить автора со строкой «модель не собрала годный вариант» — по ней
    нельзя понять ни что чинить, ни виноват ли он сам."""
    плохо = {"ok": False, "problems": ["вариант 1: приём FEAT_X не из библиотеки",
                                       "вариант 2: пустая концепция"]}
    Fakes(monkeypatch, [плохо], [audit()], [lens(7.0)])

    with pytest.raises(pipeline.PipelineError) as error:
        pipeline.run(PARAMS, Progress())

    текст = str(error.value)
    assert "не из библиотеки" in текст
    assert "пустая концепция" in текст


def test_разбор_попытки_уходит_на_экран(monkeypatch):
    """Строка попытки должна нести и причины, и находки — по ним открывается
    разбор, а не только слово «сорвалась»."""
    плохо = {"ok": False, "problems": ["вариант 1: нет условия победы"]}
    Fakes(monkeypatch, [плохо, generation(2)], [audit()], [lens(7.0)])

    out = pipeline.run(PARAMS, Progress())
    первая = out["attempts"][0]

    assert первая["problems"] == ["вариант 1: нет условия победы"]


def test_балл_непрошедшей_попытки_виден(monkeypatch):
    """Прочерк там, где балл посчитан, скрывает главное — насколько не дотянули."""
    Fakes(monkeypatch,
          [generation(1), generation(2)], [audit()], [lens(4.2), lens(7.0)])

    out = pipeline.run(PARAMS, Progress())
    непрошедшая = out["attempts"][0]

    assert непрошедшая["score"] == 4.2
    assert непрошедшая["passed"] is False


def test_находки_линз_видны_в_строке_попытки(monkeypatch):
    """«Почему такой балл» — это находки, а не само число."""
    низкий = lens(4.0)
    низкий["report"] = {"findings": [
        {"lens": 34, "severity": "major", "detail": "нет случайности"}]}
    Fakes(monkeypatch, [generation(1)], [audit()], [низкий])

    out = pipeline.run(PARAMS, Progress())
    строка = out["attempts"][0]

    assert строка["lens_findings"][0]["lens"] == 34
    assert "нет случайности" in строка["lens_findings"][0]["detail"]


def test_нарушения_аудитора_видны_в_строке_попытки(monkeypatch):
    Fakes(monkeypatch,
          [generation(1), generation(2)],
          [audit(clean=False), audit(clean=True)],
          [lens(7.0)])

    out = pipeline.run(PARAMS, Progress())
    сорвалась = out["attempts"][0]

    assert сорвалась["audit_violations"]
    assert сорвалась["audit_violations"][0]["item"] == "elimination_respected"


def test_тяжёлые_отчёты_на_экран_не_едут(monkeypatch):
    """Разбор аудита и линз целиком остаётся в полной строке: в браузер он
    уехал бы третьим экземпляром одного и того же."""
    Fakes(monkeypatch, [generation(1)], [audit()], [lens(7.0)])

    out = pipeline.run(PARAMS, Progress())
    строка = out["attempts"][0]

    assert "variant" not in строка
    assert "audit" not in строка
    assert "lens" not in строка
