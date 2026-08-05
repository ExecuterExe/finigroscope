# -*- coding: utf-8 -*-
"""Неучтённый сбой в обработчике обязан стать ОТВЕТОМ, а не обрывом связи.

Пока этого не было, любое исключение в маршруте долетало до
ThreadingHTTPServer, тот молча закрывал соединение, и страница получала
«сервер не ответил». Отличить это от зависшей сети или уснувшего сервера
нельзя — а разница огромная: в одном случае надо чинить код, в другом ждать.

Проверка поднимает настоящий сервер на свободном порту: подменять обработчик
изнутри значило бы проверять что угодно, кроме того, доходит ли ответ по сети.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as generator_app  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    """Запущенный генератор на свободном порту."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), generator_app.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def post(base, path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_падение_маршрута_возвращает_500_а_не_обрыв(server, monkeypatch, capsys):
    def взрывается(payload):
        raise ZeroDivisionError("внутренняя поломка")

    monkeypatch.setattr(generator_app.Handler, "route_lens_status",
                        lambda self, payload: взрывается(payload))

    code, body = post(server, "/api/lenses/status", {"job_id": "x"})
    assert code == 500
    # тип ошибки виден — по нему понятно, что чинить
    assert "ZeroDivisionError" in body["error"]
    assert body["stage"] == "сервер"


def test_неизвестный_адрес_отвечает_404(server):
    # Путь латиницей намеренно: http.client кодирует строку запроса в ASCII и
    # на кириллице падает ещё до отправки — проверять надо ответ сервера, а не
    # ограничение клиента.
    code, body = post(server, "/api/no-such-route", {})
    assert code == 404
    assert "error" in body


def test_тело_не_json_отвечает_400(server):
    request = urllib.request.Request(
        server + "/api/lenses/status", data=b"not json", method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    assert code == 400
