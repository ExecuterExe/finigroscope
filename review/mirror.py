"""Агент «Понимание игры» (v3) — первый шаг ветки анализа, ДО извлечения структуры.

Три режима (полный текст роли — review/prompts/mirror.md):
  проход 1 — агент читает СЫРОЙ ТЕКСТ игры (никакого game_spec на входе — его
    ещё не существует), пересказывает игру, задаёт вопросы по «слепым пятнам», СТОП;
  проход 2 — учитывает ответы автора, отдаёт подтверждённый текст (исходный текст
    + уточнения автора) и ready_to_proceed. Может повторяться до MAX_ROUNDS раз;
  проход 3 — ВТОРОЙ ГЕЙТ: точечные вопросы по критичным пробелам, которые
    обнаружил уже Извлеченец при структурировании (`run_gate2`).

Плюс наш служебный РЕЖИМ ПРАВКИ (`revision_note`) — автор не принял собранную
структуру. Не путать со вторым гейтом: гейт открывает машина, найдя пробел,
правку открывает автор, увидев, что его поняли не так.

game_spec агент не строит ни в одном режиме — это работа агента-извлеченца.

Этот модуль не решает игровую логику — только собирает промпт, вызывает
LLMProvider и разбирает ответ на человекочитаемую часть и машинный JSON-блок.
Через LLMProvider наследуется изящная деградация: без настроенного провайдера
`run_pass` вернёт `available=False`, а не упадёт.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider


def _split_response(raw_text: str):
    """Делит ответ агента на читаемый текст и JSON-блок в конце.

    Модель обязана класть JSON последним и без пояснений вокруг — ищем сначала
    блок в ```json fence```, затем как запасной вариант — последний `{...}` от
    конца текста. Если ничего не распарсилось — human-текст возвращается
    целиком, а json = None (вызывающий код обязан на это отреагировать, не упасть).
    """
    raw_text = (raw_text or "").strip()
    m = re.search(r"```json\s*(\{.*?\})\s*```\s*$", raw_text, re.DOTALL)
    if not m:
        m = re.search(r"(\{[\s\S]*\})\s*$", raw_text)
    if not m:
        return raw_text, None
    human = raw_text[: m.start()].strip()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return raw_text, None
    return human, data


def _build_user_message(game_text: str, prior_json: dict = None, author_answer: str = None,
                        round_no: int = 1, max_rounds: int = 3,
                        revision_note: str = None) -> str:
    """Текст игры как есть — v2 работает с прозой напрямую, никакого game_spec."""
    parts = [
        "=== ТЕКСТ ИГРЫ (по разделам, как в документе) ===",
        game_text or "(текст игры не передан)",
    ]

    # Режим правки: автор увидел собранную структуру и сказал, что в ней не так.
    # Это НЕ новый раунд вопросов — агент молча учитывает замечание и закрывает
    # сверку, чтобы цикл остался конечным (у правки свой бюджет, ровно одна).
    if revision_note is not None:
        parts += [
            "",
            "=== ТВОЁ ПРЕДЫДУЩЕЕ ПОДТВЕРЖДЁННОЕ ПОНИМАНИЕ ===",
            json.dumps(prior_json or {}, ensure_ascii=False, indent=2),
            "",
            "=== ЗАМЕЧАНИЕ АВТОРА К СОБРАННОЙ СТРУКТУРЕ ===",
            revision_note,
            "",
            "Это РЕЖИМ ПРАВКИ, а не новый раунд вопросов. Автор посмотрел, как его "
            "игра выглядит в виде структуры, и указал, что понято неверно. "
            "Задавать новые вопросы ЗАПРЕЩЕНО. Учти замечание как уточнение автора "
            "(оно приоритетнее исходного текста), пересобери подтверждённый текст и "
            "сразу заверши: phase=confirmed, ready_to_proceed=true. Нераскрытое "
            "оставь в still_open. В человекочитаемой части коротко скажи, что "
            "именно поменялось после замечания.",
        ]
        return "\n".join(parts)

    if author_answer is None:
        parts += ["", f"Ответов автора пока нет — это ПРОХОД 1 (раунд {round_no} из {max_rounds})."]
    else:
        final = round_no >= max_rounds
        parts += [
            "",
            "=== ТВОЁ ЗЕРКАЛО С ПРОШЛОГО ПРОХОДА ===",
            json.dumps(prior_json or {}, ensure_ascii=False, indent=2),
            "",
            "=== ОТВЕТ АВТОРА ===",
            author_answer,
            "",
            f"Это сверка, РАУНД {round_no} из {max_rounds}.",
        ]
        if final:
            parts.append(
                "ВНИМАНИЕ: это ПОСЛЕДНИЙ раунд. Задавать новые вопросы ЗАПРЕЩЕНО. "
                "Заверши сверку: phase=confirmed, всё нераскрытое перенеси в still_open "
                "с честной причиной, ready_to_proceed=true. В человекочитаемой части "
                "прямо скажи автору, что это финальная сверка и что осталось незакрытым."
            )
        else:
            parts.append(
                "Если ответы автора закрыли не всё и остались КРИТИЧНЫЕ пробелы — можешь "
                "задать ещё вопросы (phase=mirror, ready_to_proceed=false). Если всё ясно "
                "или автор отказался уточнять — заверши (phase=confirmed, ready_to_proceed=true)."
            )
    return "\n".join(parts)


def _build_gate2_message(game_text: str, confirmed_json: dict, gaps: list,
                         ambiguities: list = None, author_answer: str = None,
                         asked_questions: list = None) -> str:
    """Сообщение прохода 3 (второй гейт).

    Ключевое здесь — ИСТОРИЯ проходов 1–2. Без неё агент неизбежно спросит то,
    на что автор уже отвечал (промпт прямо это запрещает), а автор увидит, что
    его ответы проигнорировали. Историю берём из подтверждённого JSON: там
    лежат пары вопрос-ответ (`author_clarifications`) и то, что осталось
    непокрытым (`still_open`) — включая пункты, от которых автор отказался.
    """
    confirmed_json = confirmed_json or {}
    parts = [
        "=== ТЕКСТ ИГРЫ (по разделам, как в документе) ===",
        game_text or "(текст игры не передан)",
        "",
        "=== ИСТОРИЯ ПРОХОДОВ 1-2: ЧТО УЖЕ СПРАШИВАЛИ И ЧТО АВТОР ОТВЕТИЛ ===",
        json.dumps({
            "author_clarifications": confirmed_json.get("author_clarifications") or [],
            "still_open": confirmed_json.get("still_open") or [],
        }, ensure_ascii=False, indent=2),
        "",
        "=== КРИТИЧНЫЕ ПРОБЕЛЫ ОТ ИЗВЛЕЧЕНЦА (gaps, critical=true) ===",
        json.dumps(gaps or [], ensure_ascii=False, indent=2),
        "",
        "=== НЕОДНОЗНАЧНОСТИ ОТ ИЗВЛЕЧЕНЦА (ambiguities) ===",
        json.dumps(ambiguities or [], ensure_ascii=False, indent=2),
        "",
        "Это ПРОХОД 3 — ВТОРОЙ ГЕЙТ.",
    ]

    if author_answer is None:
        parts.append(
            "Это ОБРАЩЕНИЕ А: ответа автора ещё нет, нужно сформулировать вопросы. "
            "Отбрось всё, по чему автор уже отвечал или отказался отвечать (см. историю "
            "выше) — повторный вопрос по тому же поводу запрещён. Остальное переформулируй "
            "в точечные вопросы и верни phase=gate2 с полем questions и "
            "ready_to_proceed=false. Если после фильтрации спрашивать нечего — верни "
            "phase=gate2, questions=[], ready_to_proceed=true и честно скажи автору, что "
            "вопросов нет. По всей карте понимания автора НЕ прогоняй: он её уже прошёл."
        )
    else:
        parts += [
            "",
            "=== ТОЧЕЧНЫЕ ВОПРОСЫ, КОТОРЫЕ ТЫ ЗАДАЛ ===",
            json.dumps(asked_questions or [], ensure_ascii=False, indent=2),
            "",
            "=== ОТВЕТ АВТОРА ===",
            author_answer,
            "",
            "Это ОБРАЩЕНИЕ Б: разбери ответ автора. Верни phase=gate2 с полями resolved "
            "(закрытые пробелы — обязательно с тем же field, что был в gaps), still_open "
            "(что осталось, с причиной и флагом critical), simulation_blocking (блокирует "
            "ли оставшееся симуляцию) и ready_to_proceed=true. Новых вопросов не задавай: "
            "второй гейт однократный. В человекочитаемой части скажи, что удалось закрыть.",
        ]
    return "\n".join(parts)


def run_gate2(game_text: str, confirmed_json: dict, gaps: list,
              ambiguities: list = None, author_answer: str = None,
              asked_questions: list = None,
              provider_name: str = None, cache_dir: str = None) -> dict:
    """Проход 3 — второй гейт по критичным пробелам Извлеченца.

    Одно обращение: без `author_answer` — формулирует точечные вопросы,
    с `author_answer` — разбирает ответ (resolved / still_open / simulation_blocking).

    Возвращает {available, text, json, raw, provider} либо {available: False, error}.
    """
    system = prompts.load_mirror_prompt()
    user = _build_gate2_message(game_text, confirmed_json, gaps, ambiguities,
                                author_answer, asked_questions)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    human_text, data = _split_response(resp.text)
    return {
        "available": True,
        "text": human_text,
        "json": data,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }


def run_pass(game_text: str, prior_json: dict = None, author_answer: str = None,
             provider_name: str = None, cache_dir: str = None,
             round_no: int = 1, max_rounds: int = 3,
             revision_note: str = None) -> dict:
    """Один вызов агента: проход 1, очередная сверка (раунд N) или режим правки.

    Возвращает {available, text, json, raw, provider} либо {available: False, error}.
    """
    system = prompts.load_mirror_prompt()
    user = _build_user_message(game_text, prior_json, author_answer,
                               round_no, max_rounds, revision_note)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    human_text, data = _split_response(resp.text)
    return {
        "available": True,
        "text": human_text,
        "json": data,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
