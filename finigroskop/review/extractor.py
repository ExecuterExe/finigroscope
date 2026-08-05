"""Агент «Извлеченец» (v3) — второй шаг ветки анализа, сразу после гейта понимания.

Принимает ПОДТВЕРЖДЁННЫЙ материал от агента «Понимание игры» (original_text +
author_clarifications + still_open + map со статусами узлов) и раскладывает его
в ДВА контура (полный текст роли — review/prompts/extractor.md):

  game_spec (core + text) — ЗАКРЫТЫЙ контракт, общий с веткой генерации игр;
  diagnostic_meta         — приватная надстройка ФинИгроСкопа для симуляции и
                            диагностики, лежит РЯДОМ с game_spec, а не внутри.

Ключевая дисциплина агента — НЕ ДОЗАПОЛНЯТЬ: чего нет в тексте и уточнениях,
остаётся null и попадает в gaps.

Почему здесь есть валидация, а не только вызов модели: промпт прямо предупреждает,
что три нарушения контракта ломаются БЕСШУМНО — рассинхрон ключей
actions_resolution обнуляет классификацию (субъективная механика поедет как
числовая), diagnostic_meta внутри game_spec не увидит ни один потребитель, а
сокращённый full_rules выглядит эквивалентным оригиналу. Молчаливую поломку
обязан ловить код, поэтому `run()` прогоняет ту же самопроверку, что описана в
промпте, и возвращает список найденного в `issues`.
"""

import json
import re

from review import prompts
from review.llm_provider import get_provider

# --- закрытый контракт game_spec (правило 2 промпта) ------------------------
# Набор полей зафиксирован и разделяется с веткой генерации игр: любое лишнее
# поле здесь — нарушение, о котором нужно сообщить, а не «полезное дополнение».
CORE_FIELDS = (
    "players", "mode", "elimination", "turn", "randomness", "resources",
    "win_condition", "loss_condition", "catch_up", "play_time", "limits",
)
TEXT_FIELDS = ("concept", "setting", "full_rules", "components", "recommendations")

# Поля надстройки: если модель какое-то не заполнила, подставляем нейтральную
# заготовку — потребители (симулятор, диагност) читают их без проверок наличия.
DIAGNOSTIC_DEFAULTS = {
    "win_paths": {"multiple": False, "paths": [], "source": None},
    "tie_breaker": {"applicable": None, "present": None, "description": None},
    "actions_resolution": {},
    "resource_roles": {},
    "targeted_actions": {"exists": None, "actions": []},
    "initial_deal": {"random": None, "what": None, "identical_start": None},
    "hand_exists": None,
    "strict_player_count": {"strict": None, "declared": None},
    "difficulty_modes": {"exists": None, "modes": []},
    "subjective_notes": None,
}
RESOLUTION_KINDS = ("deterministic", "probabilistic", "subjective_judgment")


def _extract_json(raw_text: str):
    """Достаёт JSON из ответа. Извлеченец обязан вернуть ТОЛЬКО JSON, но модели
    иногда всё же оборачивают его в ```-блок или добавляют текст вокруг."""
    raw_text = (raw_text or "").strip()
    for candidate in (
        re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_text),
        re.search(r"(\{[\s\S]*\})", raw_text),
    ):
        if candidate:
            try:
                return json.loads(candidate.group(1))
            except json.JSONDecodeError:
                continue
    return None


def build_confirmed_input(mirror_json: dict, game_text: str, author_answer: str = None,
                          node_map: list = None) -> dict:
    """Собирает вход извлеченца из результата гейта понимания.

    Если агент понимания вернул корректный JSON прохода «confirmed» — берём его
    поля как есть. Если нет (модель не отдала JSON, или сверку завершили
    принудительно по лимиту раундов) — честно восстанавливаем минимум из того,
    что есть: текст игры и последний ответ автора. Пустой still_open в этом
    случае корректен: непокрытые пробелы просто не были формализованы, и
    извлеченец найдёт их сам при структурировании.
    """
    mirror_json = mirror_json or {}
    return {
        "original_text": mirror_json.get("original_text") or game_text or "",
        "author_clarifications": mirror_json.get("author_clarifications")
        or ([{"question_id": None, "question": None, "answer": author_answer}] if author_answer else []),
        "still_open": mirror_json.get("still_open") or [],
        # Карта узлов (v3): по ней извлеченец отличает absent («механики нет» —
        # явное отрицание в diagnostic_meta) от missing («не описано» — пробел в
        # gaps). Без неё агент вынужден заново угадывать это различие, и ложный
        # absent бесшумно закроет тест, который нужно было выполнить.
        # На проходе 2 карты в JSON уже нет (она была на проходе 1), поэтому
        # оркестратор передаёт её отдельно — см. app._run_extraction.
        "map": node_map or mirror_json.get("map") or [],
    }


