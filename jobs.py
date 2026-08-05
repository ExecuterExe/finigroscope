"""Фоновое выполнение шагов конвейера — пул потоков поверх таблицы задач.

Зачем это вообще. Все обращения к языковой модели шли синхронно внутри
обработчика запроса. По факту живых прогонов: проход диагноста — 126 и 352
секунды, разбор по линзам — 564. Всё это время браузер ждал ответа, а
однопоточный сервер разработки не мог обслужить никого другого: второй
пользователь ждал вообще всё. Ни прогресса, ни отмены, ни возможности закрыть
вкладку и вернуться к готовому результату.

Почему без Celery, Redis и брокеров. Для сервиса такого размера это лишняя
инфраструктура: пул потоков из стандартной библиотеки плюс таблица задач в той
же базе решают ту же задачу, ничего не требуя от окружения. Ограничение честное
и его стоит знать: очередь живёт ВНУТРИ процесса, и при нескольких воркерах
(gunicorn) у каждого будет своя. Пока сервис однопроцессный — это верно; при
переходе на несколько процессов сюда понадобится настоящая очередь.

Три вещи, ради которых модуль написан именно так:

1. **Идемпотентность.** Повторный запрос на уже идущий шаг НЕ создаёт вторую
   задачу — возвращается существующая. Без этого двойной клик по кнопке удваивал
   бы расходы на модель, причём незаметно: оба вызова успешны, счёт двойной.
2. **Результат отделён от состояния.** Задача знает «идёт/готово/упало», а
   данные пишет тот же самый код, что и раньше, в свою таблицу шага. Поэтому
   переход на фоновый режим не переписывает ни одного агента.
3. **Переживание перезапуска.** Задачи, застигнутые перезапуском сервера,
   помечаются `failed` с внятной причиной, а не висят в `running` вечно.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import config
from models import Job, db

# Человекочитаемые названия шагов — для экрана и для ответа API.
STEP_TITLES = {
    "mirror": "Стадия понимания игры",
    "extraction": "Извлечение структуры",
    "gate2": "Второй гейт",
    "skeleton": "Сборка скелета-симулятора",
    "balance": "Оценка баланса по статистике",
    "diagnost_triage": "Диагност: триаж тестов",
    "diagnost_findings": "Диагност: вердикты",
    "lenses": "Оценка по линзам",
    "synthesis": "Итоговая оценка",
    "redesign": "Подбор правки",
}

# Сколько примерно идёт шаг — чтобы экран не выглядел зависшим. Числа взяты из
# живых прогонов, это ориентир для человека, а не таймаут.
STEP_HINTS = {
    "mirror": "обычно до минуты",
    "extraction": "обычно до минуты",
    "gate2": "обычно до минуты",
    "skeleton": "агент пишет целый файл кода — это дольше всего, до нескольких минут",
    "balance": "обычно меньше минуты",
    "diagnost_triage": "агент разбирает 50 тестов — до нескольких минут",
    "diagnost_findings": "самый долгий проход диагноста — до нескольких минут",
    "lenses": "разбор по 47 линзам — до нескольких минут",
    "synthesis": "обычно до минуты",
    "redesign": "обычно до минуты",
}

# Куда ведёт кнопка «прогнать заново» на плашке сбоя. Нужна с тех пор, как
# экран перестал перезапускать шаг сам (см. may_autostart): без явной кнопки
# сбой любого шага стал бы тупиком.
STEP_RETRY_ENDPOINT = {
    "mirror": "mirror_retry",
    "extraction": "spec_retry",
    "gate2": "gate2_retry",
    "skeleton": "skeleton_retry",
    "balance": "balance_retry",
    "diagnost_triage": "diagnost_retry",
    "diagnost_findings": "diagnost_retry",
    "lenses": "lenses_retry",
    "synthesis": "synthesis_retry",
}

_app = None
_pool: ThreadPoolExecutor | None = None
# Один замок на «найти или создать». Гонка тут реальна: два быстрых клика дают
# два запроса, и без замка оба не увидят чужую задачу и создадут по своей.
# Уникальный индекс в базе не спасёт — активной задача бывает в двух статусах,
# а частичные индексы SQLAlchemy описывает неудобно; для однопроцессного
# сервиса замка достаточно, и он честнее выглядит.
_lock = threading.Lock()


class Cancelled(Exception):
    """Задачу отменил автор между шагами."""


def init_app(app) -> None:
    """Привязывает пул к приложению и подчищает хвосты прошлого запуска."""
    global _app, _pool
    _app = app
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=config.max_workers(),
                                   thread_name_prefix="finigro-job")
    with app.app_context():
        recover_orphans()


def recover_orphans() -> int:
    """Задачи, застигнутые перезапуском сервера, переводит в `failed`.

    Без этого они висели бы в `running` вечно: поток, который их выполнял, умер
    вместе с прошлым процессом, и завершить их больше некому. Экран при этом
    показывал бы «идёт…» до скончания века, а автор ждал бы ответа, которого не
    будет. Честное «прервано перезапуском, запустите заново» лучше.
    """
    stale = Job.query.filter(Job.status.in_(Job.ACTIVE)).all()
    for job in stale:
        job.status = Job.FAILED
        job.error = ("Сервер перезапустился, пока шаг выполнялся. Результат не "
                     "сохранён — запустите шаг заново.")
        job.finished_at = datetime.utcnow()
    if stale:
        db.session.commit()
    return len(stale)


def active(document_id: int, game_index: int, step: str):
    """Уже идущая задача этого шага, если она есть."""
    return (Job.query
            .filter(Job.document_id == document_id, Job.game_index == game_index,
                    Job.step == step, Job.status.in_(Job.ACTIVE))
            .order_by(Job.id.desc()).first())


def latest(document_id: int, game_index: int, step: str):
    """Последняя задача шага — любого статуса (для показа ошибки после сбоя)."""
    return (Job.query
            .filter_by(document_id=document_id, game_index=game_index, step=step)
            .order_by(Job.id.desc()).first())


def may_autostart(document_id: int, game_index: int, step: str, since=None) -> bool:
    """Можно ли ЭКРАНУ запустить шаг самому, без нажатия автора.

    Разрешено ровно один раз на попытку. Если по шагу уже есть задача, созданная
    после появления текущей записи результата, экран больше не запускает ничего
    сам: он показывает, чем кончилось, и предлагает кнопку «прогнать заново».

    Правило появилось после живой проверки и стоит денег. Условия автозапуска
    смотрели только на «результата ещё нет» и «задача не идёт прямо сейчас».
    Пока страница была статической, это означало один вызов модели на одно
    открытие экрана. С плашкой прогресса, которая перезагружает страницу сама,
    получился ЦИКЛ: шаг завершился, ничего не записав (модель недоступна, ответ
    не разобрался), страница перезагрузилась, экран увидел «результата нет» и
    запустил шаг снова. На дев-сервере это дало восемь обращений к модели подряд
    без единого действия человека — и остановиться само не могло.

    `since` — момент появления записи результата (её `created_at`). Он нужен для
    шагов, которые честно идут по нескольку раз: новый круг авто-редизайна
    заводит НОВУЮ запись синтеза, и задача прошлого круга её автозапуск не
    блокирует. Ретраи же всегда ставятся явной кнопкой и через это правило не
    проходят вовсе.
    """
    job = latest(document_id, game_index, step)
    if job is None:
        return True
    if job.is_active:
        return False
    if since is not None and job.created_at is not None and job.created_at < since:
        return True
    return False


def submit(document_id: int, game_index: int, step: str, fn, stage: str = None):
    """Ставит шаг в очередь и СРАЗУ возвращает задачу.

    `fn` — функция ОДНОГО аргумента: ей передаётся `job_id` собственной задачи.
    Большинству тел он не нужен (принимают и игнорируют), но длинным
    многоэтапным шагам он необходим, чтобы вызвать `check_cancelled` между
    этапами. Передаём именно аргументом, а не через замыкание снаружи: снаружи
    id становится известен только ПОСЛЕ постановки, и в синхронном режиме тело
    успевало отработать раньше, чем его туда клали, — проверка отмены молча не
    срабатывала.

    Тело делает ровно то же, что делал синхронный код до этого: зовёт агента и
    пишет результат в таблицу своего шага. Коммит после него делает пул, поэтому
    само тело про транзакции не думает.

    Повторный вызов на уже идущий шаг возвращает существующую задачу и НЕ
    создаёт вторую — иначе двойной клик стоил бы двух обращений к модели.
    """
    with _lock:
        running = active(document_id, game_index, step)
        if running is not None:
            return running
        job = Job(document_id=document_id, game_index=game_index, step=step,
                  stage=stage or STEP_TITLES.get(step))
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    if config.jobs_sync():
        # Синхронный режим для тестов: тот же самый код, только без потока.
        _execute(job_id, fn)
        return db.session.get(Job, job_id)

    _pool.submit(_run_in_context, job_id, fn)
    return job


def _run_in_context(job_id: int, fn) -> None:
    """Тело потока: свой контекст приложения и своя сессия базы."""
    with _app.app_context():
        try:
            _execute(job_id, fn)
        finally:
            # Сессия у потока своя; не убрав её, мы бы держали соединение до
            # конца жизни потока в пуле.
            db.session.remove()


def _execute(job_id: int, fn) -> None:
    job = db.session.get(Job, job_id)
    if job is None:
        return
    if job.cancel_requested:
        _finish(job, Job.CANCELLED, "Отменено до начала выполнения.")
        return

    job.status = Job.RUNNING
    job.started_at = datetime.utcnow()
    db.session.commit()

    try:
        fn(job_id)
    except Cancelled:
        db.session.rollback()
        _finish(db.session.get(Job, job_id), Job.CANCELLED, "Отменено автором.")
        return
    except Exception as exc:                      # noqa: BLE001 — граница потока
        # Падение внутри задачи НЕ должно ронять сервер: поток умрёт молча, а
        # автор останется с вечным «идёт…». Поэтому исключение ловится целиком,
        # но в базу уходит и текст, и трассировка — иначе разбираться не по чему.
        db.session.rollback()
        detail = traceback.format_exc(limit=6)
        _finish(db.session.get(Job, job_id), Job.FAILED,
                f"{type(exc).__name__}: {exc}\n\n{detail}")
        return

    db.session.commit()
    job = db.session.get(Job, job_id)
    if job is not None and job.cancel_requested:
        # Отмену попросили, пока шаг уже доделывался. Результат сохранён (он
        # оплачен), но статус честный: автор просил остановиться.
        _finish(job, Job.CANCELLED, "Отменено автором, но шаг успел завершиться — "
                                    "результат сохранён.")
        return
    _finish(job, Job.DONE, None)


def _finish(job, status: str, error) -> None:
    if job is None:
        return
    job.status = status
    job.error = error
    job.finished_at = datetime.utcnow()
    db.session.commit()


def check_cancelled(job_id: int) -> None:
    """Точка проверки отмены МЕЖДУ шагами длинной задачи.

    Внутри одного обращения к модели отмена невозможна: запрос уже ушёл, ответ
    придёт и будет оплачен независимо от нашего желания. Поэтому проверка стоит
    там, где шаг естественно распадается на части, — например, у диагноста между
    триажем, доп. прогонами и вердиктами. Это ограничение честное, и интерфейс
    о нём говорит прямо.
    """
    job = db.session.get(Job, job_id)
    if job is not None and job.cancel_requested:
        raise Cancelled()


def request_cancel(job_id: int) -> bool:
    """Просит остановить задачу. Возвращает False, если останавливать нечего."""
    job = db.session.get(Job, job_id)
    if job is None or not job.is_active:
        return False
    job.cancel_requested = True
    if job.status == Job.QUEUED:
        # До работы дело не дошло — гасим сразу, ждать нечего.
        _finish(job, Job.CANCELLED, "Отменено до начала выполнения.")
    else:
        db.session.commit()
    return True


def step_title(step: str) -> str:
    return STEP_TITLES.get(step, step)


def step_hint(step: str) -> str:
    return STEP_HINTS.get(step, "")


def retry_endpoint(step: str):
    """Имя маршрута перезапуска шага. None — если перезапуска у шага нет."""
    return STEP_RETRY_ENDPOINT.get(step)
