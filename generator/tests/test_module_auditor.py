# -*- coding: utf-8 -*-
"""Тесты агента «Аудитор модуля».

Сетевых вызовов нет: клиент модели подменяется заглушкой FakeLLM, которая
отдаёт заранее записанные ответы. Проверяется то, за что отвечает код, а не
модель: сборка сообщения, разбор ответа, пересчёт passed, различение n/a и ok,
повторы и стыковка с оркестратором.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm                                    # noqa: E402
import runlog                                 # noqa: E402
from agents import module_auditor as auditor  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------
# Оснастка
# --------------------------------------------------------------------------

class FakeLLM(object):
    """Подмена llm: отдаёт заготовленные ответы по очереди.

    Элемент списка — либо словарь-ответ, либо исключение, которое надо бросить
    (так проверяются повторы).
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("Заглушку позвали больше раз, чем заготовлено ответов")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"data": copy.deepcopy(item), "model": "fake/model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "duration": 0.01}

    @property
    def last_user_message(self):
        return self.calls[-1]["messages"][1]["content"]


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    """Тесты не пишут журнал на диск и не спят между повторами."""
    monkeypatch.setattr(runlog, "record", lambda event, console=True: event)
    monkeypatch.setattr(auditor.time, "sleep", lambda seconds: None)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run_fixture(fixture):
    client = FakeLLM([fixture["llm_response"]])
    result = auditor.audit_module(
        phase=fixture["phase"],
        module=fixture["module"],
        params=fixture["params"],
        previous_modules=fixture.get("previous_modules"),
        run_id="test",
        llm_client=client)
    return result, client


def items_with(result, status):
    return [row["item"] for row in result.map if row["status"] == status]


# --------------------------------------------------------------------------
# 1. Сборка пользовательского сообщения по проходам
# --------------------------------------------------------------------------

MECHANICS_PARAMS = load_fixture("mechanics_elimination.json")["params"]


def test_mechanics_message_has_own_checklist_and_no_previous_section():
    message = auditor.build_user_message(
        "mechanics", {"title": "модуль"}, MECHANICS_PARAMS, previous_modules=[])

    for item in auditor.phase_config("mechanics")["checklist"]:
        assert item["id"] in message

    # чужие пункты не должны просачиваться: правило 1 промпта
    assert "genre_world_match" not in message
    assert "complexity_match" not in message
    assert "ПРИНЯТЫЕ РАНЕЕ МОДУЛИ" not in message


def test_mechanics_message_carries_only_relevant_params():
    message = auditor.build_user_message(
        "mechanics", {}, dict(MECHANICS_PARAMS, world=["Фэнтези"], story="антураж"))

    subset = auditor.relevant_params("mechanics", MECHANICS_PARAMS)
    assert "age_group" in subset and "components" in subset
    # world и story относятся к проходу story, здесь их быть не должно
    assert "world" not in subset
    assert "story" not in subset
    assert '"world"' not in message.split("## ЧЕК-ЛИСТ")[0]


def test_story_message_includes_previous_modules_and_layer_two():
    fixture = load_fixture("story_duplicate_component.json")
    message = auditor.build_user_message(
        "story", fixture["module"], fixture["params"], fixture["previous_modules"])

    assert "ПРИНЯТЫЕ РАНЕЕ МОДУЛИ" in message
    assert "жетон-ключ" in message
    assert "consistency_with_previous_modules" in message
    assert "genre_world_match" in message
    assert "elimination_respected" not in message


def test_features_message_lists_both_previous_modules():
    fixture = load_fixture("features_elimination_conflict.json")
    message = auditor.build_user_message(
        "features", fixture["module"], fixture["params"], fixture["previous_modules"])

    assert "mechanics" in message and "story" in message
    assert "complexity_match" in message


def test_message_marks_inapplicable_items_for_na():
    """Модель должна видеть, что предпосылка пункта не выполнена."""
    params = {"complexity": "низкая", "adaptation": False,
              "player_count": {"min": 2, "max": 4}, "elimination": False,
              "catch_up": True}
    message = auditor.build_user_message("features", {}, params)

    line = [l for l in message.splitlines() if "adaptation_respected" in l][0]
    assert "НЕ применим" in line and "n/a" in line

    line = [l for l in message.splitlines() if "complexity_match" in l][0]
    assert "зависит от" not in line


def test_unknown_phase_is_rejected():
    with pytest.raises(auditor.AuditError) as error:
        auditor.build_user_message("balance", {}, {})
    assert "balance" in str(error.value)


