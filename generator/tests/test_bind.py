# -*- coding: utf-8 -*-
"""Где сервис слушает и когда считает себя запущенным на хостинге.

Проверка выглядит мелкой, но закрывает три сбоя, каждый из которых выглядит
как «всё хорошо» и при этом означает неработающий сервис:

  • слушать 127.0.0.1 на хостинге — платформа не находит открытого порта и
    убивает контейнер, а в журнале написано «сервер запущен»;
  • отказывать в вызовах модели из-за отсутствия файла .env — на хостинге его
    нет и быть не должно, переменные приходят из панели;
  • держать сторож автоперезагрузки на сервере — лишний процесс, которого
    платформа не ждёт и не контролирует.
"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as config_module  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    """Чистое окружение: настройки читаются при создании объекта Config."""
    for name in ("PORT", "HOST", "GENERATOR_AUTORELOAD", "LENS_HTTP_TIMEOUT",
                 "OPENROUTER_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)

    def build(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        importlib.reload(config_module)
        return config_module

    yield build
    importlib.reload(config_module)


# --------------------------------------------------------------------------
# Адрес и порт
# --------------------------------------------------------------------------

def test_локально_слушаем_только_себя(env):
    """Сервер разработки не должен без спроса выходить в сеть."""
    module = env()
    assert module.hosted() is False
    assert module.config.bind() == ("127.0.0.1", 8000)


def test_на_хостинге_слушаем_наружу_и_на_его_порту(env):
    """PORT задаёт платформа; на 127.0.0.1 она сервис не увидит."""
    module = env(PORT="10000")
    assert module.hosted() is True
    assert module.config.bind() == ("0.0.0.0", 10000)


def test_явный_адрес_сильнее_признака(env):
    module = env(PORT="10000", HOST="127.0.0.1")
    assert module.config.bind() == ("127.0.0.1", 10000)


def test_нечисловой_порт_не_роняет_запуск(env):
    """Мусор в переменной — повод взять умолчание, а не упасть при старте."""
    module = env(PORT="не число")
    assert module.config.bind()[1] == 8000


# --------------------------------------------------------------------------
# Автоперезагрузка
# --------------------------------------------------------------------------

def test_локально_сторож_включён(env):
    assert env().config.autoreload is True


def test_на_хостинге_сторож_выключен_сам(env):
    """Забытая переменная не должна плодить процесс, которого никто не ждёт."""
    assert env(PORT="10000").config.autoreload is False


def test_сторож_включается_явно_и_на_хостинге(env):
    assert env(PORT="10000", GENERATOR_AUTORELOAD="1").config.autoreload is True


# --------------------------------------------------------------------------
# Готовность модели: проверяется КЛЮЧ, а не файл
# --------------------------------------------------------------------------

def test_ключ_из_окружения_достаточен(env):
    """На хостинге файла .env нет и быть не должно."""
    module = env(OPENROUTER_API_KEY="sk-test")
    assert module.config.problem() is None
    assert module.config.ready is True


def test_без_ключа_подсказка_упоминает_оба_способа(env):
    module = env()
    if module.ENV_FILE.is_file():
        pytest.skip("рядом лежит настоящий .env — проверять нечего")
    text = module.config.problem()
    assert "OPENROUTER_API_KEY" in text
    assert ".env" in text and "панели" in text


def test_чужой_провайдер_называется_прямо(env):
    module = env(LLM_PROVIDER="openai", OPENROUTER_API_KEY="sk-test")
    assert "openai" in module.config.problem()


# --------------------------------------------------------------------------
# Срок ожидания соседа
# --------------------------------------------------------------------------

def test_на_хостинге_ждём_дольше(env):
    """Спящий сосед просыпается около минуты — двадцати секунд там мало."""
    assert env().config.lens_http_timeout == 20
    assert env(PORT="10000").config.lens_http_timeout == 90


def test_явный_срок_сильнее_умолчания(env):
    assert env(PORT="10000", LENS_HTTP_TIMEOUT="15").config.lens_http_timeout == 15
