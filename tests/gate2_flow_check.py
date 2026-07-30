# -*- coding: utf-8 -*-
"""Проверка второго гейта (проход 3 понимателя v3) и четырёх статусов карты.

Что проверяется:
  • absent НЕ показывается вперемешку с пробелами и не порождает вопрос;
  • гейт предлагается ТОЛЬКО при критичных пробелах от извлеченца;
  • агент получает историю проходов 1-2 (иначе спросит уже отвеченное);
  • ответы гейта попадают в ПОДТВЕРЖДЁННЫЙ текст, и структура пересобирается
    с закрытым пробелом;
  • гейт однократный; simulation_blocking сохраняется;
  • подтверждённый JSON прохода 2 гейтом не перезаписывается (иначе извлеченец
    потеряет источник истины).
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402

calls = {"mirror": 0, "extract": 0, "gate2_ask": 0, "gate2_answer": 0}
seen = {"history_in_gate2": None, "gaps_in_gate2": None}

# Порог победы неизвестен на первом извлечении и становится известен после гейта.
threshold = {"value": None}


def _spec():
    gaps = []
    if threshold["value"] is None:
        gaps.append({"field": "win_condition.threshold",
                     "missing": "не назван порог победы",
                     "why_matters": "без него симулятор не поймёт, когда партия выиграна",
                     "critical": True})
    gaps.append({"field": "text.setting", "missing": "не описан сеттинг",
                 "why_matters": "мелочь для отчёта", "critical": False})
    return {
        "game_spec": {
            "core": {
                "players": {"min": 3, "max": 6}, "mode": "competitive", "elimination": False,
                "turn": {"order": "clockwise", "actions": ["pitch", "invest"]},
                "randomness": [{"type": "deck"}],
                "resources": [{"name": "жетоны", "scope": "personal", "start": 5, "goal": None}],
                "win_condition": {"type": "max_score", "metric": "жетоны",
                                  "threshold": threshold["value"]},
                "loss_condition": {"type": None}, "limits": {"max_rounds": None},
            },
            "text": {"concept": "Игра про инвестиции.", "components": [], "recommendations": []},
        },
        "gaps": gaps,
        "ambiguities": [{"field": "turn.actions", "phrase": "можно вложиться",
                         "readings": ["один раз за ход", "сколько угодно раз"]}],
        "source_format": "essay",
    }


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "извлекатель структуры" in system:
            calls["extract"] += 1
            return json.dumps(_spec(), ensure_ascii=False)

        # --- второй гейт (проход 3) ---
        if "ВТОРОЙ ГЕЙТ" in user:
            if "ОБРАЩЕНИЕ А" in user:
                calls["gate2_ask"] += 1
                seen["history_in_gate2"] = "ИСТОРИЯ ПРОХОДОВ 1-2" in user
                seen["gaps_in_gate2"] = "win_condition.threshold" in user
                data = {"phase": "gate2", "questions": [
                    {"id": 1, "question": "Сколько жетонов нужно для победы?",
                     "why": "без порога партия не завершается", "type": "rules"}],
                    "ready_to_proceed": False}
                return "Один вопрос.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"

            calls["gate2_answer"] += 1
            threshold["value"] = 12          # автор назвал порог — пробел закрыт
            data = {"phase": "gate2",
                    "resolved": [{"field": "win_condition.threshold",
                                  "answer": "победа при 12 жетонах"}],
                    "still_open": [{"field": "randomness.deck_size",
                                    "reason": "автор не знает", "critical": True}],
                    "simulation_blocking": True, "ready_to_proceed": True}
            return "Порог закрыт.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"

        # --- проходы 1-2 ---
        calls["mirror"] += 1
        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Игра про инвестиции.",
                    "map": [
                        {"node": "число игроков", "status": "ok", "note": "3-6"},
                        {"node": "метрика победы", "status": "ok", "note": "жетоны"},
                        {"node": "тратимые ресурсы", "status": "unclear",
                         "note": "не сказано, восполняются ли"},
                        {"node": "тип разрешения действий", "status": "ok",
                         "note": "питч — субъективное суждение"},
                        {"node": "тай-брейк", "status": "missing", "note": "равенство возможно"},
                        {"node": "карты на руках", "status": "absent", "note": "рук нет"},
                        {"node": "режимы сложности", "status": "absent", "note": "вариантов нет"},
                        {"node": "механика догоняния", "status": "absent", "note": "нет намёка"},
                    ],
                    "questions": [
                        {"id": 1, "question": "Восполняются ли жетоны между раундами?",
                         "why": "иначе экономика считается неверно", "type": "rules"},
                        {"id": 2, "question": "Что при равенстве жетонов?",
                         "why": "иначе ничья маскируется под отсутствие победителя",
                         "type": "diagnostic"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": "правила игры",
                    "author_clarifications": [
                        {"question_id": 1, "question": "Восполняются ли жетоны?",
                         "answer": "нет, копятся"}],
                    "still_open": [{"item": "тай-брейк", "reason": "не ответил", "critical": True}],
                    "ready_to_proceed": True}
        return "Ответ агента.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import GameSpec, MirrorSession  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

checks = {}

# --- проход 1: четыре статуса ---------------------------------------------------
html = c.get(f"/documents/{doc_id}/mirror/1").get_data(as_text=True)
checks["карта показывает пробел"] = "нет в тексте" in html and "тай-брейк" in html
checks["absent свёрнут отдельной строкой"] = "Этих механик в игре нет" in html
checks["absent-узлы перечислены"] = "карты на руках" in html and "режимы сложности" in html
checks["absent не в основной сетке"] = html.count("механики нет") == 0
checks["вопрос-диагностика помечен"] = "диагностика" in html
checks["в карте только применимые узлы"] = ">5<" in html  # 8 узлов - 3 absent

# --- проход 2: critical в непокрытых --------------------------------------------
reply = c.post(f"/documents/{doc_id}/mirror/1/reply",
               data={"answer": "жетоны копятся, тай-брейк не придумал"},
               follow_redirects=True).get_data(as_text=True)
checks["непокрытый пробел помечен критичным"] = "критично" in reply
checks["сказано про второй гейт"] = "второй гейт" in reply.lower()

# --- структура: гейт предложен ---------------------------------------------------
spec_html = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)
checks["гейт предложен"] = "Открыть второй гейт" in spec_html
checks["названо число критичных пробелов"] = "1 критичный пробел" in spec_html
checks["некритичный пробел гейт не тянет"] = "text.setting" not in spec_html.split("Второй гейт")[1].split("Принимаете")[0]

# --- обращение А: вопросы --------------------------------------------------------
gate_html = c.get(f"/documents/{doc_id}/gate2/1").get_data(as_text=True)
checks["агент получил историю проходов"] = seen["history_in_gate2"] is True
checks["агент получил критичный пробел"] = seen["gaps_in_gate2"] is True
checks["точечный вопрос показан"] = "Сколько жетонов нужно для победы" in gate_html
checks["показан сам пробел"] = "win_condition.threshold" in gate_html
checks["сказано, что гейт однократный"] = "один раз" in gate_html
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["статус гейта asked"] = ms.gate2_status == MirrorSession.GATE2_ASKED
    confirmed_before = ms.last_json_dict()
    checks["подтверждённый JSON цел"] = confirmed_before.get("phase") == "confirmed"
    # бюджет гейта уже потрачен обращением А, но вопросы ещё ждут ответа
    checks["бюджет гейта потрачен на вопросах"] = ms.can_gate2 is False

# Пока вопросы не отвечены, страница структуры обязана вести к открытому гейту:
# иначе автор теряет вход в гейт (бюджет уже израсходован, а ответа ещё нет).
spec_asked = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)
checks["структура ведёт к открытому гейту"] = "Перейти к вопросам гейта" in spec_asked
checks["не предлагает открыть гейт заново"] = "Открыть второй гейт" not in spec_asked

extract_before = calls["extract"]

# --- обращение Б: ответ автора → пересборка структуры ----------------------------
done = c.post(f"/documents/{doc_id}/gate2/1/reply",
              data={"answer": "для победы нужно 12 жетонов"},
              follow_redirects=True).get_data(as_text=True)
checks["ответ разобран"] = calls["gate2_answer"] == 1
checks["структура пересобрана"] = calls["extract"] == extract_before + 1
checks["флеш о пересборке"] = "структура пересобрана" in done

with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    confirmed = ms.last_json_dict()
    clar_text = json.dumps(confirmed.get("author_clarifications"), ensure_ascii=False)
    checks["ответ гейта в подтверждённом тексте"] = "12 жетонах" in clar_text
    checks["прошлые уточнения не потеряны"] = "копятся" in clar_text
    checks["подтверждённый JSON не перезаписан гейтом"] = confirmed.get("phase") == "confirmed"
    checks["пробел закрыт в структуре"] = (
        gs.spec_dict()["game_spec"]["core"]["win_condition"]["threshold"] == 12)
    checks["критичных пробелов больше нет"] = not [
        g for g in gs.spec_dict()["gaps"] if g.get("critical")]
    checks["статус гейта done"] = ms.gate2_status == MirrorSession.GATE2_DONE
    checks["simulation_blocking сохранён"] = ms.simulation_blocking is True
    checks["гейт больше не доступен"] = ms.can_gate2 is False

# --- гейт не предлагается второй раз ---------------------------------------------
spec_after = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)
checks["гейт не предлагается снова"] = "Открыть второй гейт" not in spec_after
again = c.get(f"/documents/{doc_id}/gate2/1", follow_redirects=True).get_data(as_text=True)
checks["повторный вход отбит"] = ("Критичных пробелов нет" in again
                                  or "уже пройден" in again)
checks["агента больше не звали"] = calls["gate2_ask"] == 1

# --- после приёмки гейт закрыт ---------------------------------------------------
c.post(f"/documents/{doc_id}/spec/1/accept", follow_redirects=True)
blocked = c.get(f"/documents/{doc_id}/gate2/1", follow_redirects=True).get_data(as_text=True)
checks["после приёмки гейт не открыть"] = ("уже принята" in blocked
                                           or "Критичных пробелов нет" in blocked)

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
