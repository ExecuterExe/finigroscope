# -*- coding: utf-8 -*-
"""Клиент оценки по линзам: стыковка с ФинИгроСкопом.

Сети нет: urlopen подменяется заглушкой.

Проверяется то, за что отвечает ЭТОТ код, а не агент линз: заявка уходит с
нужными полями и заголовком, ожидание асинхронное и с ограничением, а всякий
отказ соседнего сервиса превращается в текст, по которому видно, что чинить.
Сам балл и его арифметика проверяются на стороне ФинИгроСкопа
(finigroskop/tests/lens_module_check.py) — там, где живёт агент.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import lens_review  # noqa: E402
from config import config       # noqa: E402


MODULE = {"title": "Исследование локаций"}
PARAMS = {"purpose": ["Развлечение"]}
AUDIT = {"map": [{"item": "genre_match", "status": "ok"}], "issues": []}
RESULT = {"ready": True, "available": True, "phase": "mechanics",
          "score": {"overall": 7.412, "passed": True, "weight_covered": 0.525}}


class FakeResponse(object):
    def __init__(self, payload):
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Server(object):
    """Заглушка ФинИгроСкопа: помнит запросы и отдаёт заготовленные ответы."""

    def __init__(self, submit, *statuses):
        self.submit = submit
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = None
        if request.data:
            body = json.loads(request.data.decode("utf-8"))
        self.requests.append({"url": request.full_url,
                              "method": request.get_method(),
                              "headers": dict(request.headers),
                              "body": body})
        if request.get_method() == "POST":
            if isinstance(self.submit, Exception):
                raise self.submit
            return FakeResponse(self.submit)
        outcome = self.statuses.pop(0) if self.statuses else {"status": "running"}
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture
def server(monkeypatch):
    """Подменяет urlopen и убирает паузы между опросами."""
    monkeypatch.setattr(lens_review, "POLL_INTERVAL", 0)

    def run(submit, *statuses):
        fake = Server(submit, *statuses)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return run


def evaluate(**kwargs):
    return lens_review.evaluate("mechanics", MODULE, PARAMS, AUDIT, **kwargs)


# --------------------------------------------------------------------------
# Заявка
# --------------------------------------------------------------------------

def test_заявка_уходит_со_всеми_полями(server):
    fake = server({"ready": True, "job_id": "abc"}, {"status": "done", "result": RESULT})
    evaluate()
    sent = fake.requests[0]["body"]
    assert sent["phase"] == "mechanics"
    assert sent["module"] == MODULE
    assert sent["params"] == PARAMS
    # аудит обязателен: линзы идут ПОСЛЕ него, и по нему решается, звать ли их
    assert sent["audit"] == AUDIT


def test_токен_уходит_заголовком(server, monkeypatch):
    monkeypatch.setattr(config, "lens_api_token", "s3cret")
    fake = server({"ready": True, "job_id": "abc"}, {"status": "done", "result": RESULT})
    evaluate()
    headers = {k.lower(): v for k, v in fake.requests[0]["headers"].items()}
    assert headers.get("X-lens-token".lower()) == "s3cret"


def test_без_токена_заголовка_нет(server, monkeypatch):
    monkeypatch.setattr(config, "lens_api_token", "")
    fake = server({"ready": True, "job_id": "abc"}, {"status": "done", "result": RESULT})
    evaluate()
    headers = {k.lower() for k in fake.requests[0]["headers"]}
    assert "x-lens-token" not in headers


# --------------------------------------------------------------------------
# Ожидание результата
# --------------------------------------------------------------------------

def test_заявка_возвращается_сразу_без_ожидания(server):
    """Страница получает номер задачи мгновенно и дальше опрашивает сама.

    Ждать внутри обработчика нельзя: оценка идёт до 600 с (300 на вызов плюс
    второй заход при неудачной самопроверке), а nginx рвёт запрос на 300 с.
    """
    fake = server({"ready": True, "job_id": "abc"})
    out = lens_review.submit("mechanics", MODULE, PARAMS, AUDIT)
    assert out["job_id"] == "abc"
    # ни одного опроса состояния: заявка и ожидание разделены
    assert len(fake.requests) == 1


def test_опрос_состояния_не_ждёт(server):
    fake = server({"ready": True, "job_id": "abc"}, {"status": "running"})
    state = lens_review.poll("abc")
    assert state["status"] == "running"
    assert len(fake.requests) == 1


def test_опрос_упавшей_задачи_бросает_ошибку(server):
    server({"ready": True, "job_id": "abc"},
           {"status": "failed", "error": "Оценка по линзам не выполнена: ValueError"})
    with pytest.raises(lens_review.LensError) as error:
        lens_review.poll("abc")
    assert "ValueError" in str(error.value)


def test_опрос_без_номера_задачи_бросает_ошибку(server):
    server({})
    with pytest.raises(lens_review.LensError):
        lens_review.poll("")


def test_ждёт_пока_оценка_идёт(server):
    fake = server({"ready": True, "job_id": "abc"},
                  {"status": "queued"}, {"status": "running"},
                  {"status": "done", "result": RESULT})
    assert evaluate() == RESULT
    # одна заявка плюс три опроса состояния
    assert len(fake.requests) == 4


def test_балл_доходит_без_искажений(server):
    server({"ready": True, "job_id": "abc"}, {"status": "done", "result": RESULT})
    # три знака после запятой — не украшение: по ним сравнивают варианты между собой
    assert evaluate()["score"]["overall"] == 7.412


def test_модуль_с_нарушениями_не_идёт_к_модели(server):
    """Штатный исход, а не ошибка: сперва чинят, потом оценивают."""
    fake = server({"ready": False, "reason": "есть критичные замечания",
                   "blocking": {"violations": 1, "critical": 0}})
    out = evaluate()
    assert out["ready"] is False
    assert "критичные" in out["reason"]
    # состояние не опрашивали — задачи и не заводили
    assert len(fake.requests) == 1


def test_упавшая_оценка_даёт_понятную_ошибку(server):
    server({"ready": True, "job_id": "abc"},
           {"status": "failed", "error": "Оценка по линзам не выполнена: TimeoutError"})
    with pytest.raises(lens_review.LensError) as error:
        evaluate()
    assert "TimeoutError" in str(error.value)


def test_срок_берётся_у_оценщика_а_не_из_своей_константы(monkeypatch):
    """Две константы в разных сервисах расходятся молча, и тогда генератор
    сдаётся раньше, чем оценщик закончил: ошибка есть, а работа идёт."""
    monkeypatch.setattr(config, "lens_timeout", 999)
    assert lens_review.wait_for({"budget_seconds": 300}) == 300 + lens_review.WAIT_SLACK


def test_без_бюджета_остаётся_своя_константа(monkeypatch):
    """Старый оценщик поля не пришлёт — ждать всё равно надо."""
    monkeypatch.setattr(config, "lens_timeout", 360)
    assert lens_review.wait_for({}) == 360


def test_явный_срок_сильнее_всего(monkeypatch):
    monkeypatch.setattr(config, "lens_timeout", 360)
    assert lens_review.wait_for({"budget_seconds": 300}, wait=5) == 5


def test_мусорный_бюджет_игнорируется(monkeypatch):
    monkeypatch.setattr(config, "lens_timeout", 360)
    assert lens_review.wait_for({"budget_seconds": 0}) == 360
    assert lens_review.wait_for({"budget_seconds": "много"}) == 360


def test_ожидание_ограничено(server):
    """Иначе вкладка висела бы вечно на задаче, которая уже никогда не ответит."""
    server({"ready": True, "job_id": "abc"}, {"status": "running"})
    with pytest.raises(lens_review.LensError) as error:
        evaluate(wait=0)
    assert "не уложилась" in str(error.value)
    # номер задачи в сообщении есть: по нему можно спросить результат позже
    assert "abc" in str(error.value)


def test_ответ_без_номера_задачи_это_ошибка(server):
    server({"ready": True})
    with pytest.raises(lens_review.LensError) as error:
        evaluate()
    assert "номер задачи" in str(error.value)


# --------------------------------------------------------------------------
# Отказы соседнего сервиса
# --------------------------------------------------------------------------

def http_error(code, detail=""):
    body = json.dumps({"error": detail}).encode("utf-8")
    import io
    return urllib.error.HTTPError(config.lens_api_url, code, "err", {},
                                  io.BytesIO(body))


def test_недоступный_финигроскоп_подсказывает_что_проверить(server):
    server(urllib.error.URLError("Connection refused"))
    with pytest.raises(lens_review.LensError) as error:
        evaluate()
    text = str(error.value)
    assert "LENS_API_URL" in text
    assert config.lens_api_url in text


def test_403_подсказывает_про_токен(server):
    server(http_error(403))
    with pytest.raises(lens_review.LensError) as error:
        evaluate()
    assert "LENS_API_TOKEN" in str(error.value)


def test_404_на_задаче_объясняет_причину(server):
    server({"ready": True, "job_id": "abc"}, http_error(404))
    with pytest.raises(lens_review.LensError) as error:
        evaluate()
    assert "перезапускался" in str(error.value)


def test_номер_задачи_экранируется_в_адресе(server):
    """Номер приходит снаружи, а http.client кодирует строку запроса в ASCII.

    На букве вне латиницы он падал ДО отправки, обработчик обрывал соединение
    без ответа, и страница показывала «сервер не ответил» — неотличимо от
    зависшей сети. Поймано живой проверкой, а не в бою.
    """
    fake = server({"ready": True, "job_id": "x"}, {"status": "running"})
    lens_review.poll("задача-№1")
    url = fake.requests[0]["url"]
    assert url.isascii(), "в адресе остались символы вне ASCII: " + url
    assert "%" in url


def test_адрес_собирается_без_двойного_слэша(server, monkeypatch):
    monkeypatch.setattr(config, "lens_api_url", "http://127.0.0.1:5000/")
    fake = server({"ready": True, "job_id": "abc"}, {"status": "done", "result": RESULT})
    evaluate()
    assert fake.requests[0]["url"] == "http://127.0.0.1:5000/api/lenses/module"
