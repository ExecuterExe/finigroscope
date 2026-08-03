# -*- coding: utf-8 -*-
"""Проверка сверки понимания: компоненты, самопроверка и удобство формы ответа.

Три вещи, за которыми следит этот набор.

1. Незаданный вопрос о компонентах. Он ничем себя не выдаёт: карта выглядит
   полной, автор соглашается, а через четыре шага целая группа проверок баланса
   артефактов уходит в «не выполнено» — и вернуться уже нельзя, сверка закрыта.
2. Потеря уточнений автора между раундами. На вход извлеченца идут именно они.
3. Форма ответа: поле не тащит прошлый ответ в новый раунд, а вопросы видны
   рядом с полем, а не в начале длинной страницы.
"""
import json
import os
import sys

sys.path.insert(0, ".")
os.environ["LLM_PROVIDER_FORCE"] = "mock"

from review import mirror as M  # noqa: E402
from review.llm_provider import LLMProvider, register  # noqa: E402

checks = {}


def codes(issues):
    return [i["code"] for i in issues]


def node(name, status, note=""):
    return {"node": name, "status": status, "note": note}


def data(map_=None, questions=None, **kw):
    base = {"phase": "mirror", "understanding": "Игра про деньги.",
            "map": map_ if map_ is not None else [
                node("число игроков", "ok"),
                node("компоненты и материалы", "ok"),
            ],
            "questions": questions if questions is not None else [],
            "ready_to_proceed": False}
    base.update(kw)
    return base


CORE_Q = {"id": 1, "question": "Что считается проигрышем?", "why": "нужно для симуляции",
          "type": "rules"}
COMP_Q = {"id": 2, "question": "Сколько карт событий и какие они бывают?",
          "why": "без состава колоды баланс карт не проверить", "type": "components"}

# ============================================================================
# ЧАСТЬ 1. Узел компонентов
# ============================================================================
checks["узел компонентов найден по названию"] = M.components_node(
    [node("Компоненты и материалы", "ok")]) is not None
checks["узел найден по слову «материалы»"] = M.components_node(
    [node("Материалы игры", "ok")]) is not None
checks["чужой узел не считается компонентами"] = M.components_node(
    [node("число игроков", "ok")]) is None

checks["вопрос узнаётся по type"] = M.asks_about_components(
    [{"question": "что-то", "type": "components"}]) is True
# Модели часто забывают проставить type — тогда узнаём по смыслу.
checks["вопрос узнаётся по тексту без type"] = M.asks_about_components(
    [{"question": "Сколько карт в колоде?", "why": ""}]) is True
checks["вопрос узнаётся по слову «жетон»"] = M.asks_about_components(
    [{"question": "Сколько жетонов в наборе?"}]) is True
checks["посторонний вопрос не считается"] = M.asks_about_components([CORE_Q]) is False

# ============================================================================
# ЧАСТЬ 2. Валидатор: главный пропуск — незаданный вопрос о компонентах
# ============================================================================
c = codes(M.validate_pass(data(map_=[node("компоненты и материалы", "unclear")],
                               questions=[CORE_Q])))
checks["пропущенный вопрос о компонентах пойман"] = "components_not_asked" in c

c = codes(M.validate_pass(data(map_=[node("компоненты и материалы", "missing")],
                               questions=[CORE_Q])))
checks["missing тоже требует вопроса"] = "components_not_asked" in c

ok_case = M.validate_pass(data(map_=[node("компоненты и материалы", "unclear")],
                               questions=[CORE_Q, COMP_Q]))
checks["с вопросом о компонентах замечаний нет"] = ok_case == []

checks["ясный узел вопроса не требует"] = M.validate_pass(
    data(map_=[node("компоненты и материалы", "ok")], questions=[CORE_Q])) == []

c = codes(M.validate_pass(data(map_=[node("число игроков", "ok")])))
checks["отсутствие узла компонентов пойман"] = "components_node_missing" in c

c = codes(M.validate_pass(data(map_=[node("компоненты и материалы", "absent")])))
checks["absent у компонентов пойман"] = "components_marked_absent" in c

# Пустая карта — это не «всё хорошо»: проверять просто нечего (проход 2/3).
checks["без карты компоненты не проверяются"] = M.validate_pass(data(map_=[])) == []

# ============================================================================
# ЧАСТЬ 3. Прочие молчаливые пропуски
# ============================================================================
many = [dict(CORE_Q, id=i, question=f"Вопрос {i}") for i in range(12)]
c = codes(M.validate_pass(data(map_=[node("компоненты", "ok")], questions=many)))
checks["перебор вопросов пойман"] = "too_many_questions" in c

c = codes(M.validate_pass(data(map_=[node("компоненты", "ok")],
                               questions=[{"id": 1, "question": "Зачем игра?"}])))
checks["вопрос без «зачем» пойман"] = "question_without_why" in c

