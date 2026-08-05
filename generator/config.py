# -*- coding: utf-8 -*-
"""Настройки из .env.

Свой разбор .env, а не python-dotenv: проект держится на стандартной
библиотеке, ставить зависимость ради двух десятков строк незачем.

Ключ живёт только здесь и в llm.py. В браузер он не уходит никогда:
запросы к модели делает сервер, страница обращается к своему же /api.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env(path=ENV_FILE):
    """Читает KEY=VALUE из .env в os.environ.

    Уже существующие переменные окружения не перетираем: если ключ задан
    в системе или в CI, он должен побеждать файл.
    """
    if not path.is_file():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # значение можно взять в кавычки, если в нём есть пробелы
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env()


def _text(name, default=""):
    return os.environ.get(name, default).strip()


def _number(name, default, cast):
    raw = _text(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


class Config:
    """Разобранные настройки. Значение ключа наружу не отдаём — только
    признак того, задан он или нет."""

    def __init__(self):
        self.provider = _text("LLM_PROVIDER", "openrouter").lower()
        self.api_key = _text("OPENROUTER_API_KEY")
        self.model_pro = _text("OPENROUTER_MODEL_PRO", "deepseek/deepseek-chat")
        self.model_flash = _text("OPENROUTER_MODEL_FLASH", "deepseek/deepseek-chat")
        # рассуждающая модель для проходов аудита, где нужна семантическая
        # сверка; пусто — работаем на обычной, без рассуждений
        self.model_reasoning = _text("OPENROUTER_MODEL_REASONING")
        self.app_title = _text("OPENROUTER_APP_TITLE", "Generator igr")
        self.app_url = _text("OPENROUTER_APP_URL", "http://localhost:8000")
        # Соседний сервис экосистемы: кнопка перехода в шапке страницы. Зашивать
        # адрес в HTML нельзя — в разработке это порт, на сервере путь за nginx.
        self.finigroskop_url = _text("FINIGROSKOP_URL", "http://localhost:5000")

        # Адрес ФинИгроСкопа для вызовов СЕРВЕР-СЕРВЕР (оценка по линзам Шелла).
        # Отдельная переменная, а не повторное использование FINIGROSKOP_URL:
        # та предназначена браузеру и на сервере равна пути «/», по которому
        # Python постучаться не сможет. Здесь всегда нужен полный адрес.
        self.lens_api_url = _text("LENS_API_URL", "http://127.0.0.1:5000")
        # Общий секрет: эндпоинт линз тратит деньги на модель и закрыт им.
        self.lens_api_token = _text("LENS_API_TOKEN")
        # Линзы отвечают дольше генерации: читают модуль и разбирают его по
        # десяткам линз. Ждём столько же, сколько отведено самому агенту.
        self.lens_timeout = _number("LENS_TIMEOUT", 330, int)
        # Перезапускать сервер при правке .py. Только для разработки; на сервере
        # процессом управляет systemd, и второй сторож там лишний.
        self.autoreload = _text("GENERATOR_AUTORELOAD", "1") not in ("0", "false", "no")
        self.timeout = _number("LLM_TIMEOUT", 60, int)
        self.max_tokens = _number("LLM_MAX_TOKENS", 2000, int)
        self.temperature = _number("LLM_TEMPERATURE", 0.7, float)

    @property
    def ready(self):
        return bool(self.api_key) and self.provider == "openrouter"

    def problem(self):
        """Человекочитаемая причина, почему запрос к модели невозможен."""
        if not ENV_FILE.is_file():
            return ("Нет файла .env. Скопируйте .env.example в .env "
                    "и впишите ключ OpenRouter.")
        if self.provider != "openrouter":
            return ("LLM_PROVIDER=%s не поддерживается, ожидается openrouter."
                    % self.provider)
        if not self.api_key:
            return ("В .env не заполнен OPENROUTER_API_KEY. "
                    "Ключ берётся на https://openrouter.ai/keys")
        return None

    def status(self):
        """Безопасная для показа сводка — без ключа."""
        return {
            "provider": self.provider,
            "ready": self.ready,
            "problem": self.problem(),
            "models": {"pro": self.model_pro, "flash": self.model_flash,
                       "reasoning": self.model_reasoning or None},
            "env_file": ENV_FILE.name if ENV_FILE.is_file() else None,
        }

    def links(self):
        """Адреса соседних сервисов экосистемы для шапки страницы.

        Отдельно от status(): там сводка про модель, и мешать в неё навигацию
        значит связать два несвязанных повода менять формат ответа.
        """
        return {"finigroskop": self.finigroskop_url}


config = Config()
