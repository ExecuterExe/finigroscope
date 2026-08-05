# -*- coding: utf-8 -*-
"""Очередь долгих проходов: отчёт о ходе работы, отмена, изоляция ошибок."""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jobs  # noqa: E402


def wait(job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = jobs.status(job_id)
        if state and state["status"] in (jobs.DONE, jobs.FAILED):
            return state
        time.sleep(0.01)
    raise AssertionError("задача не завершилась за %.1f с" % timeout)


@pytest.fixture(autouse=True)
def чистая_очередь():
    jobs._reset_for_tests()
    yield
    jobs._reset_for_tests()


def test_результат_возвращается():
    job_id = jobs.submit(lambda progress: {"итог": "готово"})
    assert wait(job_id)["result"] == {"итог": "готово"}


def test_ход_работы_виден_снаружи():
    """Ради этого очередь и заведена: «идёт» про трёхшаговый проход бесполезно."""
    started = threading.Event()
    release = threading.Event()

    def работа(progress):
        progress.say("аудит модуля", detail="сверка с опросником",
                     attempt=2, attempts_total=3)
        started.set()
        release.wait(5)
        return {"ок": True}

    job_id = jobs.submit(работа)
    assert started.wait(5)

    state = jobs.status(job_id)
    assert state["status"] == jobs.RUNNING
    assert state["step"] == "аудит модуля"
    assert state["detail"] == "сверка с опросником"
    assert state["attempt"] == 2
    assert state["attempts_total"] == 3

    release.set()
    wait(job_id)


def test_итоги_попыток_копятся_по_ходу():
    started = threading.Event()
    release = threading.Event()

    def работа(progress):
        progress.add_attempt({"attempt": 1, "score": 4.2})
        started.set()
        release.wait(5)
        return {}

    job_id = jobs.submit(работа)
    assert started.wait(5)
    assert jobs.status(job_id)["attempts"] == [{"attempt": 1, "score": 4.2}]
    release.set()
    wait(job_id)


def test_отмена_прерывает_между_шагами():
    """Внутри вызова модели прервать нельзя: запрос уже отправлен и оплачен."""
    started = threading.Event()

    def работа(progress):
        started.set()
        for _ in range(500):
            progress.check_cancelled()
            time.sleep(0.01)
        return {"дошло": "до конца"}

    job_id = jobs.submit(работа)
    assert started.wait(5)
    assert jobs.request_cancel(job_id) is True

    state = wait(job_id)
    assert state["status"] == jobs.FAILED
    assert "остановлен" in state["error"]


def test_завершённую_задачу_отменить_нельзя():
    job_id = jobs.submit(lambda progress: {"готово": True})
    wait(job_id)
    assert jobs.request_cancel(job_id) is False


class ЧеловеческаяОшибка(Exception):
    """Как PipelineError и LensError: текст написан для показа."""

    user_facing = True


def test_помеченная_ошибка_доходит_до_экрана_целиком():
    """Причина вычислена и записана в исключение — выбрасывать её перед самым
    показом значит оставить пользователя вообще без объяснения."""
    текст = ("Ни одна из 3 попыток не дошла до оценки. Причины: попытка 1 — "
             "критичные замечания: elimination_respected (аудит)")

    def работа(progress):
        raise ЧеловеческаяОшибка(текст)

    assert wait(jobs.submit(работа))["error"] == текст


def test_пустой_текст_не_оставляет_пользователя_ни_с_чем():
    def работа(progress):
        raise ЧеловеческаяОшибка("")

    error = wait(jobs.submit(работа))["error"]
    assert "ЧеловеческаяОшибка" in error
    assert error.strip() != ""


def test_непомеченный_текст_наружу_не_идёт_даже_короткий():
    """Основание показать текст — только метка, а не его вид.

    `RuntimeError("секрет-из-промпта")` короток и однострочен, но показывать
    его нельзя: решает автор сообщения, а не длина строки.
    """
    def работа(progress):
        raise ValueError("ключ sk-or-v1-abcdef")

    error = wait(jobs.submit(работа))["error"]
    assert "sk-or-v1" not in error
    assert "ValueError" in error


def test_упавшая_задача_не_выносит_текст_исключения():
    def работа(progress):
        raise RuntimeError("секрет-из-промпта")

    state = wait(jobs.submit(работа))
    assert state["status"] == jobs.FAILED
    assert "секрет-из-промпта" not in state["error"]
    assert "RuntimeError" in state["error"]


def test_несуществующая_задача_даёт_none():
    assert jobs.status("нет-такой") is None


def test_время_работы_считается():
    job_id = jobs.submit(lambda progress: {"ок": True})
    wait(job_id)
    assert jobs.status(job_id)["elapsed"] >= 0


def test_время_шага_отдельно_от_общего():
    """Страница показывает их РАЗНО: общее время рядом с названием шага читалось
    как «этот шаг висит семь минут», хотя семь минут шёл весь проход."""
    started = threading.Event()
    release = threading.Event()

    def работа(progress):
        time.sleep(0.15)                 # что-то уже сделано до первого шага
        progress.say("оценка по линзам")
        started.set()
        release.wait(5)
        return {}

    job_id = jobs.submit(работа)
    assert started.wait(5)

    state = jobs.status(job_id)
    assert state["step_elapsed"] < state["elapsed"]
    release.set()
    wait(job_id)
