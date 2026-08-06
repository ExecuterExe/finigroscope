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

# ДВЕ ПОЛОСЫ, а не один пул на всех, и это не запас прочности.
#
# Через эту очередь идут две работы с несопоставимой длительностью: оценка
# модуля по линзам укладывается в свой бюджет (lens_evaluator.TOTAL_BUDGET =
# 300 с), а итоговый разбор игры — это до трёх кругов по шесть агентов, то есть
# десятки минут. Пока пул был общим и двухместным, двух запущенных разборов
# хватало, чтобы КАЖДАЯ следующая оценка модуля встала в очередь намертво.
#
# Снаружи это выглядело хуже всего: генератор ждёт свои 360 секунд, не
# дожидается и сообщает «оценка не уложилась» — при полностью исправном
# ФинИгроСкопе, который в этот момент занят совсем другой работой. Ни по
# сообщению, ни по журналу связать одно с другим нельзя.
#
# Полосы не делят места между собой: короткая работа не ждёт длинную никогда.
SHORT = "short"
LONG = "long"

LANE_WORKERS = {
    # Оценки модулей: их заказывает конвейер генератора, они частые и короткие.
    SHORT: 2,
    # Итоговые разборы: редкие, очень долгие. Одного места достаточно — второй
    # разбор всё равно упрётся в лимиты провайдера, а очередь станет честной.
    LONG: 1,
}

# Прежнее имя оставлено: на него смотрят проверки и внешний код.
MAX_WORKERS = LANE_WORKERS[SHORT]

# Сколько держать завершённую задачу, прежде чем забыть. Генератор забирает
# результат сразу после готовности; полчаса — запас на перезагрузку его вкладки.
TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_jobs = {}
_pools = {}

# Порядковый номер заявки. Считать очередь по времени создания нельзя: на
# Windows шаг системных часов около 15 мс, и две заявки подряд получают
# ОДИНАКОВУЮ метку — «сколько впереди» превращалось в ноль там, где впереди
# была чужая работа. Счётчик к тому же не боится перевода часов.
_sequence = 0


def _ensure_pool(lane):
    if lane not in _pools:
        _pools[lane] = ThreadPoolExecutor(
            max_workers=LANE_WORKERS[lane],
            thread_name_prefix="lens-%s" % lane)
    return _pools[lane]


def _forget_old(now=None):
    """Убирает завершённые задачи, за которыми не пришли. Держит очередь малой."""
    now = now or time.time()
    for job_id in [j for j, rec in _jobs.items()
                   if rec["status"] in (DONE, FAILED)
                   and rec.get("finished_at")
                   and now - rec["finished_at"] > TTL_SECONDS]:
        _jobs.pop(job_id, None)


class Progress(object):
    """То, через что задача рассказывает о себе, пока идёт.

    Понадобилось не сразу. Оценка по линзам — ОДИН шаг, и «идёт» про неё
    исчерпывающий ответ. Разбор игры целиком — шесть шагов, повторяемых до трёх
    кругов; «идёт» про него бесполезно: спрашивающий не знает, дошло ли дело до
    диагноста или всё ещё считаются партии, и через пять минут не может отличить
    работу от зависания.

    Передаётся в саму работу отдельным объектом, а не через глобальную ссылку:
    так цепочку можно прогнать в проверке с заглушкой и увидеть ровно ту
    последовательность шагов, которую она сообщает.
    """

    def __init__(self, job_id):
        self.job_id = job_id

    def say(self, step, detail=None):
        with _lock:
            record = _jobs.get(self.job_id)
            if record is None:
                return
            record["step"] = step
            record["detail"] = detail
            record["step_started"] = time.time()

    def check_cancelled(self):
        """Отмены здесь нет, и делать вид, что есть, не надо.

        Работа идёт по запросу соседнего сервиса, а не человека за экраном:
        отменять некому и незачем. Метод существует, чтобы цепочка не знала,
        в какой очереди её запустили, — у неё один и тот же контракт прогресса.
        """
        return None


