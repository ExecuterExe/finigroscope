# -*- coding: utf-8 -*-
"""Проверка агента «Оценщик статистик» (v4) и приёма STATS_JSON.

Главное — две дорогие ошибки, незаметные в самом отчёте:
  1) неразрешённая ничья, посчитанная недостижимой победой (правка тогда уйдёт
     в порог победы вместо правила ничьей — ровно баг v3);
  2) вердикт problem на «мягком» числе (доля субъективного действия — артефакт
     допущения симулятора, а не поведение игроков).
Плюс границы зон (DIAG_JSON не подаётся), разбор по конфигурациям и приём
статистики без исполнения кода.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import stats_evaluator as ev  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402
from simulation import runner  # noqa: E402

checks = {}

# ============================================================================
# ЧАСТЬ 1. Детерминированная арифметика и самопроверка — без сети
# ============================================================================

# N=6: победителя нет в 14% партий, и это ЦЕЛИКОМ ничьи (случай из промпта).
STATS = [
    {"games": 2000, "num_players": 3, "win_rate_by_seat": {"0": 0.34, "1": 0.33, "2": 0.33},
     "no_winner_rate": 0.0, "end_reason_share": {"rounds_exhausted": 1.0},
     "rounds": {"mean": 3.0, "stdev": 0.1}, "duration_cv": 0.03,
     "action_share": {"prepare_pitch": 0.33, "pitch_product": 0.33, "invest_in_player": 0.34},
     "final_score_by_seat": {}, "avg_eliminated_per_game": 0.0, "score_gini": 0.11},
    {"games": 2000, "num_players": 6, "win_rate_by_seat": {"0": 0.17, "1": 0.16},
     "no_winner_rate": 0.14,
     "end_reason_share": {"rounds_exhausted": 0.86, "tie_unresolved": 0.14},
     "rounds": {"mean": 3.0, "stdev": 0.1}, "duration_cv": 0.03,
     "action_share": {"prepare_pitch": 0.33, "pitch_product": 0.33, "invest_in_player": 0.34},
     "final_score_by_seat": {}, "avg_eliminated_per_game": 0.0, "score_gini": 0.11},
]

META = {
    "tie_breaker": {"applicable": True, "present": False, "description": None},
    "actions_resolution": {"prepare_pitch": "deterministic",
                           "pitch_product": "subjective_judgment",
                           "invest_in_player": "subjective_judgment"},
    "win_paths": {"multiple": True, "paths": ["a", "b"]},
    # лишнее поле сайдкара: агенту его подавать нельзя
    "hand_exists": False,
}

bd = ev.reachability_breakdown(STATS)
checks["ничьи вычтены из недостижимости"] = (
    bd[1]["tie_share"] == 0.14 and bd[1]["true_unreachable"] == 0.0)
checks["чистая конфигурация без ничьих"] = bd[0]["unreachable_total"] == 0.0
checks["мягкие действия найдены"] = ev.soft_actions(META) == ["invest_in_player", "pitch_product"]
checks["сайдкар урезан до трёх полей"] = set(ev.readable_meta(META)) == {
    "tie_breaker", "actions_resolution", "win_paths"}

BASE_CHECKS = [
    {"id": "seat_fairness", "verdict": "ok", "confidence": "measured", "evidence": "откл. 3 п.п."},
    {"id": "win_reachability", "verdict": "ok", "confidence": "measured", "evidence": "ок"},
    {"id": "duration", "verdict": "ok", "confidence": "measured", "evidence": "rounds.mean=3.0"},
    {"id": "action_space", "verdict": "ok", "confidence": "measured", "evidence": "ровные доли"},
    {"id": "economy_runaway", "verdict": "ok", "confidence": "measured", "evidence": "gini=0.11"},
    {"id": "eliminations", "verdict": "ok", "confidence": "measured", "evidence": "0.0"},
]


def report_with(**over):
    rep = {"mode": "competitive", "configs_analyzed": [3, 6],
           "checks": [dict(c) for c in BASE_CHECKS], "flags": [],
           "scores": {"balance": 8, "win_reachability": 8},
           "score_rationale": "чисто", "priority_fixes": [],
           "covered_tests": [{"test": "2.3", "coverage": "full", "note": "ок"}],
           "notes_for_lenses": []}
    rep.update(over)
    return rep


def codes(issues):
    return [i["code"] for i in issues]


# --- 1а) ЛОВУШКА v3: ничья выдана за недостижимую победу ---------------------
bad = report_with(
    checks=[dict(c, verdict="problem", evidence="no_winner_rate=0.14 при N=6")
            if c["id"] == "win_reachability" else dict(c) for c in BASE_CHECKS],
    flags=[{"code": "unreachable_win", "where": "win_condition.threshold",
            "detail": "14% партий без победителя при N=6", "severity": "critical",
            "confidence": "measured", "affected_configs": [6]}],
    priority_fixes=[{"target": "core.win_condition.threshold",
                     "direction": "снизить порог", "closes": "unreachable_win"}])
_, issues = ev.validate(bad, STATS, META)
checks["ничья, выданная за недостижимость, поймана"] = "tie_counted_as_unreachable" in codes(issues)
checks["ошибка помечена важной"] = any(
    i["code"] == "tie_counted_as_unreachable" and i["severity"] == "error" for i in issues)

# --- 1б) корректный разбор той же ситуации не даёт ложных жалоб --------------
good = report_with(
    checks=[dict(c, verdict="problem",
                 evidence="tie_unresolved=0.14 при N=6; истинная недостижимость=0.0")
            if c["id"] == "win_reachability" else dict(c) for c in BASE_CHECKS],
    flags=[{"code": "tie_unresolved", "where": "win_condition",
            "detail": "14% партий при N=6 — неразрешённая ничья", "severity": "major",
            "confidence": "measured", "affected_configs": [6]}],
    priority_fixes=[{"target": "core.win_condition",
                     "direction": "добавить правило разрешения ничьей",
                     "closes": "tie_unresolved"}])
_, issues = ev.validate(good, STATS, META)
checks["корректный разбор без жалоб"] = issues == []

# --- 1в) ничьи есть, а флага нет ---------------------------------------------
_, issues = ev.validate(report_with(), STATS, META)
checks["незамеченные ничьи пойманы"] = "tie_not_flagged" in codes(issues)

# --- 1г) problem на мягком числе ---------------------------------------------
soft_problem = report_with(
    checks=[dict(c, verdict="problem", confidence="assumed",
                 evidence="action_share(pitch_product)=0.33")
            if c["id"] == "action_space" else dict(c) for c in BASE_CHECKS],
    flags=[{"code": "dominant_strategy", "where": "turn.actions", "detail": "0.33",
            "severity": "critical", "confidence": "assumed", "affected_configs": [6]}],
    priority_fixes=[{"target": "core.turn.actions", "direction": "убрать действие",
                     "closes": "dominant_strategy"}])
_, issues = ev.validate(soft_problem, STATS, META)
c = codes(issues)
checks["problem на мягком числе пойман"] = "problem_on_assumed" in c
checks["critical на мягком числе пойман"] = "critical_on_assumed" in c
checks["правка по мягкому числу поймана"] = "fix_on_assumed" in c

# --- 1д) структурные инварианты отчёта ---------------------------------------
_, issues = ev.validate(report_with(checks=BASE_CHECKS[:4]), STATS, META)
checks["пропущенные проверки пойманы"] = codes(issues).count("check_missing") == 2

_, issues = ev.validate(
    report_with(checks=BASE_CHECKS + [{"id": "softlocks", "verdict": "ok",
                                       "confidence": "measured", "evidence": "x"}]),
    STATS, META)
checks["седьмая проверка поймана"] = "check_extra" in codes(issues)

_, issues = ev.validate(
    report_with(flags=[{"code": "seat_advantage", "where": "setup", "detail": "x",
                        "severity": "major", "confidence": "measured"}]), STATS, META)
checks["флаг без проверки-источника пойман"] = "flag_without_check" in codes(issues)

_, issues = ev.validate(
    report_with(checks=[dict(c, verdict="problem", evidence="x") if c["id"] == "duration"
                        else dict(c) for c in BASE_CHECKS]), STATS, META)
checks["problem без флага пойман"] = "problem_without_flag" in codes(issues)

_, issues = ev.validate(report_with(mode="cooperative"), STATS, META)
checks["кооператив со счётом мест пойман"] = "coop_seat_fairness" in codes(issues)

_, issues = ev.validate(report_with(configs_analyzed=[3]), STATS, META)
checks["несовпадение конфигураций поймано"] = "configs_mismatch" in codes(issues)

_, issues = ev.validate(report_with(priority_fixes=[
    {"target": "сделать игру лучше", "direction": "", "closes": ""}]), STATS, META)
c = codes(issues)
checks["расплывчатая правка поймана"] = "fix_target_vague" in c and "fix_direction_missing" in c

# --- 1е) ничьи при tie_breaker.applicable=false -> расхождение со спекой ------
_, issues = ev.validate(
    good, STATS, {**META, "tie_breaker": {"applicable": False, "present": False}})
checks["ничья вопреки спеке поймана"] = "tie_vs_spec" in codes(issues)

# ============================================================================
# ЧАСТЬ 2. Разбор вывода скелета
# ============================================================================

OUT = ("ОТЧЁТ СИМУЛЯЦИИ\nВинрейт: ...\n"
       "JSON ДЛЯ ОЦЕНКИ БАЛАНСА — скопируйте всё ниже\n====\n"
       + json.dumps(STATS, ensure_ascii=False))
checks["stats вычленяются из вывода"] = runner.extract_stats(OUT)[1]["num_players"] == 6
checks["мусор не принимается за stats"] = not runner.looks_like_stats([{"foo": 1}])
checks["пустой код отбивается"] = runner.run_skeleton("")["ok"] is False

# реальный прогон настоящего шаблона (наш собственный код, не от модели)
with open("simulation/templates/game_skeleton.py", encoding="utf-8") as f:
    template_code = f.read().replace("GAMES_PER_CONFIG = 2000", "GAMES_PER_CONFIG = 30")
res = runner.run_skeleton(template_code, timeout=90)
checks["шаблон реально прогоняется"] = res["ok"] and len(res["stats"]) == 3
checks["в прогоне есть нужные поля"] = all(
    k in res["stats"][0] for k in ("num_players", "win_rate_by_seat", "end_reason_share"))
checks["таймаут срабатывает"] = runner.run_skeleton(
    "while True: pass", timeout=3)["ok"] is False

# ============================================================================
# ЧАСТЬ 3. Сквозной прогон через интерфейс
# ============================================================================

seen = {"has_diag": None, "has_breakdown": None, "has_soft": None, "meta_keys": None}
calls = {"eval": 0}

SPEC = {
    "game_spec": {
        "core": {"players": {"min": 3, "max": 6}, "mode": "competitive", "elimination": False,
                 "turn": {"order": "clockwise",
                          "actions": ["prepare_pitch", "pitch_product", "invest_in_player"]},
                 "resources": [], "win_condition": {"type": "most", "metric": "жетоны"},
                 "loss_condition": {"type": "rounds_exhausted"},
                 "catch_up": {"enabled": False}, "play_time": None,
                 "limits": {"max_rounds": 3}},
        "text": {"concept": "Инвестиции.", "full_rules": "правила", "components": []},
    },
    "diagnostic_meta": META,
    "gaps": [], "ambiguities": [], "source_format": "essay",
}


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "аналитик игрового баланса" in system:
            calls["eval"] += 1
            seen["has_diag"] = "DIAG_JSON" not in user
            seen["has_breakdown"] = "РАЗЛОЖЕНИЕ no_winner_rate" in user
            seen["has_soft"] = "pitch_product" in user and "МЯГКИМИ" in user
            seen["meta_keys"] = "hand_exists" not in user
            return json.dumps(good, ensure_ascii=False)

        if "инженер игровых симуляций" in system:
            return json.dumps({"simulatable": True, "player_counts": [3, 6],
                               "assumptions": ["питч смоделирован случайно"],
                               "code": "print('skeleton')"}, ensure_ascii=False)

        if "извлекатель структуры" in system:
            return json.dumps(SPEC, ensure_ascii=False)

        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Игра.", "map": [],
                    "questions": [{"id": 1, "question": "?", "why": "!", "type": "rules"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": "текст",
                    "author_clarifications": [], "still_open": [], "ready_to_proceed": True}
        return "Ответ.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import BalanceReport  # noqa: E402

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

# --- без статистики: экран просит её, агента не зовёт ------------------------
html = cl.get(f"/documents/{doc_id}/balance/1").get_data(as_text=True)
checks["без статистики агент не звался"] = calls["eval"] == 0
checks["экран просит статистику"] = "Нужна статистика прогона" in html
checks["предупреждение о запуске кода"] = "написанный языковой моделью" in html

# --- мусор вместо JSON отбивается -------------------------------------------
bad_paste = cl.post(f"/documents/{doc_id}/balance/1/stats", data={"stats": "не json"},
                    follow_redirects=True).get_data(as_text=True)
checks["не-JSON отбит"] = "не похоже на JSON" in bad_paste
wrong = cl.post(f"/documents/{doc_id}/balance/1/stats",
                data={"stats": json.dumps([{"foo": 1}])},
                follow_redirects=True).get_data(as_text=True)
checks["чужой JSON отбит"] = "не STATS_JSON" in wrong
checks["агент так и не звался"] = calls["eval"] == 0

# --- вставка корректной статистики -------------------------------------------
res_html = cl.post(f"/documents/{doc_id}/balance/1/stats",
                   data={"stats": json.dumps(STATS, ensure_ascii=False)},
                   follow_redirects=True).get_data(as_text=True)

checks["агент прогнан"] = calls["eval"] == 1
checks["DIAG_JSON не подавался"] = seen["has_diag"] is True
checks["разложение ушло в промпт"] = seen["has_breakdown"] is True
checks["мягкие действия ушли в промпт"] = seen["has_soft"] is True
checks["лишние поля сайдкара отсечены"] = seen["meta_keys"] is True

checks["разложение показано автору"] = "Почему в партии не было победителя" in res_html
checks["видно, что 14% — ничьи"] = "14.0%" in res_html
# В списке мягких действий должны быть ИМЕНА действий, а не свободные фразы из
# assumptions симуляциониста («питч смоделирован случайно») — иначе автор видит
# предложение там, где ожидает идентификатор.
soft_line = res_html.split("Действия с «мягкими» числами:")[1].split("</div>")[0]
checks["в мягких действиях только имена"] = (
    "pitch_product" in soft_line and "смоделирован случайно" not in soft_line)
checks["баллы показаны"] = "Баланс" in res_html and ">8<" in res_html
checks["флаг показан"] = "tie_unresolved" in res_html
checks["тяжесть по-русски"] = "существенно" in res_html
checks["правка показана"] = "правило разрешения ничьей" in res_html
checks["мягкие числа подсвечены"] = "мягк" in res_html
checks["закрытые тесты показаны"] = "тест 2.3" in res_html

with A.app.app_context():
    br = BalanceReport.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["статистика сохранена"] = len(br.stats()) == 2
    checks["источник — вставка"] = br.stats_source == BalanceReport.SOURCE_PASTED
    checks["отчёт сохранён"] = br.report()["mode"] == "competitive"
    checks["нарушений нет"] = br.issues() == []

dl = cl.get(f"/documents/{doc_id}/balance/1.json")
checks["Finding_balance качается"] = dl.status_code == 200 and "tie_unresolved" in dl.get_data(as_text=True)

# --- новая статистика обнуляет прежний вердикт -------------------------------
cl.post(f"/documents/{doc_id}/balance/1/stats",
        data={"stats": json.dumps(STATS[:1], ensure_ascii=False)}, follow_redirects=True)
checks["новая статистика переоценена"] = calls["eval"] == 2

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
