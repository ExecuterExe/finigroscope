# -*- coding: utf-8 -*-
"""Проверка цикла раундов у «Понимания игры» + передачи данных извлеченцу.

Поддельный провайдер имитирует упрямого агента, который КАЖДЫЙ раз просит ещё
уточнений (ready_to_proceed=false + questions) — так проверяется, что цикл
всё равно конечен: сервер сам закрывает сверку на MAX_ROUNDS. Затем проверяется
извлечение game_spec и его отдача файлом.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402

seen_final_marker = []


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        # извлеченец: в его системном промпте есть роль «извлекатель структуры»
        if "извлекатель структуры" in system:
            spec = {
                "game_spec": {
                    "core": {
                        "players": {"min": 3, "max": 6},
                        "mode": "competitive",
                        "elimination": False,
                        "turn": {"order": "clockwise", "actions": ["read_case", "pick_card"]},
                        "randomness": [{"type": "deck", "outcomes": {}}],
                        "resources": [{"name": "reputation", "scope": "personal", "start": 0, "goal": None}],
                        "win_condition": {"type": "max_score", "metric": "reputation", "threshold": None},
                        "loss_condition": {"type": None},
                        "limits": {"max_rounds": None},
                    },
                    "text": {
                        "concept": "Психологический детектив про когнитивные искажения.",
                        "setting": "Расследование финансовых кейсов.",
                        "components": [
                            {"name": "карта дела", "qty": 50, "material": None,
                             "function": "описывает кейс, который разбирает детектив"},
                            {"name": "жетон репутации", "qty": 50, "material": None, "function": None},
                        ],
                        "recommendations": ["играть от 3 человек"],
                    },
                },
                "gaps": [
                    {"field": "text.components[1].function", "critical": False,
                     "source": "unresolved_by_author", "missing": "функция жетона не пояснена",
                     "why_matters": "линзы не оценят артефакт"},
                    {"field": "win_condition.threshold", "critical": True, "source": "extraction",
                     "missing": "порог победы не назван", "why_matters": "симулятор не поймёт конец"},
                ],
                "ambiguities": [{"field": "randomness", "phrase": "карты тасуются",
                                 "readings": ["с возвратом?", "без возврата?"]}],
                "source_format": "essay",
            }
            return json.dumps(spec, ensure_ascii=False)

        # агент понимания: ВСЕГДА просит ещё (проверяем предохранитель цикла)
        seen_final_marker.append("ПОСЛЕДНИЙ раунд" in user)
        data = {
            "phase": "mirror",
            "understanding": "Понял игру частично.",
            "map": [{"node": "компоненты и материалы", "status": "unclear", "note": "нет примеров карт"}],
            "questions": [{"id": 1, "question": "Приведите примеры карт.",
                           "why": "нужно для баланса", "type": "components"}],
            "ready_to_proceed": False,
        }
        return "Вопросы ниже.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import GameSpec, MirrorSession  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

c.get(f"/documents/{doc_id}/mirror/1")  # проход 1


def phase_round():
    with A.app.app_context():
        ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
        return ms.phase, ms.round, ms.ready_to_proceed


print("после прохода 1:", phase_round())
assert phase_round()[:2] == ("mirror", 1)

# --- раунд 2: агент снова просит уточнений -> цикл продолжается ---------------
c.post(f"/documents/{doc_id}/mirror/1/reply", data={"answer": "ответ 1"}, follow_redirects=True)
print("после ответа 1:", phase_round())
assert phase_round()[:2] == ("mirror", 2), "должен быть переход на раунд 2"

# --- раунд 3 (последний): сервер обязан закрыть сверку, даже если агент просит ещё ---
c.post(f"/documents/{doc_id}/mirror/1/reply", data={"answer": "ответ 2"}, follow_redirects=True)
print("после ответа 2:", phase_round())
assert phase_round()[:2] == ("mirror", 3), "должен быть переход на раунд 3 (последний)"

c.post(f"/documents/{doc_id}/mirror/1/reply", data={"answer": "ответ 3"}, follow_redirects=True)
phase, rnd, ready = phase_round()
print("после ответа 3 (лимит):", (phase, rnd, ready))
assert phase == "confirmed", "на последнем раунде сверка ОБЯЗАНА закрыться"
assert ready is True, "принудительное закрытие должно выставить ready_to_proceed"
assert seen_final_marker[-1] is True, "агенту должен был уйти маркер последнего раунда"
print("маркер 'последний раунд' по раундам:", seen_final_marker)

# --- извлечение game_spec ------------------------------------------------------
html = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)
checks = {
    "страница структуры отрисована": "Структура игры" in html,
    "ядро core показано": "spec-grid" in html and "3–6" in html,
    "таблица компонентов": "comp-table" in html and "карта дела" in html,
    "пробелы показаны": "gap-item" in html and "порог победы не назван" in html,
    "критичный пробел выделен": "gap-critical" in html,
    "неоднозначности показаны": "Неоднозначности" in html,
    "кнопка скачивания json": "spec/1.json" in html,
}
for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values())

with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    assert gs.spec_json and gs.error is None

dl = c.get(f"/documents/{doc_id}/spec/1.json")
print("скачивание json:", dl.status_code, dl.headers.get("Content-Disposition"))
assert dl.status_code == 200 and "attachment" in dl.headers.get("Content-Disposition", "")

print("\nВСЁ ОК")
