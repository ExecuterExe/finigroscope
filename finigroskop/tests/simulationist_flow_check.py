# -*- coding: utf-8 -*-
"""Проверка агента «Симуляционист»: принятая структура → скелет game_skeleton.py.

Ключевое, что проверяется:
  • экран скелета доступен ТОЛЬКО после приёмки структуры (accepted);
  • при симулируемой игре показывается код, допущения, число игроков; файл качается;
  • при несимулируемой — причина и список недостающего, кода нет;
  • сам код сервисом НЕ исполняется (на экране нет статистик).
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402

FILLED_CODE = (
    "import random\n"
    "def setup_game(num_players, rng, config):\n"
    "    return None\n"
    "# заполнено симуляционистом\n"
)

# Управляем веткой из теста: True — симулируема, False — нет.
scenario = {"simulatable": True}
calls = {"mirror": 0, "extract": 0, "sim": 0}


def _spec():
    return {
        "game_spec": {
            "core": {
                "players": {"min": 2, "max": 4},
                "mode": "competitive",
                "elimination": True,
                "turn": {"order": "clockwise", "actions": ["move", "invest"]},
                "randomness": [{"type": "d6"}],
                "resources": [{"name": "деньги", "scope": "personal", "start": 10, "goal": None}],
                "win_condition": {"type": "reach", "metric": "position", "threshold": 24},
                "loss_condition": {"type": "bankrupt"},
                "limits": {"max_rounds": 200},
            },
            "text": {"concept": "Денежная гонка.", "components": [], "recommendations": []},
        },
        "gaps": [], "ambiguities": [], "source_format": "essay",
    }


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        if "инженер игровых симуляций" in system:
            calls["sim"] += 1
            if scenario["simulatable"]:
                # Формат v6: метаданные JSON (без code) + код ОТДЕЛЬНЫМ блоком.
                meta = {"simulatable": True, "player_counts": [2, 3, 4],
                        "pattern": "race", "manual_turn_order": False,
                        "assumptions": ["исход инвестиции смоделирован случайно"],
                        "subjective_actions": ["invest"],
                        "coalition_expressible": False,
                        "coalition_note": "адресных действий нет",
                        "metric_responds_immediately": True, "fixed_length": False,
                        "end_reasons_used": ["goal_reached", "round_cap"],
                        "content_scale": {}, "ignored_components": [],
                        "hooks_filled": {"snapshot_metric": True, "snapshot_resources": True,
                                         "hand_snapshot": False, "state_signature": True,
                                         "clone_state": True, "win_path": True}}
                return (json.dumps(meta, ensure_ascii=False)
                        + "\n\n```python\n" + FILLED_CODE + "\n```")
            data = {"simulatable": False,
                    "reason": "в core нет условия победы — непонятно, когда партия выиграна",
                    "missing": ["win_condition"]}
            return json.dumps(data, ensure_ascii=False)

        if "извлекатель структуры" in system:
            calls["extract"] += 1
            return json.dumps(_spec(), ensure_ascii=False)

        calls["mirror"] += 1
        if "Ответов автора пока нет" in user:
            data = {"phase": "mirror", "understanding": "Денежная гонка.",
                    "map": [{"node": "условие победы", "status": "ok", "note": ""}],
                    "questions": [{"id": 1, "question": "Порог победы?",
                                   "why": "для симуляции", "type": "rules"}],
                    "ready_to_proceed": False}
        else:
            data = {"phase": "confirmed", "original_text": "текст", "author_clarifications": [],
                    "still_open": [], "ready_to_proceed": True}
        return "Ответ агента.\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


register("mock", MockProvider)

import app as A  # noqa: E402
from models import GameSkeleton  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

# --- доводим до принятой структуры --------------------------------------------
c.get(f"/documents/{doc_id}/mirror/1")
c.post(f"/documents/{doc_id}/mirror/1/reply",
       data={"answer": "всё ясно, продолжаем"}, follow_redirects=True)
c.get(f"/documents/{doc_id}/spec/1")

checks = {}

# --- скелет ДО приёмки недоступен ----------------------------------------------
before = c.get(f"/documents/{doc_id}/skeleton/1", follow_redirects=True).get_data(as_text=True)
checks["скелет до приёмки закрыт"] = "Сначала примите структуру" in before
checks["симуляционист до приёмки не звался"] = calls["sim"] == 0

# --- приёмка + скелет (симулируемая игра) --------------------------------------
c.post(f"/documents/{doc_id}/spec/1/accept", follow_redirects=True)
html = c.get(f"/documents/{doc_id}/skeleton/1").get_data(as_text=True)

checks["симуляционист прогнан"] = calls["sim"] == 1
checks["бейдж симулируема"] = "Игра симулируема" in html
checks["код показан"] = "заполнено симуляционистом" in html
checks["допущение показано"] = "смоделирован случайно" in html
checks["границы модели показаны"] = "Границы модели" in html
checks["субъективное действие названо"] = "invest" in html
checks["число игроков показано"] = ">2<" in html and ">4<" in html
checks["нет статистик на экране"] = "win_rate_by_seat" not in html and "seat_fairness" not in html
checks["есть кнопка скачать"] = "Скачать game_skeleton.py" in html

with A.app.app_context():
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["скелет сохранён симулируемым"] = sk.simulatable is True
    checks["код в базе"] = "заполнено симуляционистом" in (sk.code or "")

# повторный заход НЕ прогоняет агента снова (кэш результата)
c.get(f"/documents/{doc_id}/skeleton/1")
checks["повторный заход не зовёт агента"] = calls["sim"] == 1

# скачивание .py
dl = c.get(f"/documents/{doc_id}/skeleton/1.py")
checks["файл .py отдан"] = dl.status_code == 200 and "заполнено симуляционистом" in dl.get_data(as_text=True)
checks["mime питона"] = "python" in dl.headers.get("Content-Type", "")

# --- ветка «несимулируема» (через пересборку) ----------------------------------
scenario["simulatable"] = False
c.post(f"/documents/{doc_id}/skeleton/1/retry", follow_redirects=True)
nores = c.get(f"/documents/{doc_id}/skeleton/1").get_data(as_text=True)
checks["бейдж несимулируема"] = "нельзя симулировать" in nores
checks["причина показана"] = "нет условия победы" in nores
checks["недостающее показано"] = "win_condition" in nores
checks["кода нет"] = "заполнено симуляционистом" not in nores
with A.app.app_context():
    sk = GameSkeleton.query.filter_by(document_id=doc_id, game_index=1).first()
    checks["в базе несимулируема"] = sk.simulatable is False and sk.code is None

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print("\nВСЁ ОК")
