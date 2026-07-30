"""Агент «Симуляционист» (v6) — генерация кода скелета-игры.

Третий шаг ветки анализа. Берёт `game_spec.core` + `diagnostic_meta` + канонический
текст игры + шаблон `game_skeleton.py` и заполняет блок СКЕЛЕТ так, чтобы скелет
собирал ДВЕ статистики: базовую (STATS_JSON, читает «Оценщик статистик») и
диагностическую (DIAG_JSON, читает агент-диагност).
Полный текст роли — review/prompts/simulationist.md.

Два принципиальных отличия от v3, из-за которых переписан парсер:

  • ответ состоит из ДВУХ блоков — JSON с метаданными и отдельный ```python с
    кодом. Поля `code` в JSON больше НЕТ: файл начинается с докстринга и полон
    кавычек и переносов, внутри JSON-строки одна пропущенная кавычка рвёт весь
    ответ (проверено на живом прогоне);
  • появились метаданные о ГРАНИЦАХ МОДЕЛИ (`metric_responds_immediately`,
    `coalition_expressible`, `subjective_actions`, `fixed_length` и др.). Их
    читают следующие агенты, и умолчание здесь равно дезинформации: диагност
    примет «персоны не различаются» за «игра устойчива к стилям игры».

Валидация здесь особенно плотная, потому что все нарушения этого агента
БЕСШУМНЫ: программа отрабатывает и выдаёт правдоподобные числа несуществующей
игры. Дословно из промпта: «Ошибка, которая роняет программу, — дешёвая: её
видно сразу. Дорогая ошибка — та, после которой программа работает, числа
выглядят правдоподобно, а означают не то».
"""

import ast
import json
import re

from review import prompts
from review.llm_provider import get_provider

# Закрытый словарь reason: «Оценщик статистик» читает эту строку посимвольно и
# раскладывает по ней no_winner_rate. Синоним даёт неверный вердикт, а не ошибку.
REASONS = (
    "goal_reached", "most_points", "last_standing", "all_eliminated",
    "deck_exhausted", "rounds_exhausted", "round_cap", "tie_unresolved",
)

# Девять ключей determine_result — на них держится вся статистика.
RESULT_KEYS = (
    "winner", "reason", "rounds", "total_actions", "eliminated",
    "final_scores", "action_counts", "per_seat_actions", "win_path",
)

# Функции, которые агент обязан заполнить (блок СКЕЛЕТ).
SKELETON_FUNCS = (
    "setup_game", "is_game_over", "get_legal_actions", "choose_action",
    "apply_action", "determine_result",
    "snapshot_metric", "snapshot_resources", "hand_snapshot",
    "state_signature", "clone_state",
    "choose_action_passive", "choose_action_aggressive", "choose_action_novice",
    "choose_action_expert", "choose_action_coalition",
)

# Предохранители, которые нельзя удалять и увеличивать.
GUARDS = ("MAX_ROUNDS", "EXPLOIT_MAX_DEPTH", "EXPLOIT_MAX_NODES")

# Служебные счётчики, которых не должно быть в подписи состояния (правило 17).
COUNTERS = ("total_actions", "action_counts", "per_seat_actions")

STDLIB_OK = {"random", "statistics", "json", "math", "dataclasses"}

ENGINE_MARKER = "# ДВИЖОК"

META_REQUIRED = (
    "simulatable", "player_counts", "manual_turn_order", "assumptions",
    "subjective_actions", "coalition_expressible", "metric_responds_immediately",
    "fixed_length", "end_reasons_used", "hooks_filled",
)