def test_new_phase_needs_only_config(monkeypatch):
    """Четвёртый проход добавляется правкой конфига, код не меняется."""
    extended = copy.deepcopy(auditor.checklists())
    extended["phases"]["balance"] = {
        "order": 4, "title": "Проверка баланса", "module_label": "МОДУЛЬ БАЛАНСА",
        "consistency": False, "llm": {"tier": "flash"},
        "relevant_params": ["complexity"],
        "checklist": [{"id": "curve_ok", "item": "Кривая сложности ровная",
                       "applies_when": None}],
    }
    monkeypatch.setattr(auditor, "checklists", lambda: extended)

    message = auditor.build_user_message("balance", {}, {"complexity": "низкая"})
    assert "curve_ok" in message
    assert "curve_ok" in auditor.known_item_ids("balance")


# --------------------------------------------------------------------------
# 2. Применимость пунктов
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rule, params, expected", [
    (None, {}, True),
    ({"param": "adaptation", "equals": True}, {"adaptation": False}, False),
    ({"param": "adaptation", "equals": True}, {"adaptation": True}, True),
    ({"param": "elimination", "equals": False}, {"elimination": False}, True),
    ({"param": "catch_up", "equals": True}, {"catch_up": False}, False),
    ({"param": "purpose", "contains": "Обучение"}, {"purpose": ["Развлечение"]}, False),
    ({"param": "purpose", "contains": "Обучение"},
     {"purpose": ["Обучение", "Развлечение"]}, True),
    ({"param": "world", "truthy": True}, {"world": []}, False),
])
def test_applies_rules(rule, params, expected):
    assert auditor.applies(rule, params) is expected


# --------------------------------------------------------------------------
# 3. Пересчёт passed
# --------------------------------------------------------------------------

def base_response(phase, statuses, issues=None, passed=True):
    """Полный ответ по всем пунктам прохода: по умолчанию всё ok."""
    rows = []
    for item in auditor.phase_config(phase)["checklist"]:
        rows.append({"item": item["id"],
                     "status": statuses.get(item["id"], "ok"),
                     "note": "тест"})
    return {
        "phase": phase, "map": rows,
        "consistency": {"applicable": False, "conflicts": []},
        "issues": issues or [],
        "cleaned_module": {"title": "модуль"},
        "passed": passed,
        "summary": "тест",
    }


def test_passed_recomputed_when_model_lies():
    """Модель прислала passed=true, хотя в map есть violation."""
    data = base_response(
        "mechanics", {"elimination_respected": "violation"},
        issues=[{"checklist_item": "elimination_respected", "severity": "critical",
                 "explanation": "выбывание есть"}],
        passed=True)

    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)

    assert data["passed"] is False
    assert any("passed=True" in a and "False" in a for a in anomalies)


def test_passed_true_when_only_concerns():
    data = base_response(
        "mechanics", {"catch_up_respected": "concern"},
        issues=[{"checklist_item": "catch_up_respected", "severity": "minor",
                 "explanation": "формулировка расплывчата"}],
        passed=True)

    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)

    assert data["passed"] is True
    assert anomalies == []


def test_violation_without_issue_is_anomaly():
    data = base_response("mechanics", {"elimination_respected": "violation"},
                         issues=[], passed=False)
    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)
    assert any("не попало в issues" in a for a in anomalies)


def test_unknown_checklist_item_is_anomaly():
    data = base_response("mechanics", {})
    data["map"].append({"item": "выдуманный_пункт", "status": "concern", "note": ""})
    data["issues"].append({"checklist_item": "выдуманный_пункт",
                           "severity": "minor", "explanation": "..."})

    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)

    assert any("которого нет в чек-листе" in a for a in anomalies)
    assert any("вне чек-листа" in a for a in anomalies)
    assert data["passed"] is True   # аномалия не блокирует прохождение


def test_missing_checklist_item_is_anomaly():
    data = base_response("mechanics", {})
    data["map"] = [row for row in data["map"] if row["item"] != "genre_match"]
    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)
    assert any("не хватает пунктов" in a and "genre_match" in a for a in anomalies)


# --------------------------------------------------------------------------
# 4. n/a не путается с ok
# --------------------------------------------------------------------------

