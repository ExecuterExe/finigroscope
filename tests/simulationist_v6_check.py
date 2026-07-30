# -*- coding: utf-8 -*-
"""Проверка контракта Симуляциониста v6: два блока на выходе + самопроверка кода.

Все нарушения этого агента БЕСШУМНЫ — программа отрабатывает и выдаёт
правдоподобные числа несуществующей игры. Поэтому проверяем именно их:
подменённое значение reason, умный базовый choose_action, счётчики в подписи
состояния, поднятый предохранитель, правку блока ДВИЖОК и параметр в имени
действия. Эталонный скелет фазовой игры и шаблон обязаны проходить чисто.
"""
import io
import json
import os
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import prompts, simulationist as S  # noqa: E402
from simulation import runner  # noqa: E402

checks = {}

TPL = prompts.load_skeleton_template()
REF = io.open("simulation/templates/reference_judge_game.py", encoding="utf-8").read()
CORE = {"turn": {"actions": ["nominate_bias", "render_verdict"]}}

META = {
    "simulatable": True, "player_counts": [3, 4, 5, 6], "pattern": "judge_picks_best",
    "manual_turn_order": True, "assumptions": ["выбор судьи субъективен"],
    "subjective_actions": ["render_verdict"], "coalition_expressible": False,
    "coalition_note": "подачи анонимны для судьи",
    "metric_responds_immediately": False, "fixed_length": True,
    "end_reasons_used": ["most_points", "tie_unresolved"],
    "content_scale": {"bias_cards": 150}, "ignored_components": [],
    "hooks_filled": {"snapshot_metric": True, "snapshot_resources": True,
                     "hand_snapshot": True, "state_signature": True,
                     "clone_state": True, "win_path": True},
}


def codes(issues):
    return [i["code"] for i in issues]


# ============================================================================
# ЧАСТЬ 1. Разбор ответа: два блока, код НЕ в JSON
# ============================================================================
two_blocks = json.dumps(META, ensure_ascii=False) + "\n\n```python\n" + REF + "\n```"
meta, code, iss = S.parse_response(two_blocks)
checks["метаданные разобраны"] = meta and meta["pattern"] == "judge_picks_best"
checks["код разобран отдельным блоком"] = code and "def setup_game" in code
checks["чистый формат без замечаний"] = iss == []

# JSON в ```json-обёртке — модели так делают
fenced = ("```json\n" + json.dumps(META, ensure_ascii=False) + "\n```\n\n```python\n"
          + REF + "\n```")
meta2, code2, _ = S.parse_response(fenced)
checks["обёртка ```json разобрана"] = meta2 and code2 and "def setup_game" in code2

# запрещённый вариант: код внутри JSON — разбираем, но сообщаем о нарушении
inside = json.dumps(dict(META, code="print('x')"), ensure_ascii=False)
meta3, code3, iss3 = S.parse_response(inside)
checks["код в JSON разобран"] = code3 == "print('x')"
checks["о коде в JSON сообщено"] = "code_inside_json" in codes(iss3)
checks["поле code вычищено из метаданных"] = "code" not in meta3

# отказ «несимулируема» — одного JSON достаточно
meta4, code4, _ = S.parse_response(
    json.dumps({"simulatable": False, "reason": "нет условия победы",
                "missing": ["win_condition"]}, ensure_ascii=False))
checks["отказ разобран"] = meta4["simulatable"] is False and code4 is None

# ============================================================================
# ЧАСТЬ 2. Самопроверка кода: эталон чист, порча ловится
# ============================================================================
checks["эталон фазовой игры чист"] = S.validate(META, REF, CORE, TPL) == []
checks["шаблон как есть чист"] = S.validate(
    dict(META, manual_turn_order=False, player_counts=[2, 3, 4],
         subjective_actions=[], end_reasons_used=["goal_reached", "round_cap"]),
    TPL, {"turn": {"actions": ["move", "invest"]}}, TPL) == []

# reason вне закрытого словаря — самая дорогая подмена: «Оценщик статистик»
# не вычтет ничьи и выставит ложный critical о недостижимости победы
bad = REF.replace('winner, reason = None, "tie_unresolved"', 'winner, reason = None, "tie"')
checks["reason вне словаря пойман"] = "reason_unknown" in codes(S.validate(META, bad, CORE, TPL))

# умный базовый choose_action ломает эталон для «Оценщика статистик»
bad = REF.replace("    return rng.choice(legal_actions)\n\n\ndef apply_action",
                  "    return max(legal_actions)\n\n\ndef apply_action")