# --- разбор ответа -----------------------------------------------------------
def _first_json_object(text: str):
    """Первый сбалансированный {...} в тексте. Нужен именно первый: за ним идёт
    код, в котором фигурных скобок сколько угодно."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def parse_response(raw_text: str) -> tuple:
    """Делит ответ на метаданные и код. Возвращает (meta, code, parse_issues).

    Формат v6 — два блока подряд. Но модель может обернуть JSON в ```json или
    (вопреки запрету) вложить код прямо в JSON: такие случаи разбираем, а не
    отвергаем, и сообщаем о нарушении контракта отдельным замечанием.
    """
    text = (raw_text or "").strip()
    issues = []

    # Код: последний (или единственный) ```python-блок.
    code = None
    blocks = re.findall(r"```(?:python|py)\s*\n([\s\S]*?)```", text)
    if blocks:
        code = max(blocks, key=len).strip()

    # Метаданные: первый JSON-объект ДО кода.
    head = text.split("```python")[0] if "```python" in text else text
    fenced = re.search(r"```json\s*\n([\s\S]*?)```", head)
    meta = None
    if fenced:
        try:
            meta = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            meta = None
    if meta is None:
        meta = _first_json_object(head)
    if meta is None:
        meta = _first_json_object(text)

    # Запрещённый, но встречающийся вариант: код внутри JSON.
    if meta and not code and isinstance(meta.get("code"), str):
        code = meta["code"]
        issues.append(_issue(
            "code_inside_json",
            "Код пришёл внутри JSON-поля `code`, хотя промпт это прямо запрещает: "
            "файл полон кавычек и переносов, и одна пропущенная кавычка рвёт весь "
            "ответ. Разобрать удалось, но контракт нарушен."))
    if meta and "code" in meta:
        meta = {k: v for k, v in meta.items() if k != "code"}

    return meta, code, issues


def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


# --- самопроверка контракта --------------------------------------------------
def _engine_block(source: str) -> str:
    """Текст блока ДВИЖОК — от маркера до конца файла, без хвостовых пробелов."""
    idx = source.find(ENGINE_MARKER)
    if idx == -1:
        return ""
    return "\n".join(line.rstrip() for line in source[idx:].split("\n")).strip()


