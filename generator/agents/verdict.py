# -*- coding: utf-8 -*-
"""Итоговый разбор упакованной игры — клиент к ФинИгроСкопу.

Последний шаг конвейера. Упаковка отдала `game_spec.json` в закрытом контракте,
и здесь он уезжает соседнему сервису, который проводит игру через весь
симуляционный этап:

    симуляционист → прогон скелета → оценщик статистик → диагност
        → линзы Шелла → синтезатор → (авто-редизайн, если балл ниже порога)

Почему клиент, а не своя реализация. Всё перечисленное уже написано и проверено
в ФинИгроСкопе: 47 линз, 50 тестов методички, веса категорий, формула балла.
Копия означала бы две шкалы с одинаковым названием, которые разойдутся со
второй правки, — а сравнивать баллы двух сервисов придётся обязательно, иначе
непонятно, что значит 7.412.

Устройство то же, что у agents/lens_review.py, и по тем же причинам: ожидание
асинхронное (цепочка идёт минутами и делает до полутора десятков обращений к
моделям), сетевой сбой повторяется один раз (сосед мог спать), ошибки ответа не
повторяются.

ЧЕГО ЭТОТ ШАГ НЕ ДЕЛАЕТ САМ. Прогон скелета выполняет код, написанный моделью.
В ФинИгроСкопе он закрыт разрешением `SIM_API_ALLOW_RUN`, выключенным по
умолчанию. Если разрешения нет, разбор честно останавливается на шаге
статистики — и здесь это видно сразу, по полю `code_execution` в ответе на
заявку, а не через минуту ожидания.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from config import config

SUBMIT_PATH = "/api/simulate/spec"
STATUS_PATH = "/api/simulate/%s.json"

# Как часто спрашивать готовность. Секунда — компромисс: чаще значит долбить
# соседний сервис впустую, реже — держать пользователя перед готовым ответом.
POLL_INTERVAL = 1.0

# Сколько ждать ответа на сами http-запросы (не на разбор целиком).
HTTP_TIMEOUT = config.lens_http_timeout

# Один повтор и только на сетевую ошибку: типовой случай — ФинИгроСкоп спал.
RETRIES_ON_NETWORK = 1
RETRY_DELAY = 5.0

# Сколько ждать разбор целиком. Он длиннее всего в конвейере: до трёх кругов, в
# каждом шесть агентов. Значение с запасом — сдаться раньше, чем сосед закончит,
# значит показать ошибку по несуществующему поводу, пока работа идёт и тратит
# деньги.
DEFAULT_WAIT = 3600


class VerdictError(Exception):
    """Разбор не выполнен. Текст пригоден для показа пользователю."""

    user_facing = True


def _url(path):
    return config.lens_api_url.rstrip("/") + path


def _reason(error):
    return getattr(error, "reason", None) or type(error).__name__


def _request(url, payload=None, retries=RETRIES_ON_NETWORK):
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
        # Ответ получен — сервис жив, повторять незачем.
        raise VerdictError(_http_error(error))
    except (urllib.error.URLError, TimeoutError) as error:
        if retries > 0:
            print("[разбор] %s не отозвался (%s), повтор через %.0f с"
                  % (config.lens_api_url, _reason(error), RETRY_DELAY), flush=True)
            time.sleep(RETRY_DELAY)
            return _request(url, payload, retries - 1)
        raise VerdictError(
            "ФинИгроСкоп недоступен по адресу %s (%s) — ждали %d с и повторили "
            "%d раз. Итоговый разбор живёт в нём; проверьте, что сервис запущен "
            "и что LENS_API_URL указывает на него. На бесплатных тарифах сервис "
            "засыпает и просыпается около минуты."
            % (config.lens_api_url, _reason(error), HTTP_TIMEOUT,
               RETRIES_ON_NETWORK))
    except json.JSONDecodeError:
        raise VerdictError("ФинИгроСкоп вернул не JSON.")


def _http_error(error):
    try:
        detail = json.loads(error.read().decode("utf-8")).get("error", "")
    except Exception:                                     # noqa: BLE001
        detail = ""
    if error.code == 403:
        return ("ФинИгроСкоп отклонил запрос (403). %s"
                % (detail or "Проверьте LENS_API_TOKEN — он должен совпадать "
                             "в настройках обоих сервисов."))
    if error.code == 400:
        return ("ФинИгроСкоп не принял спецификацию (400). %s"
                % (detail or "Ожидается результат упаковки."))
    if error.code == 404:
        return ("Задача разбора не найдена (404). %s"
                % (detail or "Возможно, ФинИгроСкоп перезапускался."))
    base = "ФинИгроСкоп ответил ошибкой %d." % error.code
    return "%s %s" % (base, detail) if detail else base


def submit(spec_root):
    """Ставит разбор в очередь и СРАЗУ возвращается, ничего не дожидаясь."""
    if not ((spec_root or {}).get("game_spec") or {}).get("core"):
        raise VerdictError("Нечего разбирать: в спецификации нет game_spec.core. "
                           "Сначала упакуйте игру.")

    started = _request(_url(SUBMIT_PATH), {"spec": spec_root})
    if not started.get("job_id"):
        raise VerdictError("ФинИгроСкоп принял запрос, но не вернул номер задачи.")
    return started


def poll(job_id):
    """Состояние разбора. Не ждёт: отвечает тем, что есть сейчас."""
    if not job_id:
        raise VerdictError("Не указан номер задачи.")
    # Экранируем: номер приходит снаружи, а http.client кодирует строку запроса
    # в ASCII и на любой букве вне латиницы падает ещё до отправки.
    safe_id = urllib.parse.quote(str(job_id), safe="")
    state = _request(_url(STATUS_PATH % safe_id))
    if state.get("status") == "failed":
        raise VerdictError(state.get("error") or "Итоговый разбор не выполнен.")
    return state


def evaluate(spec_root, progress=None, wait=DEFAULT_WAIT):
    """Заявка плюс ожидание в одном вызове.

    `progress` — необязательный отчёт о ходе работы. Шаги приходят от соседнего
    сервиса и пересказываются как свои: цепочка идёт минутами, и «идёт» про неё
    бесполезный ответ.
    """
    started = submit(spec_root)
    note = started.get("note")
    if note:
        # Разрешения на прогон кода нет. Сказать об этом надо СЕЙЧАС, а не через
        # минуту молчания: разбор всё равно остановится на шаге статистики.
        print("[разбор] %s" % note, flush=True)

    result = _await_result(started["job_id"], wait, progress)
    result["code_execution"] = started.get("code_execution")
    result["note"] = note
    return result


def _await_result(job_id, wait, progress=None):
    deadline = time.time() + wait
    last_step = None

    while True:
        state = poll(job_id)

        step = state.get("step")
        if progress is not None and step and step != last_step:
            progress.say("разбор игры: %s" % step, detail=state.get("detail"))
            last_step = step

        if state.get("status") == "done":
            return state.get("result") or {}

        if time.time() >= deadline:
            raise VerdictError(
                "Итоговый разбор не уложился в %d с. Он мог ещё идти: номер "
                "задачи %s, состояние можно спросить у ФинИгроСкопа повторно."
                % (wait, job_id))
        time.sleep(POLL_INTERVAL)