checks["умный choose_action пойман"] = "choose_action_smart" in codes(S.validate(META, bad, CORE, TPL))

# служебный счётчик в подписи — петля не найдётся никогда
bad = REF.replace('        state.current, state.phase, state.round, state.extra.get("judge"),',
                  "        state.current, state.total_actions, state.round,")
checks["счётчик в подписи пойман"] = "signature_has_counters" in codes(S.validate(META, bad, CORE, TPL))

# правка движка и ослабленный предохранитель
bad = (REF.replace("EXPLOIT_MAX_NODES = 5000", "EXPLOIT_MAX_NODES = 50000")
          .replace("def advance(state):", "def advance(state):  # улучшено"))
c = codes(S.validate(META, bad, CORE, TPL))
checks["правка ДВИЖКА поймана"] = "engine_modified" in c
checks["ослабленный предохранитель пойман"] = "guard_increased" in c

# параметр в имени действия обнуляет три теста методички
bad = REF.replace('        return ["nominate_bias"] if state.extra["hand"].get(s) else []',
                  '        return [f"nominate_bias:{c}" for c in state.extra["hand"].get(s, [])]')
checks["параметр в имени действия пойман"] = "action_name_parametrized" in codes(
    S.validate(META, bad, CORE, TPL))

# метаданные разошлись с кодом
checks["расхождение manual_turn_order поймано"] = "manual_turn_order_mismatch" in codes(
    S.validate(dict(META, manual_turn_order=False), REF, CORE, TPL))

# внешняя зависимость
bad = REF.replace("import random", "import random\nimport numpy")
checks["внешний импорт пойман"] = "import_forbidden" in codes(S.validate(META, bad, CORE, TPL))

# синтаксически битый код
checks["битый код пойман"] = "syntax_error" in codes(S.validate(META, "def f(:\n  pass", CORE, TPL))

# отсутствующие метаданные — их читают следующие агенты
skinny = {"simulatable": True, "player_counts": [3]}
c = codes(S.validate(skinny, REF, CORE, TPL))
checks["пропущенные метаданные пойманы"] = c.count("meta_field_missing") >= 5

# субъективное действие, которого нет в core
checks["чужое субъективное действие поймано"] = "subjective_action_unknown" in codes(
    S.validate(dict(META, subjective_actions=["несуществующее"]), REF, CORE, TPL))

# ============================================================================
# ЧАСТЬ 3. Оба блока вывода реально доезжают из прогона скелета
# ============================================================================
fast = TPL.replace("GAMES_PER_CONFIG = 2000", "GAMES_PER_CONFIG = 60") \
          .replace("GAMES_PER_EXTRA_RUN = 600", "GAMES_PER_EXTRA_RUN = 20")
res = runner.run_skeleton(fast, timeout=240)
checks["шаблон v4 прогоняется"] = res["ok"] is True
if res["ok"]:
    stats, diag = res["stats"], res["diag"]
    checks["STATS_JSON собран"] = isinstance(stats, list) and "win_rate_by_seat" in stats[0]
    checks["STATS_JSON не задет диагностикой"] = all(
        "softlock_rate" not in cfg for cfg in stats)
    checks["DIAG_JSON собран"] = isinstance(diag, dict) and "runs" in diag
    checks["в DIAG есть базовый прогон"] = any(
        r["run_id"] == "base" for r in diag["runs"])
    checks["в DIAG есть персоны"] = {"persona_passive", "persona_expert"} <= {
        r["run_id"] for r in diag["runs"]}
    checks["в DIAG есть поиск петель"] = "exploit_search" in diag
    d0 = next(r for r in diag["runs"] if r["run_id"] == "base")["diagnostics"]
    for field in ("softlock_rate", "full_block_rate", "pass_rate", "action_effect",
                  "predetermination_rate", "card_coverage", "win_path_share"):
        checks[f"диагностика: {field}"] = field in d0
    # null — значимая величина: «не хватило наблюдений», а не ноль
    checks["null не подменён нулём"] = (
        d0["spiral_ratio"] is None or isinstance(d0["spiral_ratio"], dict))

# скелет v3 без DIAG_JSON — это не ошибка, а отсутствие данных
v3_out = "JSON ДЛЯ ОЦЕНКИ БАЛАНСА\n" + json.dumps(
    [{"num_players": 2, "win_rate_by_seat": {"0": 0.5}}], ensure_ascii=False)
checks["stats из вывода v3 читаются"] = runner.extract_stats(v3_out)[0]["num_players"] == 2
checks["отсутствие DIAG даёт None"] = runner.extract_diag(v3_out) is None

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