def test_na_item_never_reaches_issues():
    """n/a — не находка. Если модель положила его в issues, запись убирается."""
    data = base_response("features", {"adaptation_respected": "n/a"},
                         issues=[{"checklist_item": "adaptation_respected",
                                  "severity": "minor",
                                  "explanation": "адаптация не учтена"}])
    params = {"complexity": "низкая", "adaptation": False,
              "player_count": {"min": 2, "max": 4}, "elimination": False,
              "catch_up": True}

    anomalies = auditor.recompute_passed(data, "features", params)

    assert data["issues"] == []
    assert any("помечен n/a, но попал в issues" in a for a in anomalies)


def test_ok_instead_of_na_is_anomaly():
    """Главная ловушка: пункт неприменим, а модель отметила его выполненным."""
    data = base_response("features", {"adaptation_respected": "ok"})
    params = {"complexity": "низкая", "adaptation": False,
              "player_count": {"min": 2, "max": 4}, "elimination": False,
              "catch_up": True}

    anomalies = auditor.recompute_passed(data, "features", params)

    assert any("неприменим по параметрам" in a and "n/a" in a for a in anomalies)


def test_na_on_applicable_item_is_anomaly():
    data = base_response("features", {"adaptation_respected": "n/a"})
    params = {"complexity": "низкая", "adaptation": True,
              "player_count": {"min": 2, "max": 4}, "elimination": False,
              "catch_up": True}

    anomalies = auditor.recompute_passed(data, "features", params)

    assert any("применим по параметрам, но помечен n/a" in a for a in anomalies)


def test_ok_item_in_issues_is_removed():
    data = base_response("mechanics", {},
                         issues=[{"checklist_item": "genre_match",
                                  "severity": "minor", "explanation": "..."}])
    anomalies = auditor.recompute_passed(data, "mechanics", MECHANICS_PARAMS)
    assert data["issues"] == []
    assert any("помечен ok, но попал в issues" in a for a in anomalies)


# --------------------------------------------------------------------------
# 5. Golden-фикстуры: по одной на проход
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "mechanics_elimination.json",
    "story_age_content.json",
    "features_elimination_conflict.json",
])
def test_golden_violation_fixtures(filename):
    fixture = load_fixture(filename)
    expect = fixture["expect"]
    result, _ = run_fixture(fixture)

    assert result.passed is expect["passed"]
    assert items_with(result, "violation") == expect["violation_items"]

    critical = [i["checklist_item"] for i in result.issues
                if i["severity"] == "critical"]
    assert critical == expect["critical_items"]

    for item_id in expect.get("na_items", []):
        assert item_id in items_with(result, "n/a")

    assert len(result.anomalies) == expect["anomalies"], result.anomalies
    # каждый пункт чек-листа получил статус
    assert len(result.map) == len(auditor.phase_config(fixture["phase"])["checklist"])


def test_golden_layer_two_duplicate_component():
    fixture = load_fixture("story_duplicate_component.json")
    expect = fixture["expect"]
    result, _ = run_fixture(fixture)

    assert result.passed is True
    assert result.consistency["applicable"] is True
    assert len(result.consistency["conflicts"]) >= expect["conflicts_min"]

    text = " ".join(c["conflict"] for c in result.consistency["conflicts"]).lower()
    for mention in expect["conflict_mentions"]:
        assert mention.lower() in text

    minor = [i["checklist_item"] for i in result.issues if i["severity"] == "minor"]
    assert minor == expect["minor_items"]
    assert result.anomalies == []


def test_golden_na_fixture():
    fixture = load_fixture("features_adaptation_na.json")
    result, _ = run_fixture(fixture)

    statuses = {row["item"]: row["status"] for row in result.map}
    assert statuses["adaptation_respected"] == "n/a"
    assert statuses["adaptation_respected"] != "ok"
    assert "adaptation_respected" in statuses          # пункт присутствует в map

    assert result.issues == []
    assert result.passed is True
    assert result.anomalies == []


# --------------------------------------------------------------------------
# 6. Проверка структуры и повторы
# --------------------------------------------------------------------------

def test_invalid_json_structure_triggers_retry_with_reason():
    broken = {"phase": "mechanics", "map": []}       # не проходит схему
    good = base_response("mechanics", {})
    client = FakeLLM([broken, good])

    result = auditor.audit_module("mechanics", {}, MECHANICS_PARAMS,
                                  run_id="test", llm_client=client)

    assert result.attempts == 2
    assert "Ответ отклонён проверкой" in client.calls[1]["messages"][1]["content"]


def test_bad_status_value_is_rejected():
    data = base_response("mechanics", {})
    data["map"][0]["status"] = "почти ок"
    problems = auditor.check_structure(data, "mechanics", [])
    assert problems and "схеме" in problems[0]


