# -*- coding: utf-8 -*-
"""Клиент итогового разбора: заявка, ожидание, пересказ хода работы, отказы.

Сети нет — urlopen подменён. Проверяется то, за что отвечает клиент: что он
отправляет, как ждёт, что делает при сбое и как пересказывает шаги соседнего
сервиса. Сам разбор живёт в ФинИгроСкопе и проверяется там
(tests/headless_check.py).
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import verdict  # noqa: E402
from config import config  # noqa: E402


SPEC = {"game_spec": {"core": {"players": {"min": 2, "max": 4}},
                      "text": {"concept": "игра"}}}
RESULT = {"ok": True, "passed": True, "score": 7.4, "threshold": 6.0,
          "rounds_made": 1, "rounds_allowed": 3, "best_round": 1,
          "verdict": "Игра принята: 7.400 при пороге 6.0.",
          "rounds": [{"attempt": 1, "score": 7.4}]}


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
    """Заглушка ФинИгроСкопа: помнит запросы, отдаёт заготовленные ответы."""

    def __init__(self, submit, *statuses):
        self.submit = submit
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append({"url": request.full_url,
                              "method": request.get_method(),
                              "headers": dict(request.headers), "body": body})
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
    monkeypatch.setattr(verdict, "POLL_INTERVAL", 0)
    monkeypatch.setattr(verdict, "RETRY_DELAY", 0)

    def run(submit, *statuses):
        fake = Server(submit, *statuses)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return run


class Progress(object):
    def __init__(self):
        self.steps = []

    def say(self, step, detail=None, **kw):
        self.steps.append(step)

    def check_cancelled(self):
        return None


def http_error(code, detail="подробность"):
    import io
    body = json.dumps({"error": detail}, ensure_ascii=False).encode("utf-8")
    return urllib.error.HTTPError(config.lens_api_url, code, "err", {},
                                  io.BytesIO(body))


def accepted(**over):
    base = {"job_id": "abc", "code_execution": True, "note": None}
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Заявка
# --------------------------------------------------------------------------

def test_спецификация_уходит_целиком(server):
    fake = server(accepted(), {"status": "done", "result": RESULT})
    verdict.evaluate(SPEC)
    sent = fake.requests[0]["body"]
    assert sent["spec"] == SPEC
    assert fake.requests[0]["url"].endswith("/api/simulate/spec")


def test_токен_уходит_в_заголовке(server, monkeypatch):
    monkeypatch.setattr(config, "lens_api_token", "s3cret")
    fake = server(accepted(), {"status": "done", "result": RESULT})
    verdict.evaluate(SPEC)
    assert fake.requests[0]["headers"].get("X-lens-token") == "s3cret"


def test_пустая_спецификация_не_отправляется(server):
    """Платить за заведомо негодный запрос незачем."""
    fake = server(accepted())
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate({"game_spec": {"text": {}}})
    assert "нет game_spec.core" in str(error.value)
    assert fake.requests == [], "запрос всё-таки ушёл"


def test_ответ_без_номера_задачи_это_ошибка(server):
    server({"code_execution": True})
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "не вернул номер задачи" in str(error.value)


# --------------------------------------------------------------------------
# Ожидание и ход работы
# --------------------------------------------------------------------------

def test_результат_дожидается(server):
    server(accepted(), {"status": "running"}, {"status": "running"},
           {"status": "done", "result": RESULT})
    out = verdict.evaluate(SPEC)
    assert out["score"] == 7.4
    assert out["passed"] is True


def test_шаги_соседа_пересказываются(server):
    """«Идёт» про шесть шагов и три круга — бесполезный ответ."""
    server(accepted(),
           {"status": "running", "step": "сборка скелета", "detail": "модель"},
           {"status": "running", "step": "оценка баланса"},
           {"status": "done", "result": RESULT})
    progress = Progress()
    verdict.evaluate(SPEC, progress=progress)
    assert progress.steps == ["разбор игры: сборка скелета",
                              "разбор игры: оценка баланса"]


def test_повторный_шаг_не_дублируется(server):
    server(accepted(),
           {"status": "running", "step": "диагност"},
           {"status": "running", "step": "диагност"},
           {"status": "done", "result": RESULT})
    progress = Progress()
    verdict.evaluate(SPEC, progress=progress)
    assert progress.steps == ["разбор игры: диагност"]


def test_превышение_срока_называет_номер_задачи(server):
    """Работа могла идти дальше — по номеру её можно спросить снова."""
    server(accepted(), {"status": "running"})
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC, wait=0)
    assert "abc" in str(error.value)


def test_упавший_разбор_даёт_понятную_ошибку(server):
    server(accepted(), {"status": "failed",
                        "error": "Оценка не выполнена: TimeoutError"})
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "TimeoutError" in str(error.value)


# --------------------------------------------------------------------------
# Выключенный прогон кода
# --------------------------------------------------------------------------

def test_запрет_прогона_виден_сразу(server, capsys):
    """Сказать надо в момент заявки, а не через минуту ожидания."""
    note = "Прогон скелета выключен (SIM_API_ALLOW_RUN)."
    server(accepted(code_execution=False, note=note),
           {"status": "done", "result": RESULT})
    out = verdict.evaluate(SPEC)
    assert out["code_execution"] is False
    assert out["note"] == note
    assert "SIM_API_ALLOW_RUN" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Отказы
# --------------------------------------------------------------------------

def test_сетевой_сбой_повторяется_один_раз(server):
    fake = server(urllib.error.URLError("Connection refused"))
    with pytest.raises(verdict.VerdictError):
        verdict.evaluate(SPEC)
    assert len(fake.requests) == 1 + verdict.RETRIES_ON_NETWORK


def test_сон_соседа_назван_в_ошибке(server):
    server(urllib.error.URLError("timed out"))
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "засыпает" in str(error.value)


def test_отказ_ответом_не_повторяется(server):
    fake = server(http_error(403))
    with pytest.raises(verdict.VerdictError):
        verdict.evaluate(SPEC)
    assert len(fake.requests) == 1


def test_403_подсказывает_про_токен(server):
    """Подсказка нужна, когда сервер своей причины не прислал."""
    server(http_error(403, detail=""))
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "LENS_API_TOKEN" in str(error.value)


def test_причина_сервера_сильнее_нашей_подсказки(server):
    """Он знает, что случилось, точнее — заменять его текст догадкой нельзя."""
    server(http_error(403, detail="Неверный или отсутствующий X-Lens-Token."))
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "X-Lens-Token" in str(error.value)


def test_400_говорит_про_упаковку(server):
    server(http_error(400))
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "не принял спецификацию" in str(error.value)


def test_404_объясняет_причину(server):
    server(accepted(), http_error(404, detail=""))
    with pytest.raises(verdict.VerdictError) as error:
        verdict.evaluate(SPEC)
    assert "перезапускался" in str(error.value)


def test_ошибка_помечена_как_показываемая():
    """Иначе очередь покажет имя класса вместо причины."""
    assert verdict.VerdictError.user_facing is True
