# -*- coding: utf-8 -*-
"""Обращение к модели через OpenRouter.

OpenRouter говорит на диалекте OpenAI (/chat/completions), поэтому клиент
получается тонким. Здесь только транспорт: собрать запрос, разобрать ответ,
внятно сообщить об ошибке. Логика агентов будет выше уровнем — этот модуль
про неё ничего не знает.

Заготовка под спецификацию из документа: генерация идёт на «тяжёлой» модели
(tier='pro'), проверки и аудит — на дешёвой (tier='flash').
"""

import json
import time
import urllib.request
import urllib.error

from config import config

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_PROMPT = 20000

# Сколько раз всего пробуем отправить запрос (первый заход плюс повторы).
# Повторяем только сбои, которые проходят сами собой, — см. _post.
SEND_ATTEMPTS = 3

# Пауза перед повтором, секунды. Удваивается с каждой попыткой: если провайдер
# просит подождать (429), долбить его с той же частотой бессмысленно.
RETRY_PAUSE = 2.0


class LLMError(Exception):
    """Запрос к модели не удался. Текст пригоден для показа пользователю.

    Метка `user_facing` — не украшение: по ней очередь (jobs.describe_failure)
    отличает объяснение, написанное для человека, от случайного исключения, в
    тексте которого может оказаться кусок промпта. Без метки сообщение «Не
    удалось связаться с OpenRouter: getaddrinfo failed» подменялось строкой
    «Проход не выполнен: LLMError», и единственная подсказка, что сломался
    DNS, а не сервис, до автора не доезжала.
    """

    user_facing = True


class BadAnswer(LLMError):
    """Модель ответила, но ответом пользоваться нельзя: обрыв, не JSON, мусор.

    Отдельный класс нужен вызывающему, чтобы отличить «связи нет» от «ответ
    негодный». Разница в том, что делать дальше: при отказе сети повтор
    бессмысленен и проход надо прекращать, а негодный ответ — ровно то, ради
    чего у прохода есть три попытки. Пока оба случая были одним классом, любой
    оборванный ответ убивал проход целиком, хотя следующая попытка обычно
    проходила.
    """


def _model_for(tier, thinking=False):
    """Модель для вызова.

    thinking просит рассуждающую модель. Она задаётся отдельно, потому что
    обычная чат-модель рассуждения не поддерживает и флаг на ней просто не
    сработает. Пока OPENROUTER_MODEL_REASONING в .env пуст — тихо работаем на
    обычной модели, это осознанный запасной вариант, а не ошибка.
    """
    if thinking and config.model_reasoning:
        return config.model_reasoning
    return config.model_flash if tier == "flash" else config.model_pro


def complete(messages, tier="pro", temperature=None, max_tokens=None,
             response_format=None, thinking=False, timeout=None):
    """Отправляет диалог модели и возвращает разобранный ответ.

    messages       — [{'role': 'system'|'user'|'assistant', 'content': str}]
    tier           — 'pro' для генерации, 'flash' для проверок
    response_format— например {'type': 'json_object'}, если нужен строгий JSON
    thinking       — просить рассуждающую модель, если она задана в .env
    timeout        — секунды; по умолчанию берётся из настроек

    Возвращает {'text', 'model', 'usage', 'finish_reason', 'duration'}.
    Бросает LLMError с понятной причиной.
    """
    problem = config.problem()
    if problem:
        raise LLMError(problem)

    if not messages:
        raise LLMError("Пустой запрос к модели.")

    total = sum(len(m.get("content", "")) for m in messages)
    if total > MAX_PROMPT:
        raise LLMError("Запрос слишком длинный: %d символов, предел %d."
                       % (total, MAX_PROMPT))

    model = _model_for(tier, thinking)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": config.temperature if temperature is None else temperature,
        "max_tokens": config.max_tokens if max_tokens is None else max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    if thinking and config.model_reasoning:
        payload["reasoning"] = {"enabled": True}

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer %s" % config.api_key,
            "Content-Type": "application/json; charset=utf-8",
            # необязательные заголовки OpenRouter — приложение в их рейтинге
            "HTTP-Referer": config.app_url,
            "X-Title": config.app_title,
        },
    )

    limit = config.timeout if timeout is None else timeout
    started = time.time()
    body = _post(request, limit)

    # ошибка может прийти и с кодом 200 — в теле ответа
    if isinstance(body.get("error"), dict):
        raise LLMError("OpenRouter: %s"
                       % body["error"].get("message", "неизвестная ошибка"))

    choices = body.get("choices") or []
    if not choices:
        raise LLMError("OpenRouter вернул ответ без вариантов (choices).")

    message = choices[0].get("message") or {}
    return {
        "text": message.get("content", ""),
        "model": body.get("model", payload["model"]),
        "usage": body.get("usage", {}),
        "duration": time.time() - started,
        "finish_reason": choices[0].get("finish_reason"),
    }


