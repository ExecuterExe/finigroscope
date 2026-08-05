# -*- coding: utf-8 -*-
"""Ворота между этапами: следующий модуль строится только на ПРИНЯТОМ предыдущем.

По таблице 9 ТЗ сюжет генерируется, «если механики прошли аудит». Проверять это
на стороне страницы нельзя: «модуль принят» — вывод из двух платных проверок
(аудитор и линзы), и браузер может прислать что угодно. Поэтому на вход этапа
идёт номер завершённой задачи, а модуль сервер достаёт из неё сам.

Обращений к моделям здесь нет: задачи кладутся в очередь готовыми результатами.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as generator_app  # noqa: E402
import jobs  # noqa: E402


MODULE = {"title": "Совместный поиск", "required_component_types": ["карты"]}


@pytest.fixture(autouse=True)
def clean_jobs():
    jobs._reset_for_tests()
    yield
    jobs._reset_for_tests()


def finished(result):
    """Номер завершённой задачи с заданным результатом."""
    job_id = jobs.submit(lambda progress: result)
    deadline = time.time() + 5
    while time.time() < deadline:
        state = jobs.status(job_id)
        if state and state["status"] in (jobs.DONE, jobs.FAILED):
            return job_id
        time.sleep(0.01)
    raise AssertionError("задача не завершилась")


def passed_result(**over):
    base = {
        "ok": True, "phase": "mechanics", "passed": True, "scored": True,
        "verdict": "Модуль принят.",
        "best": {"attempt": 1, "audit": {"cleaned_module": MODULE}},
    }
    base.update(over)
    return base


def accepted(job_id, force=False):
    return generator_app.Handler.accepted_module(job_id, "mechanics", force=force)


# --------------------------------------------------------------------------
# Пропускает только принятый модуль
# --------------------------------------------------------------------------

def test_принятый_модуль_отдаётся_из_задачи():
    chain, error = accepted(finished(passed_result()))
    assert error is None
    assert chain["module"] == MODULE
    assert chain["accepted"] is True
    assert chain["override"] is False


def test_в_цепочку_идёт_проверенный_модуль_а_не_сырой_вариант():
    """Контракт ModuleChain: сырой вывод генератора аудитор мог поправить."""
    raw = {"title": "черновик", "required_component_types": ["карты", "фишки"]}
    result = passed_result(best={"attempt": 1, "variant": raw,
                                 "audit": {"cleaned_module": MODULE}})
    chain, error = accepted(finished(result))
    assert error is None
    assert chain["module"] == MODULE and chain["module"] != raw


# --------------------------------------------------------------------------
# Отказы — каждый со своим кодом и внятной причиной
# --------------------------------------------------------------------------

def test_без_номера_задачи_отказ():
    chain, error = accepted(None)
    assert chain is None
    body, code = error
    assert code == 400
    assert "не указан" in body["error"].lower()


def test_забытая_задача_объясняет_что_делать():
    """Очередь живёт в памяти: перезапуск сервера стирает её."""
    chain, error = accepted("нет-такой-задачи")
    assert chain is None
    body, code = error
    assert code == 404
    assert "перезапускался" in body["error"]
    assert "заново" in body["error"]


def test_незавершённый_проход_не_пускают_дальше():
    started = {"go": False}

    def slow(progress):
        while not started["go"]:
            time.sleep(0.01)
        return passed_result()

    job_id = jobs.submit(slow)
    time.sleep(0.1)
    chain, error = accepted(job_id)
    started["go"] = True

    assert chain is None
    body, code = error
    assert code == 409
    assert "не завершён" in body["error"]


def test_непринятый_модуль_не_пускают_дальше():
    """По умолчанию — отказ: молча строить сюжет на браке нельзя."""
    result = passed_result(passed=False,
                           verdict="Порог не взят ни одной из 3 попыток.")
    chain, error = accepted(finished(result))

    assert chain is None
    body, code = error
    assert code == 409
    # причина названа словами самого прохода, а не «нельзя»
    assert "Порог не взят" in body["error"]
    # и сказано, что отказ снимается решением автора, а не починкой
    assert body["can_accept_anyway"] is True


def test_задача_чужого_этапа_не_подходит():
    chain, error = accepted(finished(passed_result(phase="story")))
    assert chain is None
    body, code = error
    assert code == 400
    assert "story" in body["error"]


def test_результат_без_проверенного_модуля_это_отказ():
    result = passed_result(best={"attempt": 1, "audit": {}})
    chain, error = accepted(finished(result))

    assert chain is None
    body, code = error
    assert code == 409
    assert "cleaned_module" in body["error"]


def test_упавшая_задача_не_считается_принятой():
    def падает(progress):
        raise ValueError("нарочно")

    job_id = jobs.submit(падает)
    deadline = time.time() + 5
    while time.time() < deadline and jobs.status(job_id)["status"] != jobs.FAILED:
        time.sleep(0.01)

    chain, error = accepted(job_id)
    assert chain is None
    assert error[1] == 409


# --------------------------------------------------------------------------
# «Меня устраивает, идём дальше» — решение автора
# --------------------------------------------------------------------------

def test_автор_вправе_продолжить_на_непринятом_модуле():
    """ТЗ, этапы 2-6 пункт 3: если замечания — совет, можно идти дальше."""
    result = passed_result(passed=False, threshold=6.0,
                           verdict="Порог не взят ни одной из 3 попыток.",
                           best={"attempt": 2, "score": 4.7,
                                 "audit": {"cleaned_module": MODULE}})
    chain, error = accepted(finished(result), force=True)

    assert error is None
    assert chain["module"] == MODULE
    # непринятость едет дальше и обязана быть видна в итоге
    assert chain["accepted"] is False
    assert chain["override"] is True
    assert chain["score"] == 4.7
    assert chain["threshold"] == 6.0


def test_согласие_автора_ничего_не_прощает_кроме_балла():
    """Пропавшая задача и чужая фаза — не решения автора, а отсутствие данных."""
    assert accepted("нет-такой-задачи", force=True)[0] is None
    assert accepted(finished(passed_result(phase="story")), force=True)[0] is None
    assert accepted(finished(passed_result(best={"attempt": 1, "audit": {}})),
                    force=True)[0] is None


def test_у_принятого_модуля_согласие_ничего_не_меняет():
    chain, error = accepted(finished(passed_result()), force=True)
    assert error is None
    assert chain["accepted"] is True
    assert chain["override"] is False


# --------------------------------------------------------------------------
# Цепочка из нескольких оснований (этап 4 строится на двух)
# --------------------------------------------------------------------------

STORY_MODULE = {"title": "Ключи старой библиотеки"}

WANTED = [("mechanics_job_id", "mechanics"), ("story_job_id", "story")]


def story_result(**over):
    base = {
        "ok": True, "phase": "story", "passed": True, "scored": True,
        "verdict": "Модуль принят.",
        "best": {"attempt": 1, "audit": {"cleaned_module": STORY_MODULE}},
    }
    base.update(over)
    return base


def both(mechanics=None, story=None, **payload):
    request = {"mechanics_job_id": finished(mechanics or passed_result()),
               "story_job_id": finished(story or story_result())}
    request.update(payload)
    return generator_app.Handler.accepted_chain(request, WANTED)


def test_оба_основания_отдаются_в_порядке_этапов():
    """Аудитор читает список принятых модулей как последовательность."""
    chain, error = both()
    assert error is None
    assert [link["phase"] for link in chain] == ["mechanics", "story"]
    assert chain[0]["module"] == MODULE
    assert chain[1]["module"] == STORY_MODULE


def test_непринятое_второе_основание_останавливает_цепочку():
    chain, error = both(story=story_result(passed=False,
                                           verdict="Порог не взят."))
    assert chain is None
    body, code = error
    assert code == 409
    assert "story" in body["error"]
    assert body["can_accept_anyway"] is True


def test_согласие_распространяется_на_оба_основания():
    chain, error = both(
        mechanics=passed_result(passed=False, threshold=6.0,
                                verdict="Порог не взят.",
                                best={"score": 4.1,
                                      "audit": {"cleaned_module": MODULE}}),
        story=story_result(passed=False, threshold=6.0,
                           verdict="Порог не взят.",
                           best={"score": 5.2,
                                 "audit": {"cleaned_module": STORY_MODULE}}),
        accept_anyway=True)

    assert error is None
    assert [link["override"] for link in chain] == [True, True]
    assert [link["score"] for link in chain] == [4.1, 5.2]


def test_отсутствие_второго_номера_это_отказ():
    chain, error = generator_app.Handler.accepted_chain(
        {"mechanics_job_id": finished(passed_result())}, WANTED)
    assert chain is None
    assert error[1] == 400


def test_в_отчёт_модуль_не_дублируется():
    """Модуль и так лежит в результате прохода — третий экземпляр не нужен."""
    chain, _ = both()
    note = generator_app.Handler.chain_note(chain[0])
    assert "module" not in note
    assert note["phase"] == "mechanics" and note["accepted"] is True
