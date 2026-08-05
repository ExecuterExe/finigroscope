# -*- coding: utf-8 -*-
"""Проверка приёмки game_spec автором: описание, «принимаю», единственная правка.

Ключевое, что проверяется: правка имеет ОТДЕЛЬНЫЙ бюджет от раундов понимания
(раунды могут быть исчерпаны, а правка всё равно доступна) и она ровно одна —
вторая попытка должна отбиваться.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402

calls = {"mirror": 0, "extract": 0, "revision_mode": []}


def _spec(threshold):
    return {
        "game_spec": {
            "core": {
                "players": {"min": 3, "max": 6},
                "mode": "competitive",
                "elimination": False,
                "turn": {"order": "clockwise", "actions": ["read_case", "pick_card"]},
                "randomness": [{"type": "deck"}],
                "resources": [{"name": "репутация", "scope": "personal", "start": 0, "goal": None}],
                "win_condition": {"type": "max_score", "metric": "репутация", "threshold": threshold},
                "loss_condition": {"type": None},
                "limits": {"max_rounds": None},
            },
            "text": {
                "concept": "Детектив про когнитивные искажения.",
                "components": [{"name": "карта дела", "qty": 50, "material": None,
                                "function": "описывает кейс"}],
                "recommendations": [],
            },
        },
        "gaps": [], "ambiguities": [], "source_format": "essay",
    }


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "извлекатель структуры" in system:
            calls["extract"] += 1
            # после правки порог победы становится известен
            return json.dumps(_spec(5 if calls["extract"] > 1 else None), ensure_ascii=False)

        calls["mirror"] += 1
        calls["revision_mode"].append("РЕЖИМ ПРАВКИ" in user)
        # Проход 1 по протоколу всегда останавливается и ждёт автора.
        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Детектив про искажения.",
                    "map": [{"node": "условие победы", "status": "unclear", "note": "порог не назван"}],
                    "questions": [{"id": 1, "question": "Какой порог победы?",
                                   "why": "нужно для симуляции", "type": "rules"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": "текст", "author_clarifications": [],
                    "still_open": [], "ready_to_proceed": True}
        return "Ответ агента.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import GameSpec  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

c.get(f"/documents/{doc_id}/mirror/1")   # проход 1 — агент задал вопрос, ждёт автора
c.post(f"/documents/{doc_id}/mirror/1/reply",
       data={"answer": "всё ясно, продолжаем"}, follow_redirects=True)  # → confirmed
html = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)

checks = {
    "текстовое описание есть": "Так сервис понимает вашу игру" in html,
    "описание человеческим языком": "Играют от 3 до 6 человек." in html,
    "видно незаполненное поле": "порог победы — не указано" in html,
    "блок приёмки показан": "Принимаете это описание игры?" in html,
    "кнопка принять": "Да, принимаю описание" in html,
    "кнопка поправить": "Нет, нужно поправить" in html,
}

# --- правка (одна) -------------------------------------------------------------
c.post(f"/documents/{doc_id}/spec/1/revise",
       data={"note": "побеждает тот, кто первым набрал 5 очков"}, follow_redirects=True)

with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["правка засчитана"] = gs.revisions == 1
    checks["статус revised"] = gs.status == GameSpec.STATUS_REVISED
    checks["правка сохранена"] = "5 очков" in (gs.revision_note or "")
    checks["больше править нельзя"] = gs.can_revise is False
    checks["структура пересобрана"] = json.loads(gs.spec_json)["game_spec"]["core"]["win_condition"]["threshold"] == 5

checks["агент получил режим правки"] = calls["revision_mode"][-1] is True
checks["извлечение прогнали дважды"] = calls["extract"] == 2

after = c.get(f"/documents/{doc_id}/spec/1").get_data(as_text=True)
checks["правка показана автору"] = "Ваша правка учтена" in after
checks["форма правки скрыта"] = "Нет, нужно поправить" not in after
checks["новый порог в описании"] = "порог победы — 5" in after

# --- вторая правка должна отбиваться -------------------------------------------
c.post(f"/documents/{doc_id}/spec/1/revise", data={"note": "ещё правка"}, follow_redirects=True)
with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["вторая правка отклонена"] = gs.revisions == 1 and gs.revision_note == "побеждает тот, кто первым набрал 5 очков"

# --- приёмка -------------------------------------------------------------------
accepted = c.post(f"/documents/{doc_id}/spec/1/accept", follow_redirects=True).get_data(as_text=True)
with A.app.app_context():
    gs = GameSpec.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["статус accepted"] = gs.status == GameSpec.STATUS_ACCEPTED
checks["баннер принятия"] = "Вы приняли эту структуру" in accepted
checks["блок приёмки убран"] = "Принимаете это описание игры?" not in accepted

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print("\nВСЁ ОК")