def absent_nodes(confirmed: dict) -> list:
    """Названия узлов, которые гейт пометил статусом absent."""
    return [n.get("node") for n in (confirmed or {}).get("map") or []
            if n.get("status") == "absent" and n.get("node")]


def critical_gaps(spec_root: dict) -> list:
    """Критичные пробелы из результата извлеченца — вход второго гейта.

    Второй гейт запускается ТОЛЬКО при их наличии: некритичные пробелы поднимать
    не нужно, это трата терпения автора (правило прохода 3 в промпте понимателя).
    """
    return [g for g in ((spec_root or {}).get("gaps") or []) if g.get("critical")]


def ambiguities(spec_root: dict) -> list:
    """Неоднозначности из результата извлеченца — идут во второй гейт вместе с gaps."""
    return list((spec_root or {}).get("ambiguities") or [])


def merge_gate2_answers(confirmed: dict, gate2_json: dict, author_answer: str) -> dict:
    """Добавляет ответы второго гейта в подтверждённый материал.

    Зачем именно так: ответы гейта — это полноправные уточнения автора, и они
    обязаны попасть в ПОДТВЕРЖДЁННЫЙ ТЕКСТ, а не остаться сбоку. Иначе повторный
    прогон извлеченца увидит тот же неполный материал и воспроизведёт тот же
    пробел. `field` из resolved сохраняем в тексте уточнения — по нему видно,
    какой именно пробел закрывался.
    """
    merged = dict(confirmed or {})
    clarifications = list(merged.get("author_clarifications") or [])

    resolved = (gate2_json or {}).get("resolved") or []
    for item in resolved:
        field = item.get("field")
        answer = item.get("answer")
        if not answer:
            continue
        clarifications.append({
            "question_id": None,
            "question": f"[второй гейт] пробел: {field}" if field else "[второй гейт]",
            "answer": answer,
        })

    # Если агент не разложил ответ по полям — не теряем сам ответ автора.
    if not resolved and (author_answer or "").strip():
        clarifications.append({
            "question_id": None,
            "question": "[второй гейт] ответ автора на точечные вопросы",
            "answer": author_answer.strip(),
        })

    merged["author_clarifications"] = clarifications
    still_open = (gate2_json or {}).get("still_open")
    if still_open is not None:
        merged["still_open"] = still_open
    return merged


def _build_user_message(confirmed: dict, doc_type: str = None) -> str:
    parts = [
        "=== ORIGINAL_TEXT (подтверждённый текст игры) ===",
        confirmed.get("original_text") or "(текст не передан)",
        "",
        "=== AUTHOR_CLARIFICATIONS (уточнения автора, приоритетнее текста) ===",
        json.dumps(confirmed.get("author_clarifications") or [], ensure_ascii=False, indent=2),
        "",
        "=== STILL_OPEN (непокрытые пробелы с шага понимания — переносить в gaps как есть) ===",
        json.dumps(confirmed.get("still_open") or [], ensure_ascii=False, indent=2),
        "",
        "=== MAP (карта понимания от гейта: статусы узлов) ===",
        json.dumps(confirmed.get("map") or [], ensure_ascii=False, indent=2),
    ]
    absent = absent_nodes(confirmed)
    if absent:
        # Дублируем список absent явным текстом: это единственное различие,
        # ошибка в котором не видна ни в одном отчёте (absent → честное n/a,
        # missing → непроверенная дыра, а выглядят они одинаково).
        parts += [
            "",
            "ВНИМАНИЕ: узлы со статусом absent (механики в игре НЕТ — установленный "
            "факт, НЕ пробел): " + ", ".join(absent) + ". "
            "Каждый из них обязан стать явным отрицанием в diagnostic_meta "
            "(present/exists = false, пустой список) и НЕ должен попасть в gaps.",
        ]
    if doc_type:
        hint = {"essay": "essay", "card": "card"}.get(doc_type, "rules")
        parts += ["", f"Источник материала: {hint} (используй как source_format)."]
    return "\n".join(parts)