def submit(fn, lane=SHORT) -> str:
    """Ставит задачу в очередь и сразу возвращает её идентификатор.

    `fn` — функция ОДНОГО аргумента: ей передаётся Progress собственной задачи.
    Коротким задачам он не нужен (принимают и игнорируют), длинным — необходим.

    `lane` — какого рода работа. SHORT (по умолчанию) — оценка модуля, LONG —
    итоговый разбор игры. Полосы не делят места: см. пояснение к LANE_WORKERS.
    """
    if lane not in LANE_WORKERS:
        raise ValueError("Неизвестная полоса очереди: %r" % (lane,))

    global _sequence
    job_id = uuid.uuid4().hex[:16]
    with _lock:
        _forget_old()
        _sequence += 1
        _jobs[job_id] = {"status": QUEUED, "result": None, "error": None,
                         "lane": lane, "seq": _sequence,
                         "step": "в очереди", "detail": None,
                         "created_at": time.time(), "step_started": time.time(),
                         "finished_at": None}
    _ensure_pool(lane).submit(_execute, job_id, fn)
    return job_id


def waiting_ahead(job_id):
    """Сколько задач ЭТОЙ ЖЕ полосы встали в очередь раньше и ещё не закончили.

    Нужно спрашивающему: «в очереди» без числа не отличить от «висит». Пока
    полоса была общей, именно этой цифры и не хватало, чтобы понять, что оценка
    не идёт не из-за сбоя, а потому что впереди чужая долгая работа.
    """
    with _lock:
        record = _jobs.get(job_id)
        if record is None or record["status"] != QUEUED:
            return 0
        return sum(1 for other in _jobs.values()
                   if other is not record
                   and other["lane"] == record["lane"]
                   and other["status"] in (QUEUED, RUNNING)
                   and other["seq"] < record["seq"])


# Как назвать работу в сообщении об ошибке. Через очередь идут две разные вещи,
# и «Оценка по линзам не выполнена» на упавшем разборе игры — прямая ложь.
LANE_TITLES = {SHORT: "Оценка по линзам", LONG: "Итоговый разбор"}


def describe_failure(failure, lane=SHORT):
    """Что отдать наружу вместо упавшей задачи.

    По умолчанию — только ТИП ошибки: в тексте случайного исключения может
    оказаться кусок промпта, путь на диске или настройка.

    Но часть ошибок пишется ДЛЯ ЧЕЛОВЕКА и помечает себя `user_facing = True`
    (HeadlessError, а на стороне генератора — PipelineError и LensError). Их
    текст обязан дойти целиком. Пока метка здесь не читалась, объяснение
    «Прогон скелета не разрешён… SIM_API_ALLOW_RUN=1 в окружении» подменялось
    строкой «не выполнена: HeadlessError» — то есть единственная подсказка, как
    это починить, до автора не доезжала.

    Основание ровно одно — метка. Догадываться по виду строки нельзя:
    RuntimeError("секрет-из-промпта") тоже короток и однострочен.
    """
    text = str(failure).strip()
    if getattr(failure, "user_facing", False) and text:
        return text
    return "%s не выполнен%s: %s" % (
        LANE_TITLES.get(lane, LANE_TITLES[SHORT]),
        "" if lane == LONG else "а",
        type(failure).__name__)


def _execute(job_id: str, fn):
    with _lock:
        record = _jobs.get(job_id)
        if record is None:          # успели забыть — считать незачем
            return
        record["status"] = RUNNING
        lane = record["lane"]

    # В консоль — потому что оценка идёт минутами, и без этих двух строк
    # непонятно, работает сервис или завис. Ровно этот вопрос и возник на
    # первом живом прогоне.
    started = time.time()
    print("[линзы] %s: начал" % job_id, flush=True)

    try:
        result = fn(Progress(job_id))
        status, error = DONE, None
        print("[линзы] %s: готово за %.0f с" % (job_id, time.time() - started),
              flush=True)
    except Exception as failure:                     # noqa: BLE001
        print("[линзы] %s: упал за %.0f с — %s"
              % (job_id, time.time() - started, type(failure).__name__), flush=True)
        traceback.print_exc()
        result, status = None, FAILED
        error = describe_failure(failure, lane)

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
               "lane": record["lane"],
               "step": record.get("step"), "detail": record.get("detail"),
               "elapsed": round(time.time() - record["created_at"], 1),
               "error": record["error"]}
        if record["status"] == QUEUED:
            # Сколько чужой работы впереди. Без этого числа «в очереди» и
            # «висит» для спрашивающего выглядят одинаково.
            out["waiting_ahead"] = sum(
                1 for other in _jobs.values()
                if other is not record and other["lane"] == record["lane"]
                and other["status"] in (QUEUED, RUNNING)
                and other["seq"] < record["seq"])
        if record["status"] == DONE:
            out["result"] = record["result"]
        return out


def _reset_for_tests():
    """Очистка состояния между проверками — очередь модульная и переживает импорт."""
    with _lock:
        _jobs.clear()
