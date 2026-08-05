# -*- coding: utf-8 -*-
"""Агент правил: проверки того, что изложение верно принятому.

Обращений к модели нет. Проверяется единственное, ради чего этот этап вообще
можно доверить модели: что она ничего не дописала. Правила — последний текст
перед печатью и единственный, который игроки прочитают целиком; любая добавка
попадёт в коробку, не пройдя ни аудитора модуля, ни линзы.

Поэтому почти все проверки здесь — не о качестве текста, а о его верности: числа
сверяются с расчётом этапа 5, выбывание и письменные задания — с ответами
опросника, условие победы — с принятым модулем механик.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import rules  # noqa: E402


MODULES = {
    "mechanics": {
        "title": "Совместный поиск",
        "game_loop": {"turn_order": "по часовой стрелке"},
        "win_condition": {"description": "собрать ключи",
                          "trigger": "3 ключа в общей копилке"},
        "required_component_types": ["карты", "жетоны"],
    },
    "story": {"title": "Ключи старой библиотеки"},
    "features": {"concept": "Кооперативный поиск для младших школьников."},
}

COMPONENTS = [
    {"component": "карты", "quantity": 35, "material": {"chosen": "Картон"}},
    {"component": "жетоны", "quantity": 12, "material": {"chosen": "Картон"}},
]


def params(**over):
    base = {
        "age_group": {"min": 6, "max": 9},
        "player_count": {"min": 2, "max": 4},
        "play_time": {"min": 5, "max": 15},
        "complexity": "низкая",
        "elimination": False,
        "writing_required": False,
        "interaction": "кооперативное",
    }
    base.update(over)
    return base


def variant(number=1, **over):
    base = {
        "variant_id": number,
        "setup": ["Перемешайте карты и положите стопкой в центр стола.",
                  "Жетоны сложите рядом — это общая копилка."],
        "turn": ["Откройте верхнюю карту стопки.",
                 "Если на ней ключ, положите жетон в копилку.",
                 "Передайте ход соседу слева."],
        "special_rules": [{"title": "Никто не ждёт зря",
                           "text": "Попавший в ловушку пропускает один ход и "
                                   "возвращается в игру на следующем круге."}],
        "ending": "Партия заканчивается, когда в копилке окажется 3 ключа — "
                  "тогда команда побеждает вместе.",
        "tips": [{"title": "Первая партия", "text": "Разложите карты открытыми "
                                                    "и пройдите круг вместе.",
                  "for_whom": "ведущему"},
                 {"title": "Частая ошибка", "text": "Жетон кладут себе, а не в "
                                                    "общую копилку.",
                  "for_whom": "всем"}],
        "gaps": [],
        "fit_rationale": "Короткие шаги под возраст 6-9.",
        "risks": ["дети могут спорить, кто открывает карту"],
    }
    base.update(over)
    return base


def answer(*variants, **over):
    base = {
        "error": None,
        "variants": list(variants) or [variant(1), variant(2), variant(3)],
        "recommended_variant_id": 1,
        "self_check": {"no_new_rules": True},
    }
    base.update(over)
    return base


def three(**over):
    return [variant(i + 1, **over) for i in range(3)]


def check(data, p=None, modules=None, components=None):
    return rules.validate(data, p or params(), modules or MODULES,
                          COMPONENTS if components is None else components)


def problems_of(data, **kwargs):
    return check(data, **kwargs)[0]


# --------------------------------------------------------------------------
# Годный ответ
# --------------------------------------------------------------------------

def test_нормальный_ответ_принимается():
    problems, warnings = check(answer(*three()))
    assert problems == [], problems


def test_пустой_ответ_это_нарушение():
    problems = problems_of(answer(variants=[]))
    assert problems and "нет ни одного варианта" in problems[0]


def test_не_объект_это_нарушение():
    problems = problems_of("правила")
    assert problems and "не является объектом" in problems[0]


# --------------------------------------------------------------------------
# Количества компонентов — самая ценная проверка этапа
# --------------------------------------------------------------------------

def test_разошедшееся_количество_не_принимается():
    """Число, разошедшееся с расчётом, попадёт в напечатанную коробку."""
    problems = problems_of(answer(*three(
        setup=["Перемешайте 20 карт и положите стопкой.",
               "Жетоны сложите рядом."])))
    assert any("для «карты» названо количество 20, а посчитано 35" in p
               for p in problems)


def test_верное_количество_проходит():
    problems = problems_of(answer(*three(
        setup=["Перемешайте 35 карт и положите стопкой.",
               "Жетоны сложите рядом."])))
    assert not any("названо количество" in p for p in problems)


def test_без_расчёта_количества_не_проверяются():
    """Расчёта нет — сверять не с чем, и придираться не за что."""
    problems = problems_of(answer(*three(
        setup=["Перемешайте 20 карт.", "Жетоны рядом."])), components=[])
    assert not any("названо количество" in p for p in problems)


# --------------------------------------------------------------------------
# Правила не спорят с ответами опросника
# --------------------------------------------------------------------------

def test_выбывание_при_запрете_не_принимается():
    problems = problems_of(answer(*three(
        ending="Игрок, оставшийся без жетонов, выбывает из партии.")))
    assert any("есть выбывание" in p for p in problems)


def test_при_разрешённом_выбывании_формулировка_допустима():
    p = params(elimination=True)
    problems = problems_of(answer(*three(
        ending="Игрок без жетонов выбывает, остальные продолжают.")), p=p)
    assert not any("выбывание" in x for x in problems)


def test_письменные_задания_при_запрете_не_принимаются():
    problems = problems_of(answer(*three(
        turn=["Откройте карту.", "Запишите найденный ключ на листе.",
              "Передайте ход."])))
    assert any("требуют записывать" in p for p in problems)


# --------------------------------------------------------------------------
# Полнота изложения
# --------------------------------------------------------------------------

def test_без_подготовки_не_принимается():
    problems = problems_of(answer(*three(setup=["Разложите игру."])))
    assert any("подготовка из 1 шаг" in p for p in problems)


def test_без_хода_не_принимается():
    problems = problems_of(answer(*three(turn=["Играйте."])))
    assert any("ход игрока описан 1 шаг" in p for p in problems)


def test_без_концовки_не_принимается():
    problems = problems_of(answer(*three(ending="")))
    assert any("не заполнено поле ending" in p for p in problems)


def test_мало_советов_не_принимается():
    problems = problems_of(answer(*three(tips=[
        {"title": "Один", "text": "совет", "for_whom": "всем"}])))
    assert any("советов 1" in p for p in problems)


def test_рекомендованный_вариант_обязан_существовать():
    problems = problems_of(answer(*three(), recommended_variant_id=9))
    assert any("recommended_variant_id" in p for p in problems)


# --------------------------------------------------------------------------
# Условие победы — предупреждение, а не отказ
# --------------------------------------------------------------------------

def test_потерянное_число_условия_победы_это_предупреждение():
    """Формулировок победы много, и отказывать по числам было бы слишком."""
    problems, warnings = check(answer(*three(
        ending="Партия заканчивается, когда команда соберёт все ключи.")))
    assert problems == []
    assert any("в условии победы модуля есть числа" in w for w in warnings)


def test_совпавшее_число_молчит():
    problems, warnings = check(answer(*three()))
    assert not any("условии победы" in w for w in warnings)


# --------------------------------------------------------------------------
# Вход агента
# --------------------------------------------------------------------------

def test_без_принятых_модулей_агент_не_запускается():
    with pytest.raises(rules.RulesError) as error:
        rules.generate(params(), {"mechanics": MODULES["mechanics"]}, COMPONENTS)
    text = str(error.value)
    assert "story" in text and "features" in text


def test_ошибка_агента_помечена_как_показываемая():
    """Иначе очередь покажет имя класса вместо причины."""
    assert rules.RulesError.user_facing is True


def test_в_промпт_уходят_все_модули_и_компоненты():
    message = rules.build_user_message(params(), MODULES, COMPONENTS)
    assert "МЕХАНИКИ" in message and "СЮЖЕТ" in message and "ОСОБЕННОСТИ" in message
    assert "35 шт." in message and "Картон" in message
    assert "Ничего не добавляй" in message


def test_критика_попадает_в_повторный_запрос():
    message = rules.build_user_message(params(), MODULES, COMPONENTS,
                                       critique=["для «карты» названо 20"],
                                       previous={"variants": []})
    assert "НЕ ПРОШЁЛ ПРОВЕРКУ" in message
    assert "названо 20" in message
