# -*- coding: utf-8 -*-
"""Долгие задачи генератора: очередь в памяти с отчётом о ходе работы.

Зачем. Полный проход «сгенерировать — проверить аудитором — оценить по линзам»
делает до девяти обращений к моделям и идёт минутами. Держать на нём HTTP-запрос
нельзя: nginx рвёт запрос на 300 с, а пользователь всё это время смотрит на
неподвижную строку. Ровно на этом уже обожглись с оценкой по линзам.

Отличие от очереди ФинИгроСкопа (finigroskop/review/lens_queue.py) одно, но
важное: здесь задача СООБЩАЕТ О ХОДЕ РАБОТЫ. Оценка линз — один шаг, про неё
достаточно знать «идёт». Полный проход состоит из трёх шагов, повторяемых до
трёх раз, и «идёт» про него — бесполезный ответ: пользователю нужно видеть, что
сейчас происходит и какая это попытка.

Почему в памяти. Хранить нечего: перезапустился сервер — проход запускают
заново. Единственный процесс гарантирован тем, что сервер генератора и так
однопроцессный.
"""

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Проходов одновременно. Двух достаточно: они почти всё время ждут ответа
# модели, а не считают.
MAX_WORKERS = 2

# Сколько держать завершённую задачу. Страница забирает результат сразу;
# полчаса — запас на перезагрузку вкладки.
TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_jobs = {}
_pool = None


class Cancelled(Exception):
    """Проход остановлен по просьбе пользователя."""


class Progress:
    """То, через что задача рассказывает о себе.

    Передаётся в саму работу отдельным объектом, а не через глобальную ссылку:
    так функцию прохода можно прогнать в проверке, подсунув ей заглушку, и
    увидеть ровно ту последовательность шагов, которую она сообщает.
    """

    def __init__(self, job_id):
        self.job_id = job_id

    def say(self, step, detail=None, attempt=None, attempts_total=None):
        with _lock:
            record = _jobs.get(self.job_id)
            if record is None:
                return
            record["step"] = step
            record["detail"] = detail
            if attempt is not None:
                record["attempt"] = attempt
            if attempts_total is not None:
                record["attempts_total"] = attempts_total
            record["step_started"] = time.time()

    def add_attempt(self, row):
        """Итог одной попытки — виден ещё до конца прохода."""
        with _lock:
            record = _jobs.get(self.job_id)
            if record is None:
                return
            record["attempts"].append(row)

    def check_cancelled(self):
        with _lock:
            record = _jobs.get(self.job_id)
            if record is not None and record.get("cancel"):
                raise Cancelled()


def _ensure_pool():
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                   thread_name_prefix="pipeline")
    return _pool


def _forget_old(now=None):
    now = now or time.time()
    for job_id in [j for j, rec in _jobs.items()
                   if rec["status"] in (DONE, FAILED)
                   and rec.get("finished_at")
                   and now - rec["finished_at"] > TTL_SECONDS]:
        _jobs.pop(job_id, None)


def submit(fn):
    """Ставит задачу в очередь. `fn` получает единственный аргумент — Progress."""
    job_id = uuid.uuid4().hex[:16]
    with _lock:
        _forget_old()
        _jobs[job_id] = {
            "status": QUEUED, "result": None, "error": None,
            "step": "в очереди", "detail": None,
            "attempt": 0, "attempts_total": None, "attempts": [],
            "created_at": time.time(), "step_started": time.time(),
            "finished_at": None, "cancel": False,
        }
    _ensure_pool().submit(_execute, job_id, fn)
    return job_id


def describe_failure(failure):
    """Что показать пользователю вместо упавшей задачи.

    По умолчанию наружу уходит только ТИП ошибки: в тексте случайного
    исключения может оказаться кусок промпта, путь на диске или настройка.

    Но у части ошибок текст и написан для человека — их классы помечают себя
    `user_facing = True` (agents/pipeline.py, agents/lens_review.py). Раньше
    метки не было, и сообщение «Ни одна из трёх попыток не дошла до оценки,
    причины: …» превращалось в «Проход не выполнен: PipelineError». Причина
    была вычислена, записана в исключение — и выброшена ровно перед показом.

    Пустой текст тоже ни о чём не говорит, поэтому на него возвращаемся к типу:
    «просто ничего» в окне — худший из возможных ответов.

    Основание ровно одно — метка. Догадываться по длине или по отсутствию
    переносов нельзя: `RuntimeError("секрет-из-промпта")` короток и однострочен,
    а показывать его нельзя. Кому текст можно показать — решает тот, кто его
    писал, а не длина строки.
    """
    text = str(failure).strip()
    if getattr(failure, "user_facing", False) and text:
        return text
    return ("Проход не выполнен: %s. Подробности в окне сервера."
            % type(failure).__name__)


def _execute(job_id, fn):
    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return
        record["status"] = RUNNING

    started = time.time()
    print("[проход] %s: начал" % job_id, flush=True)

    try:
        result = fn(Progress(job_id))
        status, error = DONE, None
        print("[проход] %s: готово за %.0f с" % (job_id, time.time() - started),
              flush=True)
    except Cancelled:
        result, status = None, FAILED
        error = "Проход остановлен."
        print("[проход] %s: остановлен пользователем" % job_id, flush=True)
    except Exception as failure:                          # noqa: BLE001
        traceback.print_exc()
        result, status = None, FAILED
        error = describe_failure(failure)
        print("[проход] %s: упал за %.0f с" % (job_id, time.time() - started),
              flush=True)

    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return
        record.update(status=status, result=result, error=error,
                      finished_at=time.time(), step="готово")


def status(job_id):
    """Состояние задачи или None, если такой нет."""
    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return None
        out = {
            "job_id": job_id,
            "status": record["status"],
            "step": record["step"],
            "detail": record["detail"],
            "attempt": record["attempt"],
            "attempts_total": record["attempts_total"],
            "attempts": list(record["attempts"]),
            "elapsed": round(time.time() - record["created_at"], 1),
            # Время ТЕКУЩЕГО шага отдельно от общего. Без него страница
            # показывала общее время рядом с названием шага, и «оценка по
            # линзам 436 с» читалось как «линзы висят семь минут», хотя семь
            # минут шёл весь проход, а линзы — минуту из них.
            "step_elapsed": round(time.time() - record["step_started"], 1),
            "error": record["error"],
        }
        if record["status"] == DONE:
            out["result"] = record["result"]
        return out


def request_cancel(job_id):
    """Просьба остановиться. Сработает МЕЖДУ шагами, а не внутри вызова модели:
    запрос уже отправлен и будет оплачен независимо от нашего желания."""
    with _lock:
        record = _jobs.get(job_id)
        if record is None or record["status"] in (DONE, FAILED):
            return False
        record["cancel"] = True
        return True


def _reset_for_tests():
    with _lock:
        _jobs.clear()
