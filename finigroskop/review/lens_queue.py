"""Очередь оценок по линзам для ВНЕШНИХ модулей (запросы «Генератора игр»).

Почему не общий jobs.py. Та очередь построена вокруг документа автора:
`Job.document_id` — NOT NULL с внешним ключом на `documents`, а результат по
устройству лежит не в задаче, а в таблице своего шага («задача — это про
идёт/готово/упало, а не про данные»). У модуля от генератора нет ни документа,
ни своей таблицы: результат уезжает по HTTP обратно и в базе ФинИгроСкопа никому
не нужен. Класть его туда пришлось бы через миграцию схемы и фиктивный документ
в хранилище пользователя — цена, несоразмерная задаче.

Почему в памяти, а не в базе. Хранить нечего: если сервис перезапустился,
генератор просто спросит заново — это дешевле любой персистентности. Единственный
процесс гарантирован тем же условием, которое и так обязательно для ФинИгроСкопа
(`gunicorn -w 1`, см. deploy/): при нескольких воркерах статус пришёл бы не от
того процесса, который считает, — ровно та же причина, по которой очередь задач
не даёт масштабировать сервис.

Зачем очередь вообще нужна. Линзы читают много и отвечают долго (LLM_TIMEOUT =
300 с). Синхронный ответ на такой запрос занял бы единственный воркер на пять
минут — ФинИгроСкоп на это время перестал бы отвечать ВСЕМ.
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

# Одновременно считаемых оценок. Двух достаточно: вызовы почти всё время ждут
# ответа модели, а не занимают процессор.
MAX_WORKERS = 2

# Сколько держать завершённую задачу, прежде чем забыть. Генератор забирает
# результат сразу после готовности; полчаса — запас на перезагрузку его вкладки.
TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_jobs = {}
_pool = None


def _ensure_pool():
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                   thread_name_prefix="lens-module")
    return _pool


def _forget_old(now=None):
    """Убирает завершённые задачи, за которыми не пришли. Держит очередь малой."""
    now = now or time.time()
    for job_id in [j for j, rec in _jobs.items()
                   if rec["status"] in (DONE, FAILED)
                   and rec.get("finished_at")
                   and now - rec["finished_at"] > TTL_SECONDS]:
        _jobs.pop(job_id, None)


def submit(fn) -> str:
    """Ставит оценку в очередь и сразу возвращает её идентификатор.

    `fn` — функция без аргументов, возвращающая словарь результата.
    """
    job_id = uuid.uuid4().hex[:16]
    with _lock:
        _forget_old()
        _jobs[job_id] = {"status": QUEUED, "result": None, "error": None,
                         "created_at": time.time(), "finished_at": None}
    _ensure_pool().submit(_execute, job_id, fn)
    return job_id


def _execute(job_id: str, fn):
    with _lock:
        record = _jobs.get(job_id)
        if record is None:          # успели забыть — считать незачем
            return
        record["status"] = RUNNING

    # В консоль — потому что оценка идёт минутами, и без этих двух строк
    # непонятно, работает сервис или завис. Ровно этот вопрос и возник на
    # первом живом прогоне.
    started = time.time()
    print("[линзы] %s: начал" % job_id, flush=True)

    try:
        result = fn()
        status, error = DONE, None
        print("[линзы] %s: готово за %.0f с" % (job_id, time.time() - started),
              flush=True)
    except Exception as failure:                     # noqa: BLE001
        print("[линзы] %s: упал за %.0f с — %s"
              % (job_id, time.time() - started, type(failure).__name__), flush=True)
        # Текст исключения наружу не отдаём: в нём может оказаться кусок
        # промпта или настроек. В журнал — полностью, наружу — коротко.
        traceback.print_exc()
        result, status = None, FAILED
        error = "Оценка по линзам не выполнена: %s" % type(failure).__name__

    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return
        record.update(status=status, result=result, error=error,
                      finished_at=time.time())


def status(job_id: str):
    """Состояние задачи или None, если такой нет (или её уже забыли)."""
    with _lock:
        record = _jobs.get(job_id)
        if record is None:
            return None
        out = {"job_id": job_id, "status": record["status"],
               "error": record["error"]}
        if record["status"] == DONE:
            out["result"] = record["result"]
        return out


def _reset_for_tests():
    """Очистка состояния между проверками — очередь модульная и переживает импорт."""
    with _lock:
        _jobs.clear()