# --- самопроверка контракта (промпт: эти поломки БЕСШУМНЫ) -------------------
def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


def validate(spec_root: dict, confirmed: dict = None, canonical_text: str = None) -> tuple:
    """Прогоняет самопроверку из промпта по факту и починяет то, что можно.

    Возвращает `(нормализованный_root, issues)`.

    Что ЧИНИМ автоматически (потому что верное значение однозначно известно):
      • diagnostic_meta, ошибочно вложенный в game_spec — это те же данные не на
        своём месте, и на своём месте их не увидит ни один потребитель;
      • win_condition списком вместо объекта — контракт требует объект;
      • original_text, продублированный в выходе — промпт это прямо запрещает,
        канонический текст несёт оркестратор;
      • full_rules — переписываем каноническим текстом (см. ниже).

    Что только СООБЩАЕМ, не трогая (верного значения кодом не вывести, а
    самодеятельность была бы той же выдумкой, против которой весь промпт):
      • рассинхрон ключей actions_resolution с core.turn.actions;
      • ресурсы без роли в resource_roles;
      • узлы absent, попавшие в gaps;
      • лишние поля сверх закрытого контракта.
    """
    issues = []
    root = dict(spec_root or {})
    spec = dict(root.get("game_spec") or {})
    core = dict(spec.get("core") or {})
    text = dict(spec.get("text") or {})

    # 1) diagnostic_meta обязан лежать РЯДОМ с game_spec, а не внутри него.
    meta = root.get("diagnostic_meta")
    nested = spec.pop("diagnostic_meta", None) or core.pop("diagnostic_meta", None)
    if nested and not meta:
        meta = nested
        issues.append(_issue(
            "meta_nested",
            "diagnostic_meta лежал внутри game_spec — перенесён на верхний уровень. "
            "Внутри контракта его не читает ни один потребитель."))
    elif nested:
        issues.append(_issue(
            "meta_duplicated",
            "diagnostic_meta встретился и внутри game_spec, и на верхнем уровне — "
            "вложенная копия отброшена."))
    if not meta:
        issues.append(_issue(
            "meta_missing",
            "Агент не вернул diagnostic_meta — вся диагностическая надстройка пуста "
            "(классификация действий, роли ресурсов, тай-брейк). Поля заполнены "
            "нейтральными заготовками.", "error"))
        meta = {}
    meta = {**DIAGNOSTIC_DEFAULTS, **{k: v for k, v in (meta or {}).items()}}

    # 2) win_condition — объект, даже когда путей к победе несколько.
    wc = core.get("win_condition")
    if isinstance(wc, list):
        core["win_condition"] = wc[0] if wc else None
        issues.append(_issue(
            "win_condition_list",
            f"win_condition пришёл списком из {len(wc)} элементов — оставлен первый. "
            "Несколько путей к победе описываются в diagnostic_meta.win_paths."))

    # 3) закрытый контракт: лишние поля.
    for extra in sorted(set(core) - set(CORE_FIELDS)):
        issues.append(_issue(
            "core_extra_field",
            f"В core лишнее поле «{extra}» — контракт закрыт, место таких фактов "
            "в diagnostic_meta."))
    for extra in sorted(set(text) - set(TEXT_FIELDS)):
        issues.append(_issue(
            "text_extra_field",
            f"В text лишнее поле «{extra}» — контракт закрыт."))

    # 4) actions_resolution ↔ core.turn.actions: рассинхрон бесшумно обнуляет
    #    классификацию, и субъективная механика поедет как числовая.
    actions = list((core.get("turn") or {}).get("actions") or [])
    resolution = dict(meta.get("actions_resolution") or {})
    missing_keys = [a for a in actions if a not in resolution]
    extra_keys = [k for k in resolution if k not in actions]
    if missing_keys:
        issues.append(_issue(
            "resolution_missing",
            "Действия без классификации в actions_resolution: "
            + ", ".join(missing_keys)
            + ". Они будут промоделированы как обычные числовые, даже если на самом "
              "деле субъективные.", "error"))
    if extra_keys:
        issues.append(_issue(
            "resolution_extra",
            "В actions_resolution классифицированы действия, которых нет в "
            "core.turn.actions: " + ", ".join(extra_keys)
            + ". Имена должны совпадать посимвольно."))
    bad_kinds = sorted({v for v in resolution.values() if v not in RESOLUTION_KINDS})
    if bad_kinds:
        issues.append(_issue(
            "resolution_unknown_kind",
            "Неизвестные типы разрешения действий: " + ", ".join(map(str, bad_kinds))
            + f". Допустимы только {', '.join(RESOLUTION_KINDS)}."))

    # 5) каждый ресурс должен получить роль (иначе хуки снимков считают не то).
    roles = dict(meta.get("resource_roles") or {})
    res_names = [r.get("name") for r in (core.get("resources") or []) if r.get("name")]
    no_role = [n for n in res_names if n not in roles]
    if no_role:
        issues.append(_issue(
            "resource_role_missing",
            "Ресурсы без роли в resource_roles: " + ", ".join(no_role)
            + ". Без роли метрика победы и тратимые ресурсы сливаются в одно."))

    # 6) absent ≠ missing: узел, помеченный гейтом как absent, не может быть пробелом.
    absent = [a.lower() for a in absent_nodes(confirmed)]
    if absent:
        for gap in root.get("gaps") or []:
            haystack = f"{gap.get('field') or ''} {gap.get('missing') or ''}".lower()
            hit = next((a for a in absent if a and a in haystack), None)
            if hit:
                issues.append(_issue(
                    "absent_in_gaps",
                    f"Пробел «{gap.get('field')}» относится к узлу «{hit}», который гейт "
                    "пометил как absent (механики нет). Это превращает установленный "
                    "факт в непроверенный тест."))

    # 7) канонический текст. Промпт требует full_rules ДОСЛОВНО и одновременно
    #    предупреждает, что модель, переписывая длинный текст, незаметно его
    #    сокращает. Копию делает код — тогда дрейфа нет по построению.
    if canonical_text:
        agent_rules = (text.get("full_rules") or "").strip()
        if not agent_rules:
            text["full_rules"] = canonical_text
            issues.append(_issue(
                "full_rules_filled",
                "full_rules не был заполнен — подставлен канонический текст игры."))
        elif len(agent_rules) < len(canonical_text) * 0.9:
            text["full_rules"] = canonical_text
            issues.append(_issue(
                "full_rules_shortened",
                f"full_rules был короче оригинала ({len(agent_rules)} против "
                f"{len(canonical_text)} знаков) — заменён каноническим текстом. "
                "Правила читают дальше по деталям, которые легко принять за воду."))

    # 8) original_text в выходе агента не нужен — его несёт оркестратор.
    for holder, label in ((text, "text"), (core, "core"), (root, "верхнем уровне")):
        if "original_text" in holder:
            holder.pop("original_text", None)
            issues.append(_issue(
                "original_text_duplicated",
                f"original_text продублирован в выходе агента ({label}) — удалён. "
                "Канонический текст переносит код, а не модель."))

    spec["core"] = core
    spec["text"] = text
    root["game_spec"] = spec
    root["diagnostic_meta"] = meta
    root.setdefault("gaps", [])
    root.setdefault("ambiguities", [])
    return root, issues


