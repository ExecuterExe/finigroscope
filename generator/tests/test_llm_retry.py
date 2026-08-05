# -*- coding: utf-8 -*-
"""Повторы транспорта в llm._post.

Сетевых вызовов нет: urlopen подменяется заглушкой с заготовленной
последовательностью исходов.

Зачем эти тесты. Агент механик делает до трёх попыток, но только когда модель
вернула негодный ответ; сетевая ошибка вылетала из его цикла сразу и отменяла
генерацию целиком. Один обрыв TLS («EOF occurred in violation of protocol»)
стоил пользователю всей минуты ожидания. Повтор добавлен в транспорт, и здесь
закреплено главное — ЧТО повторяется, а что нет: повторять ошибку ключа или
таймаут не только бесполезно, но и вредно.
"""

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm  # noqa: E402


# --------------------------------------------------------------------------
# Оснастка
# --------------------------------------------------------------------------

GOOD = {"choices": [{"message": {"content": "готово"}}]}


class FakeResponse(object):
    """Минимальный ответ urlopen: контекстный менеджер с .read()."""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeUrlopen(object):
    """urlopen с заготовленной последовательностью исходов.

    Элемент — исключение (будет брошено), словарь (уйдёт как JSON) или готовые
    байты (так проверяется ответ, который вообще не разбирается). Последний
    элемент повторяется, если вызовов окажется больше.
    """

    def __init__(self, *outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def __call__(self, request, timeout=None):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, bytes):
            return FakeResponse(outcome)
        return FakeResponse(json.dumps(outcome).encode("utf-8"))


def http_error(code):
    detail = json.dumps({"error": {"message": "подробность от провайдера"}})
    return urllib.error.HTTPError(llm.API_URL, code, "err", {},
                                  io.BytesIO(detail.encode("utf-8")))


def dropped():
    """Обрыв соединения — ровно то, что ловил живой сбой."""
    return urllib.error.URLError("EOF occurred in violation of protocol")


@pytest.fixture
def send(monkeypatch):
    """Подменяет urlopen и убирает настоящие паузы между повторами."""
    monkeypatch.setattr(llm, "RETRY_PAUSE", 0)

    def run(*outcomes):
        fake = FakeUrlopen(*outcomes)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return run


def post():
    return llm._post(urllib.request.Request(llm.API_URL), 10)


# --------------------------------------------------------------------------
# Повторяется то, что может пройти само
# --------------------------------------------------------------------------

def test_обрыв_связи_повторяется_и_запрос_доходит(send):
    fake = send(dropped(), GOOD)
    assert post() == GOOD
    assert fake.calls == 2


def test_429_повторяется(send):
    fake = send(http_error(429), GOOD)
    assert post() == GOOD
    assert fake.calls == 2


def test_ошибка_на_стороне_провайдера_повторяется(send):
    fake = send(http_error(502), GOOD)
    assert post() == GOOD
    assert fake.calls == 2


def test_постоянный_обрыв_сдаётся_после_всех_попыток(send):
    fake = send(dropped())
    with pytest.raises(llm.LLMError) as error:
        post()
    assert fake.calls == llm.SEND_ATTEMPTS
    # пользователю важно понять, что дело не в одной случайности
    assert "попытки" in str(error.value)


# --------------------------------------------------------------------------
# НЕ повторяется то, где повтор бесполезен или вреден
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [400, 401, 402, 404])
def test_ошибки_запроса_не_повторяются(send, code):
    """Ключ, оплата, название модели: ответ будет тот же, ждать незачем."""
    fake = send(http_error(code))
    with pytest.raises(llm.LLMError):
        post()
    assert fake.calls == 1


def test_таймаут_не_повторяется(send):
    """Время уже потрачено, а запрос мог дойти и посчитаться — повтор рискует
    оплатить генерацию дважды."""
    fake = send(TimeoutError())
    with pytest.raises(llm.LLMError) as error:
        post()
    assert fake.calls == 1
    assert "не ответил" in str(error.value)


def test_не_json_не_повторяется(send):
    """Провайдер ответил, но мусором (типичная страница-заглушка): разбирать
    нечего, и повтор ничего не изменит."""
    fake = send(b"<html>503 Service Unavailable</html>")
    with pytest.raises(llm.LLMError) as error:
        post()
    assert fake.calls == 1
    assert "не JSON" in str(error.value)


# --------------------------------------------------------------------------
# Сообщение об ошибке остаётся понятным пользователю
# --------------------------------------------------------------------------

def test_текст_ошибки_ключа_подсказывает_где_чинить(send):
    send(http_error(401))
    with pytest.raises(llm.LLMError) as error:
        post()
    assert "OPENROUTER_API_KEY" in str(error.value)
