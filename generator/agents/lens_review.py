# -*- coding: utf-8 -*-
"""Оценка модуля по линзам Шелла — клиент к ФинИгроСкопу.

Сам агент, промпт, шкала и веса категорий живут в ФинИгроСкопе
(finigroskop/review/lens_module.py). Здесь только транспорт: собрать запрос,
дождаться результата, внятно сообщить об ошибке.

Почему не скопировать агента сюда. Копия — это две шкалы Шелла с одинаковым
названием: правку 47 линз или весов категорий пришлось бы делать дважды, и уже
со второго раза они разойдутся. А сравнивать баллы двух сервисов надо будет
обязательно — иначе непонятно, что значит 7.412.

Почему не импортировать напрямую. У обоих проектов есть модуль верхнего уровня
`config`, и это РАЗНЫЕ модули: генератор делает `from config import config`
(объект), у ФинИгроСкопа такого объекта нет. Общий sys.path сломал бы тот
сервис, который окажется в нём вторым.

Ожидание асинхронное: линзы отвечают до пяти минут, и ФинИгроСкоп обязан
работать одним воркером — синхронный ответ занял бы его целиком.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from config import config

SUBMIT_PATH = "/api/lenses/module"

# Как часто спрашивать готовность. Секунда — компромисс: чаще значит долбить
# соседний сервис впустую, реже — держать пользователя перед готовым ответом.
POLL_INTERVAL = 1.0

# Сколько ждать ответа на сами HTTP-запросы (не на оценку целиком).
HTTP_TIMEOUT = 20


class LensError(Exception):
    """Оценка не выполнена. Текст пригоден для показа пользователю."""


def _url(path):
    return config.lens_api_url.rstrip("/") + path


def _request(url, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if config.lens_api_token:
        headers["X-Lens-Token"] = config.lens_api_token

    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise LensError(_http_error(error))
    except urllib.error.URLError as error:
        raise LensError(
            "ФинИгроСкоп недоступен по адресу %s (%s). Оценка по линзам живёт "
            "в нём; проверьте, что сервис запущен и что LENS_API_URL в .env "
            "указывает на него." % (config.lens_api_url, error.reason))
    except json.JSONDecodeError:
        raise LensError("ФинИгроСкоп вернул не JSON.")
    except TimeoutError:
        raise LensError("ФинИгроСкоп не ответил за %d с." % HTTP_TIMEOUT)


def _http_error(error):
    try:
        detail = json.loads(error.read().decode("utf-8")).get("error", "")
    except Exception:                                     # noqa: BLE001
        detail = ""
    if error.code == 403:
        return ("ФинИгроСкоп отклонил запрос (403). %s"
                % (detail or "Проверьте LENS_API_TOKEN — он должен совпадать "
                             "в .env обоих сервисов."))
    if error.code == 404:
        return ("Задача оценки не найдена (404). %s"
                % (detail or "Возможно, ФинИгроСкоп перезапускался."))
    base = "ФинИгроСкоп ответил ошибкой %d." % error.code
    return "%s %s" % (base, detail) if detail else base


def submit(phase, module, params, audit):
    """Ставит оценку в очередь и СРАЗУ возвращается, ничего не дожидаясь.

    Ждать внутри обработчика запроса нельзя: линзы отвечают до 300 с, а при
    неудачной самопроверке ответа агент делает второй заход — то есть до 600 с
    на оценку. Столько не выдержит ни один участник цепочки: nginx рвёт запрос
    на 300 с (proxy_read_timeout), а пользователь всё это время смотрит на
    неподвижную строку и не знает, работает оно или зависло.

    Возвращает:
      • {'ready': False, 'reason': ...} — линзы не звали, и это штатный исход:
        у аудитора остались критичные замечания, модуль сперва чинят;
      • {'ready': True, 'job_id': ...} — оценка принята в работу.
    """
    started = _request(_url(SUBMIT_PATH), {
        "phase": phase,
        "module": module,
        "params": params,
        "audit": audit,
    })
    if started.get("ready") and not started.get("job_id"):
        raise LensError("ФинИгроСкоп принял запрос, но не вернул номер задачи.")
    return started


def poll(job_id):
    """Состояние одной оценки. Не ждёт: отвечает тем, что есть сейчас.

    Возвращает {'status': 'queued'|'running'|'done'|'failed', ...}; у 'done'
    добавлено поле 'result'.
    """
    if not job_id:
        raise LensError("Не указан номер задачи.")
    # Экранируем: номер приходит снаружи, а http.client кодирует строку запроса
    # в ASCII и на любой букве вне латиницы бросает UnicodeEncodeError. Это не
    # ошибка ответа, а падение до отправки — обработчик обрывал бы соединение
    # вообще без ответа, и страница показывала бы «сервер не ответил».
    safe_id = urllib.parse.quote(str(job_id), safe="")
    state = _request(_url("%s/%s.json" % (SUBMIT_PATH, safe_id)))
    if state.get("status") == "failed":
        raise LensError(state.get("error") or "Оценка по линзам не выполнена.")
    return state


def evaluate(phase, module, params, audit, wait=None):
    """Заявка плюс ожидание в одном вызове.

    Нужно тем, кто работает не из браузера: проверкам и разбору вручную из
    консоли. Страница так НЕ делает — она опрашивает состояние сама, чтобы
    показывать, сколько идёт оценка.
    """
    started = submit(phase, module, params, audit)
    if not started.get("ready"):
        return started
    return _await_result(started["job_id"],
                         wait if wait is not None else config.lens_timeout)


def _await_result(job_id, wait):
    deadline = time.time() + wait

    while True:
        state = poll(job_id)
        if state.get("status") == "done":
            return state.get("result") or {}

        if time.time() >= deadline:
            raise LensError(
                "Оценка по линзам не уложилась в %d с. Она могла ещё идти: "
                "номер задачи %s, состояние можно спросить у ФинИгроСкопа "
                "повторно." % (wait, job_id))
        time.sleep(POLL_INTERVAL)