def test_bad_severity_value_is_rejected():
    data = base_response("mechanics", {},
                         issues=[{"checklist_item": "genre_match",
                                  "severity": "blocker", "explanation": "..."}])
    problems = auditor.check_structure(data, "mechanics", [])
    assert problems and "схеме" in problems[0]


def test_consistency_skipped_although_previous_given_is_rejected():
    """Слой 2 нельзя молча пропустить: ради него проход и добавлен."""
    data = base_response("story", {})
    data["consistency"] = {"applicable": False, "conflicts": []}
    previous = [{"phase": "mechanics", "module": {"title": "..."}}]

    problems = auditor.check_structure(data, "story", previous)

    assert problems and "слоя 2" in problems[0]


def test_consistency_skip_triggers_retry():
    fixture = load_fixture("story_duplicate_component.json")
    lazy = copy.deepcopy(fixture["llm_response"])
    lazy["consistency"] = {"applicable": False, "conflicts": []}
    client = FakeLLM([lazy, fixture["llm_response"]])

    result = auditor.audit_module("story", fixture["module"], fixture["params"],
                                  fixture["previous_modules"], run_id="test",
                                  llm_client=client)

    assert result.attempts == 2
    assert result.consistency["applicable"] is True


def test_network_error_retries_then_succeeds():
    good = base_response("mechanics", {})
    client = FakeLLM([llm.LLMError("сеть недоступна"), good])

    result = auditor.audit_module("mechanics", {}, MECHANICS_PARAMS,
                                  run_id="test", llm_client=client)

    assert result.attempts == 2
    assert result.passed is True


def test_gives_up_after_retry_budget():
    client = FakeLLM([llm.LLMError("сеть")] * 3)

    with pytest.raises(auditor.AuditError) as error:
        auditor.audit_module("mechanics", {}, MECHANICS_PARAMS,
                             run_id="test", llm_client=client)

    assert "3 попыт" in str(error.value)
    assert client.responses == []


def test_phase_mismatch_is_rejected():
    data = base_response("mechanics", {})
    data["phase"] = "story"
    problems = auditor.check_structure(data, "mechanics", [])
    assert problems and "phase" in problems[0]


# --------------------------------------------------------------------------
# 7. Параметры вызова модели из конфига
# --------------------------------------------------------------------------

def test_thinking_flag_comes_from_config_not_code():
    for phase, expected in [("mechanics", False), ("story", True), ("features", True)]:
        assert auditor.phase_config(phase)["llm"]["thinking"] is expected


def test_llm_options_are_passed_through():
    fixture = load_fixture("story_duplicate_component.json")
    client = FakeLLM([fixture["llm_response"]])

    auditor.audit_module("story", fixture["module"], fixture["params"],
                         fixture["previous_modules"], run_id="test",
                         llm_client=client)

    kwargs = client.calls[0]["kwargs"]
    options = auditor.phase_config("story")["llm"]
    assert kwargs["thinking"] is options["thinking"]
    assert kwargs["tier"] == options["tier"]
    assert kwargs["temperature"] == options["temperature"]
    assert kwargs["timeout"] == options["timeout"]


# --------------------------------------------------------------------------
# 8. Стыковка с оркестратором
# --------------------------------------------------------------------------

def test_chain_collects_cleaned_modules():
    mechanics = load_fixture("features_adaptation_na.json")["previous_modules"][0]
    chain = auditor.ModuleChain()
    assert chain.previous() == []

    fixture = load_fixture("story_duplicate_component.json")
    result, _ = run_fixture(fixture)
    chain.accept(result)

    assert len(chain) == 1
    assert chain.phases() == ["story"]
    # в цепочку уходит именно cleaned_module, а не сырой модуль
    assert chain.previous()[0]["module"] == result.cleaned_module
    assert chain.previous()[0]["module"] != fixture["module"]
    assert mechanics["phase"] == "mechanics"


def test_chain_rejects_failed_module():
    fixture = load_fixture("mechanics_elimination.json")
    result, _ = run_fixture(fixture)
    assert result.passed is False

    with pytest.raises(auditor.AuditError):
        auditor.ModuleChain().accept(result)


def test_result_to_dict_is_json_serializable():
    fixture = load_fixture("features_adaptation_na.json")
    result, _ = run_fixture(fixture)
    text = json.dumps(result.to_dict(), ensure_ascii=False)
    assert '"passed": true' in text
