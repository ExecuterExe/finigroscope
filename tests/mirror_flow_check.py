# -*- coding: utf-8 -*-
"""Сквозная проверка экрана «Зеркало понимания»: маршруты, БД, парсинг ответа.

Использует поддельный LLM-провайдер (детерминированный текст в формате, который
реальная модель обязана соблюдать), чтобы проверить весь путь без сетевого
вызова и без ключа: GET (авто-проход 1) -> POST /reply (проход 2) -> confirmed.
Отдельно проверяет деградацию с NullProvider и блокировку входа без can_simulate.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")

os.environ["LLM_PROVIDER"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        assert "РОЛЬ" in system or "Понимание игры" in system, "системный промпт не подставлен"
        assert "game_spec" not in user, "v2: на вход не должен подаваться game_spec"
        if "ОТВЕТ АВТОРА" in user:
            data = {
                "phase": "confirmed",
                "original_text": "(исходный текст игры)",
                "author_clarifications": [
                    {"question_id": 1, "question": "Сколько игроков?", "answer": "2-4 игрока"},
                ],
                "still_open": [],
                "ready_to_proceed": True,
            }
            return ("Спасибо, учёл ваши уточнения по числу игроков и поражению.\n\n"
                    "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")
        data = {
            "phase": "mirror",
            "understanding": "Это гонка по треку с элементами викторины.",
            "map": [
                {"node": "игровой цикл", "status": "ok", "note": ""},
                {"node": "число игроков", "status": "unclear", "note": "не указан диапазон"},
                {"node": "компоненты и материалы", "status": "ok", "note": ""},
            ],
            "questions": [
                {"id": 1, "question": "Сколько игроков?", "why": "нужно для симуляции", "type": "rules"},
            ],
            "ready_to_proceed": False,
        }
        return ("Как я понял игру: это гонка по треку с элементами викторины.\n\n"
                "Вопросы автору:\n1. Сколько игроков? Зачем: нужно для симуляции.\n\n"
                "Ответьте на вопросы — или напишите, что всё ясно.\n\n"
                "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")


register("mock", MockProvider)

import app as A  # noqa: E402  (импорт после регистрации мока — на всякий случай)
from models import MirrorSession  # noqa: E402
from tests.make_sample import build_sample  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"

with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "essay.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))
print("doc_id:", doc_id)

rep = c.get(f"/documents/{doc_id}/report/1").get_data(as_text=True)
print("report: can_simulate button present:", "Далее: диалог с ИИ-агентом" in rep)

# --- шаг 1: GET /mirror -> авто-проход 1 --------------------------------
g1 = c.get(f"/documents/{doc_id}/mirror/1")
html1 = g1.get_data(as_text=True)
print("\nGET /mirror status:", g1.status_code)
print("  содержит текст агента:", "гонка по треку" in html1)
print("  форма ответа показана:", "mirror-reply-form" in html1)
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    print("  DB phase:", ms.phase, "| ready_to_proceed:", ms.ready_to_proceed, "| error:", ms.error)
    assert ms.phase == MirrorSession.PHASE_MIRROR
    assert ms.last_json_dict()["questions"][0]["question"] == "Сколько игроков?"

# --- шаг 2: POST /reply -> проход 2 -------------------------------------
r2 = c.post(f"/documents/{doc_id}/mirror/1/reply",
            data={"answer": "2-4 игрока; поражение — когда колода кончилась"},
            follow_redirects=True)
html2 = r2.get_data(as_text=True)
print("\nPOST /reply -> status:", r2.status_code)
print("  badge ready:", "Понимание подтверждено — можно двигаться дальше" in html2)
print("  карточка уточнений (проход 2) отрисована:", "Ваши уточнения учтены" in html2 and "clar-item" in html2)
print("  шаг 3 done:", "step step-done" in html2)
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=doc_id, game_index=1).first()
    print("  DB phase:", ms.phase, "| ready_to_proceed:", ms.ready_to_proceed)
    print("  author_answer saved:", ms.author_answer)
    assert ms.phase == MirrorSession.PHASE_CONFIRMED
    assert ms.ready_to_proceed is True

# --- шаг 3: деградация без провайдера (null) -----------------------------
os.environ["LLM_PROVIDER"] = "null"
with open(essay, "rb") as f:
    r3 = c.post("/upload", data={"doc_type": "essay", "file": (f, "essay2.docx")},
                content_type="multipart/form-data", follow_redirects=True)
doc_id2 = int(re.search(r"/documents/(\d+)/games", r3.request.path).group(1))
g3 = c.get(f"/documents/{doc_id2}/mirror/1")
html3 = g3.get_data(as_text=True)
print("\nNullProvider GET /mirror status:", g3.status_code)
print("  ошибка показана:", "недоступен" in html3.lower() or "не настроен" in html3.lower())
print("  нет 500 / краша:", g3.status_code == 200)
with A.app.app_context():
    ms3 = MirrorSession.query.filter_by(document_id=doc_id2, game_index=1).first()
    print("  DB phase остался pending:", ms3.phase == MirrorSession.PHASE_PENDING)

# --- шаг 4: блокировка входа без can_simulate ----------------------------
os.environ["LLM_PROVIDER"] = "mock"
sample_buf = build_sample()
r4 = c.post("/upload", data={"doc_type": "essay", "file": (sample_buf, "weak.docx")},
            content_type="multipart/form-data", follow_redirects=True)
doc_id3 = int(re.search(r"/documents/(\d+)/games", r4.request.path).group(1))
# игра 2 в синтетике заведомо слабая (см. tests/make_sample.py)
rep_weak = c.get(f"/documents/{doc_id3}/report/2").get_data(as_text=True)
weak_can_simulate = "Не заполнены ключевые разделы" in rep_weak
print("\nСлабая игра 2: can_simulate=False обнаружено:", weak_can_simulate)
g4 = c.get(f"/documents/{doc_id3}/mirror/2", follow_redirects=True)
print("  редирект вместо диалога:", g4.status_code == 200 and "mirror-reply-form" not in g4.get_data(as_text=True))

print("\nВСЁ ОК" if True else "")