# Уточнения обязаны накапливаться: на них строится вход извлеченца.
prior = {"author_clarifications": [{"question": "1", "answer": "a"},
                                   {"question": "2", "answer": "b"}]}
c = codes(M.validate_pass(
    data(map_=[], author_clarifications=[{"question": "2", "answer": "b"}]),
    round_no=2, prior_json=prior))
checks["потеря уточнений поймана"] = "clarifications_lost" in c
checks["накопленные уточнения проходят"] = "clarifications_lost" not in codes(
    M.validate_pass(data(map_=[], author_clarifications=prior["author_clarifications"]
                         + [{"question": "3", "answer": "c"}]),
                    round_no=2, prior_json=prior))
checks["в первом раунде уточнения не сверяются"] = "clarifications_lost" not in codes(
    M.validate_pass(data(map_=[]), round_no=1, prior_json=prior))

# --- ответ без машинного блока: сверка закрывается, и это надо заметить -------
lost = M.validate_pass(None)
checks["потерянный JSON пойман"] = codes(lost) == ["json_missing"]
checks["потеря JSON блокирующая"] = lost[0]["severity"] == "error"
checks["пустой объект — не потеря"] = codes(M.validate_pass({})) == []

# ============================================================================
# ЧАСТЬ 4. Промпт требует полного разбора компонентов
# ============================================================================
from review import prompts  # noqa: E402

PROMPT = prompts.load_mirror_prompt()
checks["промпт требует qty"] = "qty" in PROMPT
checks["промпт требует функцию"] = "function" in PROMPT
checks["промпт требует примеры содержания"] = "примеры самого содержания" in PROMPT
checks["промпт защищает слот вопроса"] = "нельзя вытеснить другими" in PROMPT
checks["промпт запрещает ok без деталей"] = "не `ok`" in PROMPT
checks["в приложении разведены три подузла"] = (
    "9а. Компоненты: функция" in PROMPT and "9б. Компоненты: состав" in PROMPT)

# ============================================================================
# ЧАСТЬ 5. Экран: форма ответа
# ============================================================================
FIRST = data(map_=[node("число игроков", "ok"),
                   node("компоненты и материалы", "unclear",
                        "названы карты событий, но состав не раскрыт"),
                   node("условие поражения", "missing")],
             questions=[CORE_Q, COMP_Q])
SECOND = data(map_=[node("компоненты и материалы", "unclear")],
              questions=[{"id": 3, "question": "Какие бывают события в колоде?",
                          "why": "нужен состав", "type": "components"}],
              author_clarifications=[{"question": CORE_Q["question"], "answer": "разорился"}])


class MirrorMock(LLMProvider):
    """Проход 1 отдаёт вопросы, проход 2 — новые вопросы (агент переспрашивает)."""

    name = "mock"

    def _complete(self, system, user, **opts):
        if "Ответов автора пока нет" in user:
            return "Разобрал игру.\n\n```json\n" + json.dumps(FIRST, ensure_ascii=False) + "\n```"
        return "Учёл ответ.\n\n```json\n" + json.dumps(SECOND, ensure_ascii=False) + "\n```"


register("mock", MirrorMock)

import app as A  # noqa: E402
from models import Document, MirrorSession, User, db  # noqa: E402

ESSAY = r"C:\Users\Eugene\Desktop\НСПК\Фин-игры\Фин-игры эссе - версия от 1 февраля.docx"
cl = A.app.test_client()
cl.get("/dashboard")
with A.app.app_context():
    u = User.query.filter(User.tg_tag.like("@guest-%")).order_by(User.id.desc()).first()
    UID = u.id
    d = Document(user_id=UID, filename="зеркало.docx", stored_path=ESSAY,
                 file_hash="mirror-comp", doc_type="essay", version=1)
    db.session.add(d)
    db.session.commit()
    DOC = d.id

def form_block(html: str) -> str:
    """Кусок страницы от начала формы ответа до её конца — «рядом с формой»."""
    return html.split('class="mirror-reply-form"', 1)[1].split("</form>", 1)[0]


page = cl.get(f"/documents/{DOC}/mirror/1").get_data(as_text=True)
block = form_block(page)
checks["вопросы продублированы у формы"] = "reply-qs" in block
checks["текст вопроса виден у формы"] = COMP_Q["question"] in block
checks["второй вопрос тоже виден"] = CORE_Q["question"] in block
checks["зачем-строка видна у формы"] = "reply-qs-why" in block
checks["нумерованная подсказка в поле"] = "Удобно отвечать по номерам" in block

form = page.split('name="answer"')[1].split("</textarea>")[0]
checks["поле ответа пустое"] = form.rstrip().endswith(">")

ANSWER = "1. Проигрыш — банкротство. 2. Карт событий 40, бывают доход и расход."
cl.post(f"/documents/{DOC}/mirror/1/reply", data={"answer": ANSWER}, follow_redirects=True)
page2 = cl.get(f"/documents/{DOC}/mirror/1").get_data(as_text=True)