def subjective_actions(spec_root: dict) -> list:
    """Действия с исходом «по суждению игроков» — они кодом не моделируются.

    По их наличию дальше решается вопрос о нарративном плейтесте, поэтому
    показываем их автору отдельно, а не прячем в общем JSON.
    """
    resolution = ((spec_root or {}).get("diagnostic_meta") or {}).get("actions_resolution") or {}
    return sorted(k for k, v in resolution.items() if v == "subjective_judgment")


def run(confirmed: dict, doc_type: str = None,
        provider_name: str = None, cache_dir: str = None,
        canonical_text: str = None) -> dict:
    """Один вызов извлеченца с последующей самопроверкой контракта.

    Возвращает {available, spec, issues, raw, provider} либо {available: False, error}.
    `spec` — нормализованный JSON целиком (game_spec + diagnostic_meta + gaps +
    ambiguities), `issues` — найденные нарушения контракта (см. `validate`).

    `canonical_text` — текст игры, который есть у оркестратора; по нему
    восстанавливается `text.full_rules`, если модель его сократила.
    """
    system = prompts.load_extractor_prompt()
    user = _build_user_message(confirmed, doc_type)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    spec = _extract_json(resp.text)
    if spec is None:
        return {"available": False,
                "error": "Извлеченец вернул ответ, который не удалось разобрать как JSON.",
                "raw": resp.text}

    spec, issues = validate(
        spec, confirmed,
        canonical_text or (confirmed or {}).get("original_text"))

    return {
        "available": True,
        "spec": spec,
        "issues": issues,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
