# -*- coding: utf-8 -*-
"""Настройки хостинга: забытая переменная не должна стоить платных вызовов.

Сети нет. Проверяется то, что видно только на развёрнутом сервисе и потому
легче всего ломается незаметно: на машине разработчика localhost — правильный
адрес, и все проверки молчат.

Повод конкретный. В render.yaml LENS_API_URL и FINIGROSKOP_URL помечены
`sync: false`: Render умеет отдать только host сервиса, а клиенту нужен полный
адрес со схемой, поэтому их вписывают руками ПОСЛЕ первого деплоя. Забыть этот
шаг легко, а последствие молчаливое и дорогое — генератор стучится в
собственный контейнер, и выясняется это на шаге линз, то есть после оплаченных
генерации и аудита.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def настройка(monkeypatch, **env):
    """Свежий config с заданным окружением."""
    for key in ("PORT", "LENS_API_URL", "FINIGROSKOP_URL", "LENS_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import config as config_module
    importlib.reload(config_module)
    return config_module.config


# --------------------------------------------------------------------------
# Локально проверки молчат
# --------------------------------------------------------------------------

def test_локально_localhost_это_норма(monkeypatch):
    """127.0.0.1 на машине разработчика — единственно верный адрес."""
    cfg = настройка(monkeypatch)
    assert cfg.neighbour_problem() is None
    assert cfg.link_problem() is None


# --------------------------------------------------------------------------
# На хостинге — нет
# --------------------------------------------------------------------------

def test_забытый_адрес_соседа_замечен(monkeypatch):
    cfg = настройка(monkeypatch, PORT="10000")
    problem = cfg.neighbour_problem()
    assert problem, "локальный адрес на хостинге прошёл молча"
    assert "LENS_API_URL" in problem
    # текст обязан говорить, ЧТО вписать, а не только что не так
    assert "onrender.com" in problem


def test_http_вместо_https_замечен(monkeypatch):
    cfg = настройка(monkeypatch, PORT="10000",
                    LENS_API_URL="http://finigroskop.example.com",
                    LENS_API_TOKEN="secret")
    assert "https" in (cfg.neighbour_problem() or "")


def test_пустой_общий_секрет_замечен(monkeypatch):
    """Без него ФинИгроСкоп ответит 403 — но уже после оплаченной генерации."""
    cfg = настройка(monkeypatch, PORT="10000",
                    LENS_API_URL="https://finigroskop.onrender.com")
    assert "LENS_API_TOKEN" in (cfg.neighbour_problem() or "")


def test_настроенный_сервис_проверки_проходит(monkeypatch):
    cfg = настройка(monkeypatch, PORT="10000",
                    LENS_API_URL="https://finigroskop.onrender.com",
                    FINIGROSKOP_URL="https://finigroskop.onrender.com",
                    LENS_API_TOKEN="secret")
    assert cfg.neighbour_problem() is None
    assert cfg.link_problem() is None


def test_ссылка_в_шапке_проверяется_отдельно(monkeypatch):
    """Она ломает не работу, а навигацию: смешивать их в одном отказе нельзя —
    из-за нерабочей кнопки нельзя запрещать генерацию."""
    cfg = настройка(monkeypatch, PORT="10000",
                    LENS_API_URL="https://finigroskop.onrender.com",
                    LENS_API_TOKEN="secret")
    assert cfg.neighbour_problem() is None
    assert "FINIGROSKOP_URL" in (cfg.link_problem() or "")


# --------------------------------------------------------------------------
# Ожидание запроса на хостинге длиннее: спящий сервис просыпается минуту
# --------------------------------------------------------------------------

def test_на_хостинге_ждём_дольше(monkeypatch):
    дома = настройка(monkeypatch).lens_http_timeout
    хостинг = настройка(monkeypatch, PORT="10000").lens_http_timeout
    assert хостинг > дома


def test_сторож_перезагрузки_на_хостинге_выключен(monkeypatch):
    """Процессом там управляет платформа, второй сторож плодит процесс,
    которого она не ждёт."""
    assert настройка(monkeypatch, PORT="10000").autoreload is False
    assert настройка(monkeypatch).autoreload is True


def test_на_хостинге_слушаем_наружу(monkeypatch):
    """127.0.0.1 на хостинге — контейнер, который платформа считает мёртвым."""
    host, port = настройка(monkeypatch, PORT="10000").bind()
    assert host == "0.0.0.0"
    assert port == 10000
    assert настройка(monkeypatch).bind()[0] == "127.0.0.1"
