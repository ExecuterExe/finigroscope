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
    """Запрос к модели не удался. Текст пригоден для показа пользователю."""


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
        except TimeoutError:
            raise LLMError("OpenRouter не ответил за %d с." % limit)
        except json.JSONDecodeError:
            raise LLMError("OpenRouter вернул не JSON.")

        if attempt < SEND_ATTEMPTS:
            time.sleep(RETRY_PAUSE * attempt)

    raise LLMError("%s Не помогли %d попытки." % (last_error, SEND_ATTEMPTS))


def complete_json(messages, **kwargs):
    """То же самое, но ответ разбирается как JSON.

    Агентам по документу нужен строго структурированный ответ, поэтому просим
    модель о json_object и падаем с внятной ошибкой, если она не послушалась.
    """
    kwargs.setdefault("response_format", {"type": "json_object"})
    result = complete(messages, **kwargs)
    try:
        result["data"] = json.loads(result["text"])
    except json.JSONDecodeError:
        raise LLMError("Модель вернула не JSON: %s" % result["text"][:200])
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