def _module_constants(tree) -> dict:
    """Значения простых констант верхнего уровня."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                try:
                    out[t.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    out[t.id] = None
    return out


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _reason_literals(fn) -> set:
    """Строки, которые функция кладёт в `reason` — присваиванием или в словарь."""
    found = set()
    if fn is None:
        return found
    for node in ast.walk(fn):
        # winner, reason = leaders[0], "most_points"
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                names = (tgt.elts if isinstance(tgt, ast.Tuple) else [tgt])
                vals = (node.value.elts if isinstance(node.value, ast.Tuple)
                        else [node.value])
                for nm, vl in zip(names, vals):
                    if (isinstance(nm, ast.Name) and nm.id == "reason"
                            and isinstance(vl, ast.Constant) and isinstance(vl.value, str)):
                        found.add(vl.value)
        # {"reason": "tie_unresolved", ...}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "reason"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    found.add(v.value)
    return found


def _result_keys(fn) -> set:
    """Ключи словаря, который возвращает determine_result."""
    if fn is None:
        return set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "winner" in keys:
                return keys
    return set()


def validate(meta: dict, code: str, core: dict = None, template: str = None) -> list:
    """Самопроверка из промпта, выполненная кодом. Возвращает список нарушений.

    Ничего не «чинит»: править сгенерированный код от себя значило бы
    редактировать модель игры. Задача — сделать молчаливую поломку видимой.
    """
    issues = []
    meta = meta or {}
    core = core or {}

    for field in META_REQUIRED:
        if field not in meta:
            issues.append(_issue(
                "meta_field_missing",
                f"В метаданных нет поля «{field}». Каждое из них читает следующий "
                "агент; умолчание здесь равно дезинформации.",
                "error" if field in ("simulatable", "player_counts") else "warning"))

    if not meta.get("simulatable"):
        return issues        # кода нет и быть не должно — дальше проверять нечего

    if not (code or "").strip():
        issues.append(_issue("code_missing",
                             "Игра помечена симулируемой, но код не вернулся.", "error"))
        return issues

    # 1) код обязан быть синтаксически корректным
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        issues.append(_issue("syntax_error",
                             f"Код не разбирается как Python: строка {exc.lineno}, {exc.msg}",
                             "error"))
        return issues

    consts = _module_constants(tree)

    # 2) ДВИЖОК побайтово тот же (правило 2)
    if template:
        want, got = _engine_block(template), _engine_block(code)
        if not got:
            issues.append(_issue("engine_missing",
                                 "В коде нет блока ДВИЖОК — статистика не соберётся.",
                                 "error"))
        elif want and got != want:
            issues.append(_issue(
                "engine_modified",
                "Блок ДВИЖОК отличается от шаблона. Он собирает обе статистики и "
                "держит предохранители — правки в нём ломают весь конвейер молча.",
                "error"))

    # 3) все функции скелета на месте
    missing = [f for f in SKELETON_FUNCS if _func(tree, f) is None]
    if missing:
        issues.append(_issue("skeleton_func_missing",
                             "Не заполнены функции скелета: " + ", ".join(missing), "error"))

    # 4) базовый choose_action — ровно rng.choice(legal_actions) (правило 11)
    ch = _func(tree, "choose_action")
    if ch is not None:
        body = [n for n in ch.body if not (isinstance(n, ast.Expr)
                                           and isinstance(n.value, ast.Constant))]
        ok = (len(body) == 1 and isinstance(body[0], ast.Return)
              and isinstance(body[0].value, ast.Call)
              and isinstance(body[0].value.func, ast.Attribute)
              and body[0].value.func.attr == "choice")
        if not ok:
            issues.append(_issue(
                "choose_action_smart",
                "Базовый choose_action не сводится к rng.choice(legal_actions). "
                "Базовый прогон — эталон для «Оценщика статистик»; ум живёт в персонах.",
                "error"))

    # 5) state_signature без служебных счётчиков (правило 17)
    sig = _func(tree, "state_signature")
    if sig is not None:
        used = {n.attr for n in ast.walk(sig) if isinstance(n, ast.Attribute)}
        leaked = sorted(used & set(COUNTERS))
        if leaked:
            issues.append(_issue(
                "signature_has_counters",
                "В state_signature попали служебные счётчики: " + ", ".join(leaked)
                + ". Они растут всегда — петля не найдётся никогда.", "error"))

    # 6) предохранители на месте и не увеличены
    tpl_consts = _module_constants(ast.parse(template)) if template else {}
    for g in GUARDS:
        if g not in consts:
            issues.append(_issue("guard_removed", f"Удалён предохранитель {g}.", "error"))
        elif (isinstance(consts.get(g), int) and isinstance(tpl_consts.get(g), int)
              and consts[g] > tpl_consts[g]):
            issues.append(_issue(
                "guard_increased",
                f"{g} увеличен с {tpl_consts[g]} до {consts[g]} — предохранители "
                "нельзя ослаблять.", "error"))

    # 7) только стандартная библиотека
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module.split(".")[0]]
        for m in mods:
            if m not in STDLIB_OK:
                issues.append(_issue("import_forbidden",
                                     f"Импорт «{m}» вне разрешённого списка "
                                     f"({', '.join(sorted(STDLIB_OK))}).", "error"))

    # 8) determine_result: девять ключей и reason из закрытого словаря
    dr = _func(tree, "determine_result")
    keys = _result_keys(dr)
    if keys:
        lost = [k for k in RESULT_KEYS if k not in keys]
        if lost:
            issues.append(_issue("result_keys_missing",
                                 "determine_result не возвращает: " + ", ".join(lost),
                                 "error"))
        extra = sorted(keys - set(RESULT_KEYS))
        if extra:
            issues.append(_issue("result_keys_extra",
                                 "Лишние ключи в determine_result: " + ", ".join(extra)))
    bad_reasons = sorted(_reason_literals(dr) - set(REASONS))
    if bad_reasons:
        issues.append(_issue(
            "reason_unknown",
            "Значения reason вне закрытого словаря: " + ", ".join(bad_reasons)
            + ". «Оценщик статистик» читает эту строку посимвольно: синоним даёт "
              "не ошибку, а ложный вердикт о недостижимости победы.", "error"))

    declared = [r for r in (meta.get("end_reasons_used") or []) if r not in REASONS]
    if declared:
        issues.append(_issue("end_reasons_unknown",
                             "В end_reasons_used значения вне словаря: " + ", ".join(declared)))

    # 9) метаданные не расходятся с кодом
    if "manual_turn_order" in meta and "MANUAL_TURN_ORDER" in consts:
        if bool(meta["manual_turn_order"]) != bool(consts["MANUAL_TURN_ORDER"]):
            issues.append(_issue(
                "manual_turn_order_mismatch",
                f"manual_turn_order={meta['manual_turn_order']} в метаданных, а в коде "
                f"MANUAL_TURN_ORDER={consts['MANUAL_TURN_ORDER']}.", "error"))
    if meta.get("player_counts") and consts.get("PLAYER_COUNTS"):
        if list(meta["player_counts"]) != list(consts["PLAYER_COUNTS"]):
            issues.append(_issue(
                "player_counts_mismatch",
                f"player_counts={meta['player_counts']} в метаданных, а в коде "
                f"PLAYER_COUNTS={consts['PLAYER_COUNTS']}."))

    # 10) RUN_PLAN: базовый прогон первый и с пустым конфигом
    plan = consts.get("RUN_PLAN")
    if isinstance(plan, list) and plan:
        if not (isinstance(plan[0], dict) and plan[0].get("id") == "base"):
            issues.append(_issue(
                "run_plan_base_moved",
                "Первым в RUN_PLAN должен идти прогон base — на нём держится "
                "STATS_JSON для «Оценщика статистик».", "error"))
        elif plan[0].get("config"):
            issues.append(_issue("run_plan_base_config",
                                 "У базового прогона непустой config.", "error"))
    elif plan is not None:
        issues.append(_issue("run_plan_broken", "RUN_PLAN пуст или не список.", "error"))

    # 11) субъективные действия должны быть настоящими действиями из core
    actions = list((core.get("turn") or {}).get("actions") or [])
    if actions:
        unknown = [a for a in (meta.get("subjective_actions") or []) if a not in actions]
        if unknown:
            issues.append(_issue(
                "subjective_action_unknown",
                "В subjective_actions действия, которых нет в core.turn.actions: "
                + ", ".join(unknown)))

    # 12) параметр в имени действия — три теста методички обнуляются молча
    gl = _func(tree, "get_legal_actions")
    if gl is not None:
        for node in ast.walk(gl):
            if isinstance(node, ast.JoinedStr) or (
                    isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
                issues.append(_issue(
                    "action_name_parametrized",
                    "get_legal_actions собирает имя действия из частей — похоже, "
                    "параметр попал в имя. Тогда action_share распадётся на десятки "
                    "долей, и тесты на мёртвые и доминирующие действия обнулятся.",
                    "error"))
                break

    return issues


def _build_user_message(core: dict, diagnostic_meta: dict = None,
                        canonical_text: str = None) -> str:
    """Вход агента: ядро + сайдкар + канонический текст + шаблон.

    diagnostic_meta подаётся с v6: по нему агент решает, что класть в
    snapshot_metric, а что в snapshot_resources, выразим ли сговор и нужен ли
    тай-брейк. В v3 его не было, и агент вынужден был всё это угадывать.
    """
    parts = [
        "=== GAME_SPEC.CORE (структурное ядро игры) ===",
        json.dumps(core or {}, ensure_ascii=False, indent=2),
        "",
        "=== DIAGNOSTIC_META (сайдкар от извлеченца) ===",
        json.dumps(diagnostic_meta or {}, ensure_ascii=False, indent=2),
    ]
    if canonical_text:
        parts += ["", "=== КАНОНИЧЕСКИЙ ТЕКСТ ИГРЫ (для контекста) ===", canonical_text]
    parts += [
        "",
        "=== ШАБЛОН game_skeleton.py (заполни блок СКЕЛЕТ, ДВИЖОК не трогай) ===",
        prompts.load_skeleton_template(),
        "",
        "Верни ДВА блока подряд: сначала JSON с метаданными (без поля code), "
        "сразу за ним — полный текст файла отдельным блоком ```python.",
    ]
    return "\n".join(parts)


def run(core: dict, diagnostic_meta: dict = None, canonical_text: str = None,
        provider_name: str = None, cache_dir: str = None) -> dict:
    """Один вызов симуляциониста по ядру игры.

    Возвращает при успехе:
      {available: True, simulatable: True, code, meta, issues, raw, provider};
      {available: True, simulatable: False, reason, missing, ...} — честный отказ;
    при сбое провайдера/парсинга:
      {available: False, error, raw?}.
    """
    system = prompts.load_simulationist_prompt()
    user = _build_user_message(core, diagnostic_meta, canonical_text)

    provider = get_provider(provider_name, cache_dir=cache_dir)
    resp = provider.complete(system, user)
    if not resp.available:
        return {"available": False, "error": resp.error}

    meta, code, issues = parse_response(resp.text)
    if meta is None:
        return {"available": False,
                "error": "Симуляционист вернул ответ, в котором не нашлось JSON с метаданными.",
                "raw": resp.text}

    result = {
        "available": True,
        "simulatable": bool(meta.get("simulatable")),
        "meta": meta,
        "raw": resp.text,
        "provider": resp.provider,
        "cached": resp.cached,
    }
    if not result["simulatable"]:
        result["reason"] = meta.get("reason") or "Причина не указана."
        result["missing"] = meta.get("missing") or []
        result["issues"] = issues
        return result

    if not (code or "").strip():
        return {"available": False,
                "error": "Симуляционист пометил игру симулируемой, но кода в ответе нет.",
                "raw": resp.text}

    result["code"] = code
    result["issues"] = issues + validate(meta, code, core, prompts.load_skeleton_template())
    return result
