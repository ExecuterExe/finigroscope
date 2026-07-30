# -*- coding: utf-8 -*-
"""Проверка НОВОГО структурированного рендеринга страницы «Стадия понимания».

Регистрирует поддельного провайдера, возвращающего реалистичный JSON прохода 1
(с картой узлов и вопросом про компоненты), проходит поток и проверяет, что на
странице появились структурные элементы (карта узлов, карточки вопросов, шаги),
а сырой markdown ушёл в сворачиваемый блок.
"""
import json
import os
import re
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review.llm_provider import LLMProvider, register  # noqa: E402


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        data = {
            "phase": "mirror",
            "understanding": "«КДИ» — психологический детектив на 3-6 игроков.",
            "map": [
                {"node": "число игроков", "status": "ok", "note": "3-6"},
                {"node": "компоненты и материалы", "status": "unclear",
                 "note": "есть 50 карт дела и 150 карт искажений, но нет примеров их содержания"},
                {"node": "условие поражения", "status": "missing", "note": "не описано"},
            ],
            "questions": [
                {"id": 1, "question": "Приведите текст 2-3 карт дела и 2-3 карт искажений.",
                 "why": "содержание карт определяет баланс", "type": "components"},
                {"id": 2, "question": "Что считается проигрышем?",
                 "why": "нужно для симуляции", "type": "rules"},
            ],
            "ready_to_proceed": False,
        }
        return ("**Как я понял игру** ...\n\n**Карта понимания**\n\n| Узел | Статус |\n|---|---|\n| ... | ... |\n\n"
                "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```")


register("mock", MockProvider)

import app as A  # noqa: E402
from tests.make_sample import build_sample  # noqa: E402

c = A.app.test_client()
essay = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
with open(essay, "rb") as f:
    r = c.post("/upload", data={"doc_type": "essay", "file": (f, "e.docx")},
               content_type="multipart/form-data", follow_redirects=True)
doc_id = int(re.search(r"/documents/(\d+)/games", r.request.path).group(1))

html = c.get(f"/documents/{doc_id}/mirror/1").get_data(as_text=True)

checks = {
    "шаги процесса (mirror-steps)": "mirror-steps" in html,
    "карта узлов (map-node) x3": html.count("map-node") == 3,
    "статус ok у узла": "st-ok" in html,
    "статус unclear у узла": "st-warn" in html,
    "статус missing у узла": "st-miss" in html,
    "карточки вопросов (qa-item) x2": html.count("qa-item") == 2,
    "бейдж «компоненты»": "qa-badge-comp" in html and "компоненты" in html,
    "понимание отрисовано": "mcard-understanding" in html,
    "форма ответа есть": "mirror-reply-form" in html,
    "сырой markdown спрятан в details": "raw-toggle" in html and "<details" in html,
    "нет сырых ** в основном виде (вне details)": html.split("raw-toggle")[0].count("**") == 0,
}
for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)

assert all(checks.values()), "часть проверок структурированного рендера провалилась"
print("\nВСЁ ОК")
