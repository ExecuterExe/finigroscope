# -*- coding: utf-8 -*-
"""Проверка агента «Авто-редизайнер» (v2).

Главное — УСЛОВИЕ ВЫЗОВА: агент зовётся только при критичных находках, и это
решает код, а не модель (правка меняет структуру игры и обнуляет скелет со
статистикой). Плюс дисциплина вмешательства: запрет ступени 3, запрет правок по
«мягким» числам, защита от осцилляции, синхронность трёх слоёв и неизменность
закрытого контракта game_spec.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import redesigner as rd  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

checks = {}

# ============================================================================
# ЧАСТЬ 1. Условие вызова и самопроверка — без сети
# ============================================================================

SPEC = {
    "game_spec": {
        "core": {"players": {"min": 2, "max": 4}, "mode": "competitive", "elimination": False,
                 "turn": {"order": "clockwise", "actions": ["move", "invest", "trade"]},
                 "randomness": [{"type": "d6"}],
                 "resources": [{"name": "деньги", "scope": "per_player", "start": 10, "goal": None}],
                 "win_condition": {"type": "reach", "metric": "position", "threshold": 24},
                 "loss_condition": {"type": "bankrupt"},
                 "catch_up": {"enabled": False, "mechanism": None},
                 "play_time": "40 минут", "limits": {"max_rounds": 3}},
        "text": {"concept": "Гонка.", "setting": "Город.", "full_rules": "Правила: порог 24.",
                 "components": [], "recommendations": []},
    },
    "diagnostic_meta": {
        "actions_resolution": {"move": "deterministic", "invest": "probabilistic",
                               "trade": "deterministic"},
        "targeted_actions": {"exists": True, "actions": ["trade"]},
        "resource_roles": {"деньги": "both"},
        "tie_breaker": {"applicable": True, "present": False},
        "win_paths": {"multiple": False, "paths": []},
    },
    "gaps": [], "ambiguities": [],
}


def finding(flags):
    return {"mode": "competitive", "configs_analyzed": [2, 3, 4], "checks": [],
            "flags": flags, "scores": {"balance": 3, "win_reachability": 3},
            "priority_fixes": [], "covered_tests": [], "notes_for_lenses": []}


CRIT = {"code": "unreachable_win", "where": "core.win_condition.threshold",
        "detail": "истинная недостижимость 32% при N=4", "severity": "critical",
        "confidence": "measured", "affected_configs": [4]}
SOFT_CRIT = {"code": "dominant_strategy", "where": "core.turn.actions",
             "detail": "0.85", "severity": "critical", "confidence": "assumed",
             "affected_configs": [4]}
MAJOR = {"code": "too_long", "where": "core.limits.max_rounds", "detail": "3x",
         "severity": "major", "confidence": "measured", "affected_configs": [2]}
DEAD = {"code": "dead_action", "where": "core.turn.actions", "detail": "action_share=0.01",
        "severity": "minor", "confidence": "measured", "affected_configs": [2, 3, 4]}
SOCIAL = {"code": "coalition_effect", "where": "turn", "detail": "+22 п.п.",
          "severity": "major", "confidence": "measured", "affected_configs": [4]}

# --- условие вызова ----------------------------------------------------------
t = rd.should_trigger(finding([CRIT, MAJOR]))
checks["критичное запускает правку"] = t["trigger"] is True and "unreachable_win" in t["reason"]

t = rd.should_trigger(finding([MAJOR, DEAD]))
checks["без критичного не запускается"] = t["trigger"] is False and "критичных флагов нет" in t["reason"]

t = rd.should_trigger(finding([SOFT_CRIT]))
checks["критичное на мягком числе не запускает"] = t["trigger"] is False
checks["и объясняет почему"] = "мягких" in t["reason"]

checks["пустой отчёт не запускает"] = rd.should_trigger(finding([]))["trigger"] is False
checks["режим A при одной статистике"] = rd.detect_mode(finding([CRIT])) == rd.MODE_A
checks["режим D при находках диагноста"] = rd.detect_mode(
    finding([CRIT]), findings_diagnost=[{"code": "exploit_loop", "severity": "critical"}]) == rd.MODE_D
checks["режим B при линзах"] = rd.detect_mode(
    finding([CRIT]), findings_diagnost=[{"code": "x"}], findings_lenses=[{"lens": 32}]) == rd.MODE_B

# --- корректная правка не даёт ложных жалоб ---------------------------------
OK_RESULT = {
    "mode": "A_technical", "attempt_number": 1,
    "revised_game_spec": json.loads(json.dumps(SPEC["game_spec"])),
    "revised_diagnostic_meta": json.loads(json.dumps(SPEC["diagnostic_meta"])),
    "changes": [{"field": "core.win_condition.threshold", "from": 24, "to": 16,
                 "addresses": "unreachable_win", "confidence_of_source": "measured",
                 "ladder": 1, "step": "small"}],
    "handed_to_recommendations": [], "kept_intent": ["competitive", "no elimination"],
    "tradeoffs": [], "not_touched": [], "needs_resimulation": True,
}
OK_RESULT["revised_game_spec"]["core"]["win_condition"]["threshold"] = 16
_, issues = rd.validate(OK_RESULT, SPEC, finding([CRIT]), [], rd.MODE_A)
checks["корректная правка без жалоб"] = issues == []


def codes(iss):
    return [i["code"] for i in iss]


def with_changes(changes, **over):
    r = json.loads(json.dumps(OK_RESULT))
    r["changes"] = changes
    r.update(over)
    return r


# --- ступень 3 запрещена -----------------------------------------------------
_, issues = rd.validate(with_changes([
    {"field": "core.win_condition.tiebreak", "to": "по капиталу", "addresses": "tie_unresolved",
     "ladder": 3}]), SPEC, finding([CRIT, {"code": "tie_unresolved", "severity": "major",
                                           "confidence": "measured"}]), [], rd.MODE_A)
checks["ступень 3 поймана"] = "ladder_three" in codes(issues)

# --- правка по мягкому числу -------------------------------------------------
_, issues = rd.validate(with_changes([
    {"field": "core.turn.actions", "op": "disable", "element": "invest",
     "addresses": "dominant_strategy", "ladder": 2, "subtraction_test": True}]),
    SPEC, finding([CRIT, SOFT_CRIT]), [], rd.MODE_A)
checks["правка по мягкому числу поймана"] = "change_on_assumed" in codes(issues)

# --- социальная находка ------------------------------------------------------
_, issues = rd.validate(with_changes([
    {"field": "core.turn.order", "from": "clockwise", "to": "random",
     "addresses": "coalition_effect", "ladder": 1}]),
    SPEC, finding([CRIT, SOCIAL]), [], rd.MODE_A)
checks["социальная правка поймана"] = "change_on_social" in codes(issues)

# --- правка без привязки к флагу ---------------------------------------------
_, issues = rd.validate(with_changes([
    {"field": "core.limits.max_rounds", "from": 3, "to": 6, "addresses": "", "ladder": 1}]),
    SPEC, finding([CRIT]), [], rd.MODE_A)
checks["правка без источника поймана"] = "change_without_source" in codes(issues)

_, issues = rd.validate(with_changes([
    {"field": "core.limits.max_rounds", "from": 3, "to": 6, "addresses": "выглядит долго",
     "ladder": 1}]), SPEC, finding([CRIT]), [], rd.MODE_A)
checks["выдуманный флаг пойман"] = "change_flag_unknown" in codes(issues)

# --- ОСЦИЛЛЯЦИЯ: возврат к прежнему значению ---------------------------------
prev = [{"attempt": 1, "field": "core.win_condition.threshold", "from": 24, "to": 16,
         "addresses": "unreachable_win"}]
_, issues = rd.validate(with_changes([
    {"field": "core.win_condition.threshold", "from": 16, "to": 24,
     "addresses": "too_hard", "ladder": 1}]),
    SPEC, finding([CRIT, {"code": "too_hard", "severity": "critical", "confidence": "measured"}]),
    prev, rd.MODE_A)
checks["осцилляция поймана"] = "oscillation" in codes(issues)
checks["осцилляция помечена важной"] = any(
    i["code"] == "oscillation" and i["severity"] == "error" for i in issues)
checks["история правок собирается"] = rd.collect_previous_changes(
    [{"attempt_number": 1, "changes": prev}])[0]["field"] == "core.win_condition.threshold"

# --- расширение закрытого контракта ------------------------------------------
bad_spec = json.loads(json.dumps(OK_RESULT))
bad_spec["revised_game_spec"]["core"]["tie_rule"] = "по капиталу"
bad_spec["revised_game_spec"]["text"]["designer_note"] = "лишнее"
_, issues = rd.validate(bad_spec, SPEC, finding([CRIT]), [], rd.MODE_A)
c = codes(issues)
checks["новое поле core поймано"] = "core_extra_field" in c
checks["новое поле text поймано"] = "text_extra_field" in c

# --- рассинхрон трёх слоёв после отключения действия -------------------------
desync = json.loads(json.dumps(OK_RESULT))
desync["revised_game_spec"]["core"]["turn"]["actions"] = ["move", "invest"]  # trade убрали
desync["changes"] = [{"field": "core.turn.actions", "op": "disable", "element": "trade",
                      "addresses": "dead_action", "ladder": 2, "subtraction_test": True}]
_, issues = rd.validate(desync, SPEC, finding([CRIT, DEAD]), [], rd.MODE_A)
c = codes(issues)
checks["забытый ключ классификации пойман"] = "resolution_extra" in c
checks["забытое адресное действие поймано"] = "targeted_stale" in c

synced = json.loads(json.dumps(desync))
synced["revised_diagnostic_meta"]["actions_resolution"].pop("trade")
synced["revised_diagnostic_meta"]["targeted_actions"]["actions"] = []
_, issues = rd.validate(synced, SPEC, finding([CRIT, DEAD]), [], rd.MODE_A)
checks["синхронизированное отключение чисто"] = issues == []

# --- замысел не ломается -----------------------------------------------------
intent = json.loads(json.dumps(OK_RESULT))
intent["revised_game_spec"]["core"]["mode"] = "cooperative"
intent["revised_game_spec"]["core"]["elimination"] = True
_, issues = rd.validate(intent, SPEC, finding([CRIT]), [], rd.MODE_A)
c = codes(issues)
checks["смена режима поймана"] = "intent_mode_changed" in c
checks["смена выбывания поймана"] = "intent_elimination_changed" in c

# --- лимит правок и needs_resimulation ---------------------------------------
many = with_changes([dict(OK_RESULT["changes"][0], field=f"core.f{i}") for i in range(5)],
                    needs_resimulation=False)
_, issues = rd.validate(many, SPEC, finding([CRIT]), [], rd.MODE_A)
c = codes(issues)
checks["перебор правок пойман"] = "too_many_changes" in c
checks["отсутствие пересимуляции поймано"] = "needs_resimulation_missing" in c

# --- находка не должна потеряться --------------------------------------------
_, issues = rd.validate(OK_RESULT, SPEC, finding([CRIT, SOFT_CRIT, SOCIAL]), [], rd.MODE_A)
checks["потерянные находки пойманы"] = codes(issues).count("finding_lost") == 2

# --- режим D не трогает major ------------------------------------------------
diag = [{"code": "exploit_loop", "severity": "critical"},
        {"code": "death_spiral", "severity": "major"}]
_, issues = rd.validate(with_changes([
    {"field": "core.catch_up.mechanism", "from": None, "to": "+2", "addresses": "death_spiral",
     "ladder": 1}], mode="D_diagnostic"), SPEC, finding([CRIT]), [], rd.MODE_D, diag)
checks["режим D не чинит major"] = "mode_d_touches_non_critical" in codes(issues)

# --- объявленный режим сверяется с фактическим -------------------------------
_, issues = rd.validate(with_changes(OK_RESULT["changes"], mode="B_full_critique"),
                        SPEC, finding([CRIT]), [], rd.MODE_A)
checks["подмена режима поймана"] = "mode_mismatch" in codes(issues)

# --- применение к структуре --------------------------------------------------
applied = rd.apply_to_spec(SPEC, OK_RESULT)
checks["структура обновлена"] = applied["game_spec"]["core"]["win_condition"]["threshold"] == 16
checks["пробелы сохранены"] = "gaps" in applied

# ============================================================================
# ЧАСТЬ 2. Сквозной прогон через интерфейс
# ============================================================================

seen = {"prev_in_prompt": None, "soft_listed": None, "mode": None}
calls = {"redesign": 0}

AGENT_OUT = json.loads(json.dumps(OK_RESULT))
AGENT_OUT["handed_to_recommendations"] = [
    {"finding": "dominant_strategy: доля 0.85", "suggestion": "оценить содержательно",
     "why_not_auto": "confidence: assumed — число отражает допущение симулятора"}]
FINDING = finding([CRIT, SOFT_CRIT])


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "редактор игрового баланса" in system:
            calls["redesign"] += 1
            seen["prev_in_prompt"] = "PREVIOUS_CHANGES" in user
            seen["soft_listed"] = "МЯГКИХ ЧИСЛАХ" in user and "dominant_strategy" in user
            seen["mode"] = "A_technical" in user
            return json.dumps(AGENT_OUT, ensure_ascii=False)

        if "аналитик игрового баланса" in system:
            return json.dumps(FINDING, ensure_ascii=False)
        if "инженер игровых симуляций" in system:
            return json.dumps({"simulatable": True, "player_counts": [2, 4],
                               "assumptions": [], "code": "print('sk')"}, ensure_ascii=False)
        if "извлекатель структуры" in system:
            return json.dumps(SPEC, ensure_ascii=False)
        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Гонка.", "map": [],
                    "questions": [{"id": 1, "question": "?", "why": "!", "type": "rules"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": "текст",
                    "author_clarifications": [], "still_open": [], "ready_to_proceed": True}
        return "Ответ.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import BalanceReport, GameSkeleton, GameSpec, RedesignAttempt  # noqa: E402

cl = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = cl.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
                content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

cl.get(f"/documents/{doc_id}/mirror/1")
cl.post(f"/documents/{doc_id}/mirror/1/reply", data={"answer": "ок"}, follow_redirects=True)
cl.get(f"/documents/{doc_id}/spec/1")
cl.post(f"/documents/{doc_id}/spec/1/accept", follow_redirects=True)
cl.get(f"/documents/{doc_id}/skeleton/1")

STATS = [{"games": 500, "num_players": 4, "win_rate_by_seat": {"0": 0.3},
          "no_winner_rate": 0.32, "end_reason_share": {"round_cap": 0.32},
          "rounds": {"mean": 9.0}, "action_share": {"move": 0.5},
          "avg_eliminated_per_game": 0.0}]
bal = cl.post(f"/documents/{doc_id}/balance/1/stats",
              data={"stats": json.dumps(STATS, ensure_ascii=False)},
              follow_redirects=True).get_data(as_text=True)

checks["баланс предлагает авто-редизайн"] = "Можно починить автоматически" in bal
checks["назван критичный флаг"] = "unreachable_win" in bal
checks["агента ещё не звали"] = calls["redesign"] == 0

page = cl.get(f"/documents/{doc_id}/redesign/1").get_data(as_text=True)
checks["экран объясняет условие вызова"] = "Нужен ли авто-редизайн" in page
checks["видно, что нужен"] = "есть критичные флаги" in page
checks["видно лимит попыток"] = "из 3" in page
checks["без запроса агент не звался"] = calls["redesign"] == 0

prop = cl.post(f"/documents/{doc_id}/redesign/1/propose",
               follow_redirects=True).get_data(as_text=True)
checks["правка предложена"] = calls["redesign"] == 1
checks["история подана агенту"] = seen["prev_in_prompt"] is True
checks["мягкие флаги перечислены агенту"] = seen["soft_listed"] is True
checks["режим передан агенту"] = seen["mode"] is True
checks["изменение показано"] = "core.win_condition.threshold" in prop
checks["ступень названа"] = "ступень 1" in prop
checks["рекомендация показана"] = "оценить содержательно" in prop
checks["есть кнопки решения"] = "Принять и пересимулировать" in prop and "Отклонить" in prop

with A.app.app_context():
    att = RedesignAttempt.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["попытка сохранена как предложение"] = att.status == RedesignAttempt.STATUS_PROPOSED
    checks["структура ещё не изменена"] = (
        GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
        .spec_dict()["game_spec"]["core"]["win_condition"]["threshold"] == 24)
    checks["самопроверка чиста"] = att.issues() == []

# --- принятие: структура меняется, симуляция обнуляется ----------------------
# Редирект НЕ следуем намеренно: он ведёт на /skeleton, который тут же соберёт
# скелет заново по новой структуре (это и есть needs_resimulation). Проверить
# нужно именно факт сброса, а не состояние после пересборки.
resp = cl.post(f"/documents/{doc_id}/redesign/1/decide", data={"action": "accept"})
checks["принятие ведёт на пересимуляцию"] = "/skeleton/" in resp.headers.get("Location", "")
with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=1).first()
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=1).first()
    att = RedesignAttempt.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["структура обновлена"] = gs.spec_dict()["game_spec"]["core"]["win_condition"]["threshold"] == 16
    checks["попытка принята"] = att.status == RedesignAttempt.STATUS_ACCEPTED
    checks["сохранена версия до правки"] = att.spec_before()["game_spec"]["core"]["win_condition"]["threshold"] == 24
    checks["скелет сброшен"] = sk.simulatable is None and sk.code is None
    checks["статистика сброшена"] = br.stats_json is None and br.report_json is None

# теперь пройдём по редиректу — скелет обязан пересобраться по НОВОЙ структуре
sim_before = calls.get("sim", 0)
cl.get(f"/documents/{doc_id}/skeleton/1")
with A.app.app_context():
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["скелет пересобран после правки"] = sk.simulatable is True and bool(sk.code)

# --- без отчёта о балансе экран закрыт ---------------------------------------
closed = cl.get(f"/documents/{doc_id}/redesign/1", follow_redirects=True).get_data(as_text=True)
checks["без оценки баланса вход закрыт"] = "Сначала нужна оценка баланса" in closed

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
