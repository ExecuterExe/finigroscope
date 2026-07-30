# -*- coding: utf-8 -*-
"""Проверка извлеченца v3: два контура вывода и самопроверка закрытого контракта.

Главное, что проверяется, — БЕСШУМНЫЕ поломки, о которых предупреждает промпт:
рассинхрон actions_resolution с core.turn.actions (классификация молча
обнуляется), diagnostic_meta внутри game_spec (его не увидит ни один
потребитель) и сокращённый full_rules (выглядит эквивалентным оригиналу).
Плюс различие absent / missing и передача карты узлов от гейта.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import extractor  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

CANON = "ПРАВИЛА ИГРЫ. " + ("Подробный текст правил с деталями. " * 40)

checks = {}

# ============================================================================
# ЧАСТЬ 1. validate() — самопроверка контракта, без сети
# ============================================================================

CONFIRMED = {
    "original_text": CANON,
    "author_clarifications": [],
    "still_open": [],
    "map": [
        {"node": "карты на руках", "status": "absent", "note": "рук нет"},
        {"node": "режимы сложности", "status": "absent", "note": "вариантов нет"},
        {"node": "тай-брейк", "status": "missing", "note": "равенство возможно"},
    ],
}

checks["absent-узлы вычленяются из карты"] = extractor.absent_nodes(CONFIRMED) == [
    "карты на руках", "режимы сложности"]

# --- 1а) агент вложил diagnostic_meta внутрь game_spec (частая ошибка) -------
nested = {
    "game_spec": {
        "core": {"turn": {"order": "clockwise", "actions": ["pitch", "invest"]},
                 "resources": [{"name": "жетоны"}],
                 "win_condition": {"type": "most", "metric": "жетоны", "threshold": None}},
        "text": {"full_rules": CANON},
        "diagnostic_meta": {"actions_resolution": {"pitch": "subjective_judgment",
                                                   "invest": "deterministic"},
                            "resource_roles": {"жетоны": "both"}},
    },
    "gaps": [], "ambiguities": [],
}
root, issues = extractor.validate(nested, CONFIRMED, CANON)
codes = [i["code"] for i in issues]
checks["вложенный diagnostic_meta поднят наверх"] = (
    "diagnostic_meta" in root and "diagnostic_meta" not in root["game_spec"])
checks["о переносе надстройки сообщено"] = "meta_nested" in codes
checks["классификация не потерялась"] = (
    root["diagnostic_meta"]["actions_resolution"]["pitch"] == "subjective_judgment")
checks["субъективные действия видны"] = extractor.subjective_actions(root) == ["pitch"]
checks["синхронный случай не даёт ложных жалоб"] = "resolution_missing" not in codes

# --- 1б) рассинхрон имён действий (бесшумно обнуляет классификацию) ----------
desync = {
    "game_spec": {
        "core": {"turn": {"actions": ["pitch_product", "invest_in_player"]},
                 "resources": [{"name": "капитал"}, {"name": "жетоны"}],
                 "win_condition": {"type": "most"}},
        "text": {"full_rules": CANON},
    },
    "diagnostic_meta": {
        "actions_resolution": {"pitch": "subjective_judgment", "vote": "нечто"},
        "resource_roles": {"капитал": "spendable"},
    },
    "gaps": [], "ambiguities": [],
}
root, issues = extractor.validate(desync, CONFIRMED, CANON)
by_code = {i["code"]: i for i in issues}
checks["пропущенные классификации найдены"] = "resolution_missing" in by_code
checks["названы конкретные действия"] = (
    "pitch_product" in by_code["resolution_missing"]["message"]
    and "invest_in_player" in by_code["resolution_missing"]["message"])
checks["рассинхрон помечен как важный"] = by_code["resolution_missing"]["severity"] == "error"
checks["лишние ключи классификации найдены"] = "resolution_extra" in by_code
checks["неизвестный тип разрешения найден"] = "resolution_unknown_kind" in by_code
checks["ресурс без роли найден"] = (
    "resource_role_missing" in by_code
    and "жетоны" in by_code["resource_role_missing"]["message"])

# --- 1в) закрытый контракт: лишние поля и win_condition списком --------------
broken = {
    "game_spec": {
        "core": {"win_condition": [{"type": "most"}, {"type": "collect"}],
                 "turn": {"actions": []}, "win_paths": ["a", "b"], "tie_breaker": {}},
        "text": {"full_rules": CANON, "author_bio": "лишнее"},
    },
    "diagnostic_meta": {}, "gaps": [], "ambiguities": [],
}
root, issues = extractor.validate(broken, CONFIRMED, CANON)
by_code = {i["code"]: i for i in issues}
msgs = " ".join(i["message"] for i in issues)
checks["win_condition приведён к объекту"] = isinstance(
    root["game_spec"]["core"]["win_condition"], dict)
checks["о списке win_condition сообщено"] = "win_condition_list" in by_code
checks["лишние поля core найдены"] = "win_paths" in msgs and "tie_breaker" in msgs
checks["лишнее поле text найдено"] = "author_bio" in msgs
checks["надстройка заполнена заготовками"] = (
    root["diagnostic_meta"]["hand_exists"] is None
    and root["diagnostic_meta"]["win_paths"]["paths"] == [])

# --- 1г) full_rules: сокращение молча теряет материал ------------------------
short = {"game_spec": {"core": {"turn": {"actions": []}},
                       "text": {"full_rules": "Правила: кратко о главном."}},
         "diagnostic_meta": {}, "gaps": [], "ambiguities": []}
root, issues = extractor.validate(short, CONFIRMED, CANON)
checks["сокращённый full_rules восстановлен"] = root["game_spec"]["text"]["full_rules"] == CANON
checks["о сокращении сообщено"] = "full_rules_shortened" in [i["code"] for i in issues]

empty_rules = {"game_spec": {"core": {"turn": {"actions": []}}, "text": {}},
               "diagnostic_meta": {}, "gaps": [], "ambiguities": []}
root, issues = extractor.validate(empty_rules, CONFIRMED, CANON)
checks["пустой full_rules заполнен каноном"] = root["game_spec"]["text"]["full_rules"] == CANON

# --- 1д) original_text дублировать нельзя (его несёт код) --------------------
dup = {"game_spec": {"core": {"turn": {"actions": []}},
                     "text": {"full_rules": CANON, "original_text": CANON}},
       "diagnostic_meta": {}, "gaps": [], "ambiguities": []}
root, issues = extractor.validate(dup, CONFIRMED, CANON)
checks["дубль original_text удалён"] = "original_text" not in root["game_spec"]["text"]
checks["о дубле сообщено"] = "original_text_duplicated" in [i["code"] for i in issues]

# --- 1е) absent не может быть пробелом --------------------------------------
absent_gap = {
    "game_spec": {"core": {"turn": {"actions": []}}, "text": {"full_rules": CANON}},
    "diagnostic_meta": {},
    "gaps": [{"field": "diagnostic_meta.hand_exists",
              "missing": "не сказано, что игроки держат карты на руках", "critical": True},
             {"field": "limits.max_rounds", "missing": "лимит раундов не назван",
              "critical": True}],
    "ambiguities": [],
}
root, issues = extractor.validate(absent_gap, CONFIRMED, CANON)
absent_issues = [i for i in issues if i["code"] == "absent_in_gaps"]
checks["absent в gaps обнаружен"] = len(absent_issues) == 1
checks["настоящий пробел не оболган"] = "max_rounds" not in absent_issues[0]["message"]
checks["gaps не вычищены молча"] = len(root["gaps"]) == 2

# --- 1ж) надстройки нет вовсе -> ошибка, а не тихая пустота ------------------
root, issues = extractor.validate(
    {"game_spec": {"core": {"turn": {"actions": []}}, "text": {"full_rules": CANON}}},
    CONFIRMED, CANON)
by_code = {i["code"]: i for i in issues}
checks["отсутствие надстройки — ошибка"] = (
    "meta_missing" in by_code and by_code["meta_missing"]["severity"] == "error")

# ============================================================================
# ЧАСТЬ 2. Сквозной прогон: карта узлов доходит до агента
# ============================================================================

seen = {"map_in_prompt": None, "absent_hint": None}


def _good_spec():
    return {
        "game_spec": {
            "core": {
                "players": {"min": 3, "max": 6}, "mode": "competitive", "elimination": False,
                "turn": {"order": "clockwise", "actions": ["pitch_product", "invest_in_player"]},
                "randomness": [{"type": "card_draw"}],
                "resources": [
                    {"name": "personal_capital", "scope": "per_player", "start": 10, "goal": None},
                    {"name": "attracted_investments", "scope": "per_player", "start": 0, "goal": None}],
                "win_condition": {"type": "most", "metric": "attracted_investments",
                                  "threshold": None},
                "loss_condition": {"type": "rounds_exhausted"},
                "catch_up": {"enabled": False, "mechanism": None},
                "play_time": "60 минут",
                "limits": {"max_rounds": 3},
            },
            "text": {"concept": "Игра про инвестиции.", "setting": "Стартап-акселератор.",
                     "full_rules": CANON,
                     "components": [{"name": "жетон", "qty": 150, "material": "картон",
                                     "function": "валюта и счётчик инвестиций"}],
                     "recommendations": []},
        },
        "diagnostic_meta": {
            "win_paths": {"multiple": True, "paths": ["best_entrepreneur", "best_investor"],
                          "source": "текст: «две цели»"},
            "tie_breaker": {"applicable": True, "present": False, "description": None},
            "actions_resolution": {"pitch_product": "subjective_judgment",
                                   "invest_in_player": "subjective_judgment"},
            "resource_roles": {"personal_capital": "spendable",
                               "attracted_investments": "win_metric"},
            "targeted_actions": {"exists": True, "actions": ["invest_in_player"]},
            "initial_deal": {"random": False, "what": None, "identical_start": True},
            "hand_exists": False,
            "strict_player_count": {"strict": False, "declared": None},
            "difficulty_modes": {"exists": False, "modes": []},
            "subjective_notes": "исход питча определяют другие игроки",
        },
        "gaps": [{"field": "win_condition.threshold", "critical": False,
                  "source": "unresolved_by_author", "missing": "порог не задан",
                  "why_matters": "победа определяется сравнением"}],
        "ambiguities": [], "source_format": "essay",
    }


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "извлекатель структуры" in system:
            seen["map_in_prompt"] = "=== MAP" in user and "absent" in user
            seen["absent_hint"] = "карты на руках" in user and "НЕ должен попасть в gaps" in user
            return json.dumps(_good_spec(), ensure_ascii=False)

        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Игра про инвестиции.",
                    "map": CONFIRMED["map"],
                    "questions": [{"id": 1, "question": "Порог победы?", "why": "нужно",
                                   "type": "rules"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": CANON,
                    "author_clarifications": [], "still_open": [], "ready_to_proceed": True}
        return "Ответ.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import GameSpec, MirrorSession  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

c.get(f"/documents/{doc_id}/mirror/1")
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["карта узлов сохранена отдельно"] = len(ms.map_list()) == 3

c.post(f"/documents/{doc_id}/mirror/1/reply", data={"answer": "всё ясно"},
       follow_redirects=True)
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    # подтверждение прохода 2 не должно стереть карту прохода 1
    checks["карта выжила после подтверждения"] = len(ms.map_list()) == 3
    checks["в подтверждённом JSON карты нет"] = "map" not in (ms.last_json_dict() or {})

html = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)

checks["карта дошла до извлеченца"] = seen["map_in_prompt"] is True
checks["absent продублирован подсказкой"] = seen["absent_hint"] is True

# --- надстройка отрисована ---------------------------------------------------
checks["блок надстройки показан"] = "Диагностическая надстройка" in html
checks["классификация действий показана"] = "суждением игроков" in html
checks["предупреждение о плейтесте"] = "живой плейтест" in html
checks["роли ресурсов показаны"] = "копится ради победы" in html and "тратится на действия" in html
checks["несколько путей к победе"] = "best_entrepreneur" in html
checks["тай-брейк без правил показан"] = "равенство возможно, правил нет" in html
checks["новые поля ядра"] = "Догоняние" in html and "Длительность" in html

# --- описание для автора обогатилось ----------------------------------------
checks["описание: путей несколько"] = "Путей к победе несколько" in html
checks["описание: роль ресурса"] = "Роль: тратится на действия" in html
checks["описание: субъективные действия"] = "определяют сами игроки" in html
checks["описание: одинаковый старт"] = "одинаковых условиях" in html
checks["описание: длительность"] = "60 минут" in html

# --- корректный ответ не порождает ложных жалоб ------------------------------
with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    saved = gs.spec_dict()
    real_issues = [i for i in gs.issues() if i["code"] != "full_rules_shortened"]
    checks["корректная структура без нарушений"] = real_issues == []
    checks["надстройка сохранена рядом"] = "diagnostic_meta" in saved
    checks["надстройки нет внутри контракта"] = "diagnostic_meta" not in saved["game_spec"]
    checks["full_rules равен канону"] = saved["game_spec"]["text"]["full_rules"] == \
        A.stage1.game_text_for_agent(
            GameSpec.query.filter_by(document_id=doc_id).first().document.stored_path,
            "essay", 1)

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