def _post(request, limit):
    """Отправляет запрос, повторяя сбои, которые проходят сами собой.

    Зачем это здесь. Единственный обрыв соединения («EOF occurred in violation
    of protocol») отменял всю генерацию целиком: агент делает до трёх попыток,
    но только когда модель вернула негодный ответ, — сетевая ошибка вылетала
    из цикла сразу. Пользователь видел «не удалось связаться» там, где через
    секунду всё работает.

    Повторяем: обрыв соединения и TLS, 429 (слишком часто) и 5xx (у провайдера).
    НЕ повторяем:
      • 401/402/404 и прочие 4xx — ответ будет тот же, ждать незачем;
      • таймаут — время уже потрачено, а запрос мог дойти и посчитаться:
        повтор рискует оплатить генерацию дважды.
    """
    last_error = None
    # Каким классом сдаваться в конце. Оборванное тело — «негодный ответ»
    # (BadAnswer): проход вправе потратить на него одну попытку и продолжить.
    # Отказ сети — LLMError: следующая попытка упрётся в то же самое.
    last_class = LLMError

    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=limit) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # HTTPError — подкласс URLError, поэтому ловим его первым
            if e.code != 429 and e.code < 500:
                raise LLMError(_http_error(e))
            last_error = _http_error(e)
        except urllib.error.URLError as e:
            last_error = "Не удалось связаться с OpenRouter: %s" % e.reason
            last_class = LLMError
        except TimeoutError:
            raise LLMError("OpenRouter не ответил за %d с." % limit)
        except json.JSONDecodeError as e:
            # ПОВТОРЯЕМ. Сперва этот случай был объявлен неповторяемым —
            # «провайдер ответил мусором, разбирать нечего». Рассуждение верно
            # для страницы-заглушки и неверно для главного случая: тело ответа
            # приходит ОБОРВАННЫМ на середине, когда соединение рвётся посреди
            # длинной выдачи. Это ровно такая же случайность, как обрыв связи, и
            # лечится ровно так же — вторым запросом.
            #
            # Так выглядели живые сбои: «Expecting value: line 763 column 1
            # (char 4191)» и «OpenRouter вернул не JSON» — оба на модуле
            # особенностей, у которого ответ самый длинный.
            last_error = ("OpenRouter вернул неполный или испорченный ответ (%s)"
                          % e)
            last_class = BadAnswer

        if attempt < SEND_ATTEMPTS:
            time.sleep(RETRY_PAUSE * attempt)

    raise last_class("%s Не помогли %d попытки." % (last_error, SEND_ATTEMPTS))


# На сколько увеличиваем предел, если ответ оборвался на нём. Полтора раза, а не
# вдвое: цель — дотянуть до конца уже почти готового ответа, а не разрешить
# модели писать вдвое больше.
LIMIT_GROWTH = 1.5

# Выше этого не поднимаем даже автоматически: если ответ не уложился и сюда,
# дело не в тесном лимите, а в том, что агента просят слишком о многом.
MAX_TOKENS_CEILING = 12000


def complete_json(messages, **kwargs):
    """То же самое, но ответ разбирается как JSON.

    Агентам по документу нужен строго структурированный ответ, поэтому просим
    модель о json_object и падаем с внятной ошибкой, если она не послушалась.

    ОБРЫВ НА ЛИМИТЕ ТОКЕНОВ разбирается отдельно, и это не мелочь. Модель,
    упёршаяся в max_tokens, возвращает JSON, оборванный на середине —
    `finish_reason` при этом равен "length". Раньше это поле приходило и не
    читалось никем, а наружу шло «Модель вернула не JSON»: обвинение в адрес
    модели за то, что сделали мы сами. На модуле особенностей, где ответ самый
    длинный, так падали ВСЕ три попытки подряд.

    Оборванный ответ добираем повтором с увеличенным пределом — один раз.
    Дальше честно говорим, что предел мал, и называем число: это настройка
    агента, и чинить её надо там, а не гадать.
    """
    kwargs.setdefault("response_format", {"type": "json_object"})
    result = complete(messages, **kwargs)

    truncated = result.get("finish_reason") == "length"
    if truncated:
        asked = kwargs.get("max_tokens") or config.max_tokens
        grown = min(int(asked * LIMIT_GROWTH), MAX_TOKENS_CEILING)
        if grown > asked:
            print("[модель] ответ оборван на пределе %d токенов, повторяю с %d"
                  % (asked, grown), flush=True)
            kwargs["max_tokens"] = grown
            result = complete(messages, **kwargs)
            truncated = result.get("finish_reason") == "length"
            asked = grown
        if truncated:
            raise BadAnswer(
                "Ответ модели оборван на пределе в %d токенов — это предел "
                "запроса, а не ошибка модели. Увеличьте max_tokens у этого "
                "агента." % asked)

    try:
        result["data"] = json.loads(result["text"])
    except json.JSONDecodeError as e:
        raise BadAnswer("Модель вернула не JSON (%s). Начало ответа: %s"
                       % (e, result["text"][:200]))
    return result


def _http_error(e):
    """Коды OpenRouter в человеческие формулировки."""
    try:
        detail = json.loads(e.read().decode("utf-8"))
        detail = detail.get("error", {}).get("message", "")
    except Exception:
        detail = ""

    known = {
        401: "OpenRouter отклонил ключ (401). Проверьте OPENROUTER_API_KEY в .env.",
        402: "На счету OpenRouter недостаточно средств (402).",
        404: "Модель не найдена (404). Проверьте название в .env.",
        429: "Слишком много запросов к OpenRouter (429), подождите немного.",
    }
    base = known.get(e.code, "OpenRouter ответил ошибкой %d." % e.code)
    return "%s %s" % (base, detail) if detail else base


if __name__ == "__main__":
    # быстрая проверка связи: python llm.py
    import sys

    print("Провайдер: %s" % config.provider)
    print("Модели: pro=%s, flash=%s" % (config.model_pro, config.model_flash))

    problem = config.problem()
    if problem:
        print("Не готово: %s" % problem)
        sys.exit(1)

    print("Ключ найден, пробую запрос...")
    try:
        answer = complete(
            [{"role": "user", "content": "Ответь одним словом: работает?"}],
            tier="flash", max_tokens=20)
    except LLMError as error:
        print("Ошибка: %s" % error)
        sys.exit(1)

    print("Ответ модели (%s): %s" % (answer["model"], answer["text"].strip()))
    print("Токены: %s" % answer["usage"])
