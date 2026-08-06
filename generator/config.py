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


def hosted():
    """Запущены ли мы на хостинге, а не на машине разработчика.

    Признак — переменная `PORT`. Её задаёт платформа (Render, Railway, Heroku и
    прочие того же рода): она сама выбирает порт и требует, чтобы процесс слушал
    именно его. Локально её никто не ставит.

    По этому признаку меняются ровно две вещи — на каком адресе слушать и нужен
    ли сторож автоперезагрузки. Обе можно перебить явной переменной; признак
    нужен для того, чтобы забытая настройка не превращалась в неработающий
    сервис, который при этом рапортует «запущен».
    """
    return bool(os.environ.get("PORT"))


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
        # Должен быть БОЛЬШЕ общего бюджета оценщика линз в ФинИгроСкопе
        # (lens_evaluator.TOTAL_BUDGET = 300 с) с запасом на очередь и сеть.
        # Если сдаться раньше, чем он закончит, получится худшее из возможного:
        # страница показывает ошибку, а работа продолжается и тратит время.
        # Запасное значение: обычно срок приходит от самого оценщика полем
        # budget_seconds (см. lens_review.wait_for), и генератор ждёт его плюс
        # WAIT_SLACK. Здесь — на случай старого оценщика, который поля не
        # пришлёт. Держать его больше бюджета обязательно: сдавшийся раньше
        # генератор показывает ошибку, пока работа идёт и тратит деньги.
        self.lens_timeout = _number("LENS_TIMEOUT", 1100, int)
        # Сколько ждать ОТДЕЛЬНОГО http-запроса к ФинИгроСкопу (не оценки
        # целиком). На бесплатных тарифах спящий сервис просыпается около
        # минуты, и двадцати секунд по умолчанию там не хватает: первый вызов
        # после простоя падал бы всегда, обесценивая уже оплаченную генерацию.
        self.lens_http_timeout = _number("LENS_HTTP_TIMEOUT",
                                         90 if hosted() else 20, int)
        # Перезапускать сервер при правке .py. Только для разработки: на сервере
        # процессом управляет платформа или systemd, и второй сторож там лишний —
        # он плодит процесс, который платформа не ждёт и не контролирует.
        self.autoreload = _text("GENERATOR_AUTORELOAD",
                                "0" if hosted() else "1") not in ("0", "false", "no")
        self.timeout = _number("LLM_TIMEOUT", 60, int)
        self.max_tokens = _number("LLM_MAX_TOKENS", 2000, int)
        self.temperature = _number("LLM_TEMPERATURE", 0.7, float)

    @property
    def ready(self):
        return bool(self.api_key) and self.provider == "openrouter"

    def problem(self):
        """Человекочитаемая причина, почему запрос к модели невозможен.

        Проверяется КЛЮЧ, а не файл. Раньше первым условием стояло «нет .env» —
        и на хостинге это отказывало во всём: файла там нет и быть не может,
        переменные приходят из панели, а ключ при этом задан. Файл — лишь один
        из способов передать ключ, и путать способ с наличием нельзя.
        """
        if self.provider != "openrouter":
            return ("LLM_PROVIDER=%s не поддерживается, ожидается openrouter."
                    % self.provider)
        if not self.api_key:
            if ENV_FILE.is_file():
                return ("В .env не заполнен OPENROUTER_API_KEY. "
                        "Ключ берётся на https://openrouter.ai/keys")
            return ("Ключ OpenRouter не задан. Локально: скопируйте .env.example "
                    "в .env и впишите OPENROUTER_API_KEY. На сервере переменные "
                    "задаются в панели хостинга — файл .env туда не попадает "
                    "и попадать не должен.")
        return None

    def bind(self):
        """Адрес и порт, на которых слушать. Возвращает (host, port).

        Порт на хостинге назначает платформа и передаёт в `PORT`; слушать надо
        `0.0.0.0`, иначе снаружи сервис недоступен, платформа не находит
        открытого порта и убивает контейнер — при том что в журнале написано
        «сервер запущен».

        Локально по умолчанию `127.0.0.1`: сервер разработки не должен без
        спроса выходить в сеть. Оба значения перебиваются переменными.
        """
        default_host = "0.0.0.0" if hosted() else "127.0.0.1"    # noqa: S104
        return _text("HOST", default_host), _number("PORT", 8000, int)

    def neighbour_problem(self):
        """Почему связь с ФинИгроСкопом заведомо не заработает. None — всё цело.

        Проверяется только НА ХОСТИНГЕ: локально `127.0.0.1:5000` — правильный и
        единственно верный адрес.

        Зачем. В render.yaml LENS_API_URL и FINIGROSKOP_URL помечены
        `sync: false`: Render умеет отдать только host сервиса, а клиенту нужен
        полный адрес со схемой, поэтому их вписывают руками ПОСЛЕ первого
        деплоя. Забыть этот шаг легко — а последствие молчаливое и дорогое:
        адрес остаётся локальным, генератор стучится в собственный контейнер,
        где ничего не слушает, и выясняется это только на шаге линз, то есть
        ПОСЛЕ оплаченных генерации и аудита.

        Ровно то, от чего защищает признак `hosted()` в случае с портом:
        забытая настройка не должна превращаться в сервис, который рапортует
        «запущен» и не работает.
        """
        if not hosted():
            return None

        local = ("127.0.0.1", "localhost", "0.0.0.0")
        if any(mark in self.lens_api_url for mark in local):
            return ("LENS_API_URL = %s — это локальный адрес, а сервис работает "
                    "на хостинге. Оценка по линзам живёт в ФинИгроСкопе и по "
                    "этому адресу недоступна: генератор постучится в собственный "
                    "контейнер. Впишите полный адрес соседнего сервиса "
                    "(https://<имя>.onrender.com) в переменную LENS_API_URL."
                    % self.lens_api_url)
        if not self.lens_api_url.startswith("https://"):
            return ("LENS_API_URL = %s без https. На хостинге страница отдаётся "
                    "по https, и обращение к http-адресу браузер заблокирует, а "
                    "часть платформ не выпустит наружу и серверный запрос."
                    % self.lens_api_url)
        if not self.lens_api_token:
            return ("LENS_API_TOKEN пуст. Эндпоинт линз тратит деньги на модель "
                    "и вне разработки закрыт: без общего секрета ФинИгроСкоп "
                    "ответит 403. Значение должно совпадать у обоих сервисов — "
                    "в render.yaml для этого заведена группа ecosystem-shared.")
        return None

    def link_problem(self):
        """То же про ссылку в шапке. Отдельно: она ломает не работу, а навигацию."""
        if not hosted():
            return None
        local = ("127.0.0.1", "localhost", "0.0.0.0")
        if any(mark in self.finigroskop_url for mark in local):
            return ("FINIGROSKOP_URL = %s — локальный адрес. Кнопка перехода в "
                    "шапке ведёт в никуда у каждого посетителя."
                    % self.finigroskop_url)
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
