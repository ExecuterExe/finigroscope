# -*- coding: utf-8 -*-
"""Агент особенностей: фильтр библиотеки и детерминированные проверки ответа.

Обращений к модели нет. Проверяется то, за что отвечает код: какие приёмы агент
вообще даёт выбрать и какой ответ он отказывается принять.

Главное, что здесь защищается, — заполненность шаблона концепции (единственный
раздел этапа с шаблоном в Приложении А) и честность двух разделов, про которые
проще всего соврать в обе стороны: помощь отстающим и адаптация для ОВЗ. Пустой
раздел при заказанной помощи — невыполненный заказ; заполненный при
незаказанной — обещание того, чего в игре нет.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import features  # noqa: E402


MECHANICS = {
    "title": "Совместный поиск",
    "game_loop": {"turn_order": "по часовой стрелке"},
    "win_condition": {"description": "собрать все ключи"},
    "required_component_types": ["карты", "жетоны"],
}

STORY = {
    "title": "Ключи старой библиотеки",
    "player_role": "юные хранители",
    "artifacts": [{"component": "карты", "name": "Комнаты библиотеки"},
                  {"component": "жетоны", "name": "Ключи"}],
}

CONCEPT = ("Цель игры — развлечение: команда хранителей ищет ключи от читального "
           "зала и открывает его до того, как догорит свеча. Игра рассчитана на "
           "2-4 игроков в возрасте от 6 до 9 лет. Средняя партия длится около "
           "15 минут и держится на совместном решении, куда идти дальше.")


def params(**over):
    base = {
        "purpose": ["Развлечение"],
        "player_count": {"min": 2, "max": 4},
        "age_group": {"min": 6, "max": 9},
        "play_time": {"min": 5, "max": 15},
        "interaction": "кооперативное",
        "complexity": "низкая",
        "catch_up": True,
        "elimination": False,
        "adaptation": False,
        "writing_required": False,
        "randomness": True,
        "disabilities": [],
    }
    base.update(over)
    return base


def feature(fid="FEAT_SHARED_PROGRESS", **over):
    base = {
        "feature_id": fid,
        "title": "Общая копилка ключей",
        "description": ("Найденные ключи кладутся в общую копилку: продвижение "
                        "одного хранителя приближает победу всей команды."),
        "why_it_matters": "Никто не чувствует себя лишним за столом.",
    }
    base.update(over)
    return base


def variant(number=1, **over):
    base = {
        "variant_id": number,
        "concept": CONCEPT,
        "features": [feature("FEAT_SHARED_PROGRESS"),
                     feature("FEAT_SKIP_NOT_OUT",
                             title="Пропуск вместо вылета",
                             description=("Попавший в ловушку хранитель "
                                          "пропускает ход, но остаётся в "
                                          "партии до самого конца."))],
        "catch_up_help": ("Ключи лежат в общей копилке, поэтому находка любого "
                          "игрока приближает общую победу; отставший ничего не "
                          "теряет и может обсудить свой ход с командой."),
        "accessibility": None,
        "fit_rationale": "Набор не спорит с кооперативным циклом.",
        "risks": ["дети могут забыть про свечу"],
    }
    base.update(over)
    return base


def three(**over):
    """Три варианта с РАЗНЫМИ наборами приёмов."""
    sets = [
        [feature("FEAT_SHARED_PROGRESS"), feature("FEAT_SKIP_NOT_OUT")],
        [feature("FEAT_HELP_HAND"), feature("FEAT_OPEN_INFO")],
        [feature("FEAT_SHORT_TURN"), feature("FEAT_SHARED_WIN")],
    ]
    if "features" in over:
        sets = [over.pop("features")] * 3
    return [variant(i + 1, features=s, **over) for i, s in enumerate(sets)]


def answer(*variants, **over):
    base = {
        "error": None,
        "invented_features": [],
        "variants": list(variants),
        "recommended_variant_id": 1,
        "self_check": {"no_retelling": True},
    }
    base.update(over)
    return base


def check(data, p=None, lib=None, mode=features.MODE_STRICT):
    p = p or params()
    lib = lib if lib is not None else features.filter_library(p)[0]
    return features.validate(data, lib, p, MECHANICS, STORY, mode)


def problems_of(data, **kwargs):
    return check(data, **kwargs)[0]


# --------------------------------------------------------------------------
# Библиотека и фильтр
# --------------------------------------------------------------------------

def test_библиотека_читается_и_приёмы_описаны_полностью():
    library = features.load_library()
    assert library["features"]
    for item in library["features"]:
        for field in ("id", "name", "kind", "description", "how_it_helps", "tags"):
            assert item.get(field), "у %s нет поля %s" % (item.get("id"), field)


def test_приём_без_нужного_параметра_отсеивается():
    """Фора отстающему при catch_up = false никому не нужна."""
    kept, dropped = features.filter_library(params(catch_up=False))
    assert all("catch_up" not in (f["tags"].get("requires") or []) for f in kept)
    assert any("нужен параметр" in d["reason"] for d in dropped)


def test_приём_противоречащий_параметру_отсеивается():
    """Замена выбывания в игре, где выбывание разрешено, — приём не про неё."""
    kept, dropped = features.filter_library(params(elimination=True))
    ids = {f["id"] for f in kept}
    assert "FEAT_SKIP_NOT_OUT" not in ids
    assert any("несовместим с параметром" in d["reason"] for d in dropped)


def test_адаптационные_приёмы_появляются_только_при_адаптации():
    without = {f["id"] for f in features.filter_library(params())[0]}
    with_it = {f["id"] for f in features.filter_library(params(adaptation=True))[0]}
    assert "FEAT_TACTILE_MARKS" not in without
    assert "FEAT_TACTILE_MARKS" in with_it


def test_взаимодействие_сужает_выбор():
    kept, dropped = features.filter_library(params(interaction="конкурентное",
                                                   catch_up=True))
    assert "FEAT_SHARED_WIN" not in {f["id"] for f in kept}
    assert any("взаимодействии" in d["reason"] for d in dropped)


def test_причина_отсева_названа_у_каждого():
    _, dropped = features.filter_library(params(catch_up=False, elimination=True))
    assert dropped
    assert all(d["reason"] for d in dropped)


def test_нехватка_приёмов_включает_достройку():
    narrow = params(interaction="соло", complexity="высокая",
                    age_group={"min": 3, "max": 5}, catch_up=False)
    kept, dropped = features.filter_library(narrow)
    assert len(kept) < features.MIN_FOR_STRICT
    assert features.choose_mode(kept, dropped) == features.MODE_INVENT


# --------------------------------------------------------------------------
# Годный ответ
# --------------------------------------------------------------------------

def test_нормальный_ответ_принимается():
    problems, warnings = check(answer(*three()))
    assert problems == [], problems
    assert warnings == []


def test_признание_модели_это_предупреждение():
    data = answer(*three())
    data["self_check"]["no_retelling"] = False
    problems, warnings = check(data)
    assert problems == []
    assert warnings and "no_retelling" in warnings[0]


def test_отказ_модели_это_нарушение():
    problems = problems_of(answer(*three(), error="приёмы не сочетаются"))
    assert problems and "отказалась" in problems[0]


# --------------------------------------------------------------------------
# Концепция: единственный раздел этапа с шаблоном
# --------------------------------------------------------------------------

def test_концепция_без_чисел_шаблона_не_принимается():
    plain = ("Цель игры — развлечение. Команда хранителей ищет ключи от "
             "читального зала и старается открыть его до того, как догорит "
             "свеча в главном зале библиотеки.")
    problems = problems_of(answer(*three(concept=plain)))
    assert any("не названо" in p for p in problems)
    assert any("число игроков" in p for p in problems)


def test_в_концепции_нужен_возраст():
    without_age = CONCEPT.replace("от 6 до 9 лет", "для младших школьников")
    problems = problems_of(answer(*three(concept=without_age)))
    assert any("возраст" in p for p in problems)


def test_в_концепции_нужна_длительность():
    without_time = CONCEPT.replace("около 15 минут", "недолго")
    problems = problems_of(answer(*three(concept=without_time)))
    assert any("длительность партии" in p for p in problems)


def test_слишком_короткая_концепция_не_принимается():
    problems = problems_of(answer(*three(concept="Игра для 2-4 игроков 6-9 лет, 15 мин.")))
    assert any("короче" in p for p in problems)


def test_без_концепции_не_принимается():
    problems = problems_of(answer(*three(concept="")))
    assert any("нет концепции" in p for p in problems)


# --------------------------------------------------------------------------
# Помощь отстающим — в обе стороны
# --------------------------------------------------------------------------

def test_заказанная_помощь_обязана_быть_описана():
    problems = problems_of(answer(*three(catch_up_help=None)))
    assert any("раздел пуст" in p for p in problems)


def test_декларация_вместо_правила_не_принимается():
    problems = problems_of(answer(*three(catch_up_help="Игроки помогают друг другу.")))
    assert any("декларацией" in p for p in problems)


def test_незаказанная_помощь_не_выдумывается():
    """Обещание того, чего в игре нет, прочитает автор, а потом игрок."""
    p = params(catch_up=False, elimination=True)
    problems = problems_of(answer(*three()), p=p)
    assert any("не заказывали" in p_ for p_ in problems)


def test_при_запрете_выбывания_раздел_допустим_и_без_заказа():
    """Невыбывание само по себе помогает отстающему — сказать об этом можно."""
    p = params(catch_up=False, elimination=False)
    problems = problems_of(answer(*three()), p=p)
    assert not any("не заказывали" in p_ for p_ in problems)


# --------------------------------------------------------------------------
# Адаптация для ОВЗ
# --------------------------------------------------------------------------

def test_заявленная_адаптация_обязана_быть_описана():
    p = params(adaptation=True, disabilities=["Нарушения зрения"])
    problems = problems_of(answer(*three()), p=p)
    assert any("раздел пуст" in p_ for p_ in problems)


def test_группы_овз_должны_быть_названы():
    p = params(adaptation=True, disabilities=["Нарушения зрения", "Нарушения слуха"])
    vague = "Компоненты сделаны удобными для всех игроков без исключения."
    problems = problems_of(answer(*three(accessibility=vague)), p=p)
    assert any("не названы заявленные группы" in p_ for p_ in problems)


def test_названные_группы_принимаются():
    p = params(adaptation=True, disabilities=["Нарушения зрения"])
    good = ("Ключи различаются формой и рельефом, а не только цветом, поэтому "
            "игра остаётся доступной при нарушениях зрения.")
    problems = problems_of(answer(*three(accessibility=good)), p=p)
    assert not any("группы ОВЗ" in p_ for p_ in problems)


# --------------------------------------------------------------------------
# Приёмы: происхождение, количество, неповторяемость
# --------------------------------------------------------------------------

def test_приём_вне_библиотеки_не_принимается():
    problems = problems_of(answer(*three(features=[feature("FEAT_MADE_UP"),
                                                   feature("FEAT_SKIP_NOT_OUT")])))
    assert any("нет ни в библиотеке" in p for p in problems)


def test_одна_особенность_это_не_особенности():
    problems = problems_of(answer(*three(features=[feature()])))
    assert any("особенностей 1" in p for p in problems)


def test_слишком_много_особенностей_не_принимается():
    many = [feature("FEAT_SHARED_PROGRESS"), feature("FEAT_SKIP_NOT_OUT"),
            feature("FEAT_HELP_HAND"), feature("FEAT_OPEN_INFO"),
            feature("FEAT_SHORT_TURN"), feature("FEAT_SHARED_WIN"),
            feature("FEAT_LUCK_SOFTENER")]
    problems = problems_of(answer(*three(features=many)))
    assert any("допустимо от" in p for p in problems)


def test_один_приём_дважды_это_одна_особенность():
    twice = [feature("FEAT_SHARED_PROGRESS"),
             feature("FEAT_SHARED_PROGRESS", title="И ещё копилка")]
    problems = problems_of(answer(*three(features=twice)))
    assert any("использован дважды" in p for p in problems)


def test_короткое_описание_приёма_не_принимается():
    """По «применяется общая копилка» непонятно, как это выглядит в игре."""
    short = [feature(description="Есть копилка."), feature("FEAT_SKIP_NOT_OUT")]
    problems = problems_of(answer(*three(features=short)))
    assert any("слишком коротко" in p for p in problems)


def test_три_варианта_из_одного_набора_это_один_вариант():
    same = [feature("FEAT_SHARED_PROGRESS"), feature("FEAT_SKIP_NOT_OUT")]
    problems = problems_of(answer(variant(1, features=same),
                                  variant(2, features=same),
                                  variant(3, features=same)))
    assert any("одного набора приёмов" in p for p in problems)


def test_придуманный_приём_с_описанием_принимается_в_режиме_достройки():
    invented = {"id": "FEAT_NEW_TEAMWORK", "name": "Общий ход", "kind": "together",
                "description": "о", "how_it_helps": "п"}
    data = answer(*three(features=[feature("FEAT_NEW_TEAMWORK"),
                                   feature("FEAT_SKIP_NOT_OUT")]),
                  invented_features=[invented])
    problems = problems_of(data, mode=features.MODE_INVENT)
    assert not any("нет ни в библиотеке" in p for p in problems)


def test_придумывать_вне_режима_достройки_нельзя():
    invented = {"id": "FEAT_NEW_TEAMWORK", "name": "Общий ход", "kind": "together",
                "description": "о", "how_it_helps": "п"}
    problems = problems_of(answer(*three(), invented_features=[invented]))
    assert any("режим достройки" in p for p in problems)


# --------------------------------------------------------------------------
# Особенности не переписывают принятое
# --------------------------------------------------------------------------

def test_переопределение_условия_победы_не_принимается():
    """Условие победы задано механиками и проверено дважды."""
    problems = problems_of(answer(*three(
        fit_rationale="Побеждает тот, кто первым соберёт больше всех ключей.")))
    assert any("переопределяют уже принятое" in p for p in problems)


def test_количество_компонентов_не_называется():
    problems = problems_of(answer(*three(
        catch_up_help="В общей копилке лежат 3 ключа, доступные всем игрокам "
                      "команды без исключения, и это выравнивает шансы.")))
    assert any("количество компонентов" in p for p in problems)


def test_пугающее_слово_не_проходит_для_младших():
    problems = problems_of(answer(*three(
        risks=["ребёнок может испугаться смерти хранителя"])))
    assert any("рассчитана с 6 лет" in p for p in problems)


def test_вариантов_должно_быть_три():
    problems = problems_of(answer(variant(1), variant(2)))
    assert any("ровно три" in p for p in problems)


def test_рекомендованный_вариант_обязан_существовать():
    problems = problems_of(answer(*three(), recommended_variant_id=9))
    assert any("recommended_variant_id" in p for p in problems)


# --------------------------------------------------------------------------
# Вход агента
# --------------------------------------------------------------------------

def test_без_принятых_модулей_агент_не_запускается():
    with pytest.raises(features.NotEnoughFeatures) as error:
        features.generate(params(), None, STORY)
    assert "механик" in str(error.value)

    with pytest.raises(features.NotEnoughFeatures) as error:
        features.generate(params(), MECHANICS, None)
    assert "сюжета" in str(error.value)


def test_в_промпт_уходят_оба_модуля():
    lib = features.for_prompt(features.filter_library(params())[0])
    message = features.build_user_message(params(), MECHANICS, STORY, lib)
    assert "ПРИНЯТЫЙ МОДУЛЬ МЕХАНИК" in message
    assert "ПРИНЯТЫЙ МОДУЛЬ СЮЖЕТА" in message
    assert "собрать все ключи" in message
    assert "Ключи старой библиотеки" in message


def test_в_промпт_не_уходят_лишние_поля_модулей():
    """Обоснования и риски предыдущих этапов только провоцируют пересказ."""
    noisy = dict(MECHANICS, fit_rationale="длинное обоснование", risks=["риск"])
    lib = features.for_prompt(features.filter_library(params())[0])
    message = features.build_user_message(params(), noisy, STORY, lib)
    assert "длинное обоснование" not in message