# Главное по жалобе: во втором раунде поле НЕ содержит ответ первого раунда.
form2 = page2.split('name="answer"')[1].split("</textarea>")[0]
checks["во втором раунде поле снова пустое"] = ANSWER not in form2
checks["прошлый ответ сохранён в базе"] = True
with A.app.app_context():
    ms = MirrorSession.query.filter_by(document_id=DOC, game_index=1).first()
    checks["прошлый ответ сохранён в базе"] = ms.author_answer == ANSWER
    checks["раунд увеличился"] = ms.round == 2
    checks["замечания сохранены"] = isinstance(ms.issues(), list)

checks["прошлый ответ показан отдельным блоком"] = "Ваши уточнения учтены" in page2
checks["новый вопрос виден у формы"] = "Какие бывают события" in form_block(page2)

# --- самопроверка попадает на экран ------------------------------------------
BAD = data(map_=[node("компоненты и материалы", "unclear")], questions=[CORE_Q])


class BadMock(LLMProvider):
    name = "mock"

    def _complete(self, system, user, **opts):
        return "Разобрал.\n\n```json\n" + json.dumps(BAD, ensure_ascii=False) + "\n```"


register("mock", BadMock)
with A.app.app_context():
    d2 = Document(user_id=UID, filename="без-компонентов.docx", stored_path=ESSAY,
                  file_hash="mirror-nocomp", doc_type="essay", version=1)
    db.session.add(d2)
    db.session.commit()
    DOC2 = d2.id

bad_page = cl.get(f"/documents/{DOC2}/mirror/1").get_data(as_text=True)
checks["самопроверка показана автору"] = "Самопроверка сверки" in bad_page
checks["названа причина пропуска"] = "components_not_asked" in bad_page
checks["автору предложено дописать самому"] = "напишите его в поле ниже" in bad_page
checks["предложен повтор прохода"] = "Прогнать проход заново" in bad_page

# --- ответ без JSON на проходе 2: сверка закрывалась молча и насовсем ---------
# Именно здесь потеря необратима: вопросов в ответе нет, значит переспрашивать
# нечего, оркестратор закрывает стадию — и форма ответа исчезает вместе с фазой.
class NoJsonMock(LLMProvider):
    """Проход 1 в порядке; на сверку модель отвечает прозой без машинного блока."""

    name = "mock"
    replies = 0

    def _complete(self, system, user, **opts):
        if "Ответов автора пока нет" in user:
            return "Разобрал.\n\n```json\n" + json.dumps(FIRST, ensure_ascii=False) + "\n```"
        NoJsonMock.replies += 1
        # Со второго обращения отдаём нормальный ответ — повтор должен вылечить.
        if NoJsonMock.replies > 1:
            return "Учёл ответ.\n\n```json\n" + json.dumps(SECOND, ensure_ascii=False) + "\n```"
        return "Спасибо, я всё понял, вопросов больше нет."


register("mock", NoJsonMock)
with A.app.app_context():
    d3 = Document(user_id=UID, filename="без-json.docx", stored_path=ESSAY,
                  file_hash="mirror-nojson", doc_type="essay", version=1)
    db.session.add(d3)
    db.session.commit()
    DOC3 = d3.id

cl.get(f"/documents/{DOC3}/mirror/1")
cl.post(f"/documents/{DOC3}/mirror/1/reply", data={"answer": "Отвечаю: 40 карт."},
        follow_redirects=True)
nojson = cl.get(f"/documents/{DOC3}/mirror/1").get_data(as_text=True)
checks["потеря JSON видна автору"] = "json_missing" in nojson
checks["карточка видна и без структурного вида"] = "Самопроверка сверки" in nojson
checks["повтор предложен и здесь"] = "Прогнать проход заново" in nojson
with A.app.app_context():
    ms3 = MirrorSession.query.filter_by(document_id=DOC3, game_index=1).first()
    checks["без JSON сверка закрылась"] = ms3.phase == MirrorSession.PHASE_CONFIRMED
    checks["формы ответа больше нет"] = "mirror-reply-form" not in nojson

# Повтор прогоняет тот же раунд с сохранённым ответом автора.
cl.post(f"/documents/{DOC3}/mirror/1/retry", follow_redirects=True)
with A.app.app_context():
    ms3 = MirrorSession.query.filter_by(document_id=DOC3, game_index=1).first()
    checks["после повтора разбор восстановлен"] = bool(ms3.last_json_dict())
    checks["ответ автора не потерян"] = ms3.author_answer == "Отвечаю: 40 карт."
    checks["раунд не потрачен"] = ms3.round == 1
    checks["замечание json_missing снято"] = "json_missing" not in codes(ms3.issues())

for label, ok in checks.items():
    print(("OK  " if ok else "FAIL") + " | " + label)
assert all(checks.values()), "часть проверок провалилась"
print(f"\nВСЁ ОК ({len(checks)} проверок)")
